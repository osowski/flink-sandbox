import importlib.util
import unittest
from pathlib import Path

from dtm.mesh.agents.enrolment import DECISION_SCHEMA, decision_facts
from dtm.mesh.agents.reasoning import signal_facts, system_and_user
from dtm.mesh.agents.risk import RISK_SCHEMA, risk_facts
from dtm.mesh.llm import ERROR, OK, TIMEOUT, LLMResponse

_SPEC = importlib.util.spec_from_file_location(
    "benchmark_models", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_models.py")
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)

OPTIONS = {"temperature": 0, "seed": 42, "think": False, "num_ctx": 4096}


class FakeProvider:
    def __init__(self, reply=None):
        self.requests = []
        self._reply = reply or (lambda req: LLMResponse(OK, {"score": 10, "rationale": "r"}
                                                        if req.schema is RISK_SCHEMA else
                                                        {"decision": "GRANT", "rationale": "r"}, {}, 100))

    def complete(self, req):
        self.requests.append(req)
        return self._reply(req)


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        values = list(range(1, 21))
        self.assertEqual(bench.percentile(values, 50), 10)
        self.assertEqual(bench.percentile(values, 95), 19)
        self.assertEqual(bench.percentile([7], 95), 7)


class AgentFactsTest(unittest.TestCase):
    def test_signal_facts_keep_the_four_fields_the_models_see(self):
        raw = [{"signal_id": "s1", "signal_type": "OUTDATED_OS", "severity": "MEDIUM", "detail": "d",
                "source_event_type": "ENROL_REQUESTED", "envelope": {}}]
        self.assertEqual(signal_facts(raw), [{"signalId": "s1", "signalType": "OUTDATED_OS",
                                              "severity": "MEDIUM", "detail": "d"}])

    def test_risk_facts_shape(self):
        facts = risk_facts("IOS 18.1", {"device_id": "d"}, {"customer_id": "c"}, [])
        self.assertEqual(facts, {"fingerprintSummary": "IOS 18.1", "device": {"device_id": "d"},
                                 "customer": {"customer_id": "c"}, "signals": []})

    def test_decision_facts_shape(self):
        self.assertEqual(decision_facts({"device_id": "d"}, {"score": 5}, []),
                         {"device": {"device_id": "d"}, "riskAssessment": {"score": 5}, "signals": []})


class ScenarioTest(unittest.TestCase):
    def test_every_injected_scenario_and_clean_are_covered(self):
        self.assertEqual(set(bench.SCENARIOS), {"CLEAN", "COMPROMISED_OS", "OUTDATED_OS",
                                                "NEW_DEVICE_ESTABLISHED_ACCOUNT", "IMPOSSIBLE_TRAVEL"})

    def test_signals_match_their_scenario(self):
        for name in bench.SCENARIOS:
            facts = bench.risk_input(name)
            types = [s["signalType"] for s in facts["signals"]]
            self.assertEqual(types, [] if name == "CLEAN" else [name], name)

    def test_compromised_device_never_reaches_the_enrolment_model(self):
        self.assertIsNone(bench.enrolment_input("COMPROMISED_OS"))
        self.assertIsNotNone(bench.enrolment_input("OUTDATED_OS"))


class RunTest(unittest.TestCase):
    def test_requests_carry_the_agent_prompts_schemas_and_options(self):
        provider = FakeProvider()
        bench.run(provider, ["qwen3:4b"], runs=1, options=OPTIONS, timeout_ms=30000)
        timed = provider.requests[1:]  # the first call warms the model and is not timed
        risk = [r for r in timed if r.schema is RISK_SCHEMA]
        enrolment = [r for r in timed if r.schema is DECISION_SCHEMA]
        self.assertEqual(len(risk), 5)
        self.assertEqual(len(enrolment), 4)
        self.assertEqual(risk[0].messages,
                         system_and_user(bench.prompt("risk.v1"), bench.risk_input(risk[0].input_message_id)))
        for req in timed:
            self.assertEqual((req.provider, req.model, req.options, req.timeout_ms),
                             ("ollama", "qwen3:4b", OPTIONS, 30000))

    def test_warmup_call_is_not_sampled(self):
        provider = FakeProvider()
        samples = bench.run(provider, ["qwen3:4b", "qwen3:8b"], runs=2, options=OPTIONS, timeout_ms=30000)
        self.assertEqual(len(provider.requests), 2 * (1 + 2 * 9))
        self.assertEqual(len(samples), 2 * 2 * 9)


class SummarizeTest(unittest.TestCase):
    def test_rows_count_schema_valid_and_failures_per_model_and_prompt(self):
        samples = [bench.Sample("m", "risk", "CLEAN", LLMResponse(OK, {"score": 3}, {}, 100)),
                   bench.Sample("m", "risk", "CLEAN", LLMResponse(OK, {"score": 5}, {}, 300)),
                   bench.Sample("m", "risk", "CLEAN",
                                LLMResponse(ERROR, None, {}, 200, error="output violates schema: x")),
                   bench.Sample("m", "risk", "CLEAN", LLMResponse(TIMEOUT, None, None, 30000, error="t"))]
        (row,) = bench.summarize(samples)
        self.assertEqual((row["model"], row["prompt"], row["calls"]), ("m", "risk", 4))
        self.assertEqual((row["schema_valid"], row["schema_invalid"], row["failed"]), (2, 1, 1))
        self.assertEqual((row["p50_ms"], row["p95_ms"]), (200, 30000))  # a timeout counts at its wait

    def test_outcomes_report_the_median_score_and_the_decisions(self):
        samples = [bench.Sample("m", "risk", "CLEAN", LLMResponse(OK, {"score": s}, {}, 1)) for s in (3, 9, 5)]
        samples += [bench.Sample("m", "enrolment", "CLEAN", LLMResponse(OK, {"decision": d}, {}, 1))
                    for d in ("GRANT", "GRANT", "CHALLENGE")]
        (row,) = bench.outcomes(samples)
        self.assertEqual(row, {"model": "m", "scenario": "CLEAN", "risk_score_p50": 5,
                               "decisions": "GRANT×2, CHALLENGE×1"})

    def test_outcomes_tell_a_guardrail_decision_from_failed_calls(self):
        guarded = [bench.Sample("m", "risk", "COMPROMISED_OS", LLMResponse(OK, {"score": 95}, {}, 1))]
        failed = [bench.Sample("m", "enrolment", "CLEAN", LLMResponse(TIMEOUT, None, None, 30000, error="t"))]
        self.assertEqual(bench.outcomes(guarded)[0]["decisions"], "guardrail (no model call)")
        self.assertEqual(bench.outcomes(failed)[0]["decisions"], "no valid output")


if __name__ == "__main__":
    unittest.main()
