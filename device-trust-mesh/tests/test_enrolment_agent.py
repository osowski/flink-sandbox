import importlib.resources
import unittest

from agent_fakes import ScriptedProvider, context_table, make_bus, produced, request_inbound, settings
from dtm.common import a2a, ids
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.enrolment import DECISION_SCHEMA, DEFAULT_CHALLENGE_AT_RISK_SCORE, EnrolmentAgent
from dtm.mesh.agents.kit import AUTH_REQUIRED, COMPLETED, FAILED, REJECTED, SUBMITTED, WORKING
from dtm.mesh.agents.reasoning import AgentLlm
from dtm.mesh.context.server import build_mcp
from dtm.mesh.llm import ERROR, OK, LLMResponse

CLOCK = lambda: 1793323940.5  # noqa: E731
DEVICES = [{"device_id": "dev-0001", "trust_status": "PENDING"}]
CUSTOMERS = [{"customer_id": "cust-001"}]
SIGNAL = {"device_id": "dev-0001", "signal_id": "sig-1", "signal_type": "IMPOSSIBLE_TRAVEL"}
ENVELOPE = "urn:device-trust-mesh:envelope:v1"
TOPIC = "agent.tasks.enrolment"
TASK_ID = ids.task_id("agent:enrolment", "router-0")


def assessment(score=10, signal_ids=()):
    return {"score": score, "rationale": "Looks fine.", "signalIds": list(signal_ids), "llmInteractionId": "llm-1"}


def decide_data(risk=None, **over):
    data = {"taskType": "DECIDE_ENROLMENT", "deviceId": "dev-0001", "customerId": "cust-001"}
    if risk is not None:
        data["riskAssessment"] = risk
    return {**data, **over}


def decided(decision, rationale="Because."):
    content = {"decision": decision, "rationale": rationale}
    return LLMResponse(OK, content, content, 120, 50, 20)


class EnrolmentAgentTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("enrolment")
        self.settings = settings("enrolment", prompt_ref="enrolment.v1", prompt_text="Decide.",
                                 llm={"provider": "recorded", "model": "m"})

    def agent(self, provider, devices=DEVICES, signals=()):
        table = context_table(devices=devices, customers=CUSTOMERS, signals=signals)
        context = SyncContext(build_mcp(table, wait_timeout_ms=50), timeout_s=5.0)
        self.addCleanup(context.close)
        return EnrolmentAgent(self.bus, context, AgentLlm("enrolment", lambda llm: provider), clock=CLOCK)

    def run_request(self, agent, data, **kwargs):
        agent(request_inbound(TOPIC, data, **kwargs), self.settings)

    def events(self):
        return [next(iter(record["result"].items())) for _, record in produced(self.bus, "agent.task-events")]

    def states(self):
        return [carrier["artifact"]["name"] if kind == "artifactUpdate" else carrier["status"]["state"]
                for kind, carrier in self.events()]

    def decision(self):
        artifacts = [c["artifact"]["parts"][0]["data"] for k, c in self.events() if k == "artifactUpdate"]
        self.assertEqual(len(artifacts), 1)
        return artifacts[0]

    def commands(self):
        return [value for _, value in produced(self.bus, "device.policy-commands")]

    def step_ups(self):
        return [value["params"]["message"] for _, value in produced(self.bus, "agent.tasks.step-up")]

    def assert_decision(self, outcome, action, final, guardrails, *, command=None):
        self.assertEqual(self.states(), [SUBMITTED, WORKING, "enrolment-decision", final])
        data = self.decision()
        self.assertEqual((data["outcome"], data["policyAction"], data["guardrails"]), (outcome, action, guardrails))
        if command is None:
            self.assertEqual(self.commands(), [])
        else:
            (record,) = self.commands()
            self.assertEqual((record["action"], record["reason"]), command)

    def test_clean_grant_allows_and_completes(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(10)))
        self.assert_decision("GRANTED", "ALLOW", COMPLETED, [], command=("ALLOW", "GRANTED"))
        data = self.decision()
        self.assertEqual((data["riskScore"], data["asOf"]["contextSeq"]), (10, 7))
        self.assertEqual(data["rationale"], "Because.")
        (key, interaction), = produced(self.bus, "agent.llm-interactions")
        self.assertEqual((key, interaction["guardrail_result"], interaction["status"]), ("dev-0001", "PASSED", "OK"))
        self.assertEqual(self.step_ups(), [])
        self.assertEqual(self.events()[2][1]["artifact"]["parts"][1]["text"], "GRANTED: Because.")

    def test_outputs_are_emitted_in_order_with_the_final_status_last(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(10)))
        message_id = lambda i: ids.message_id("agent:enrolment", "router-0", "cfg-1", i)  # noqa: E731
        self.assertEqual([c["metadata"][ENVELOPE]["messageId"] for _, c in self.events()],
                         [message_id(0), message_id(1), message_id(4), message_id(5)])
        self.assertEqual(produced(self.bus, "agent.llm-interactions")[0][1]["envelope"]["message_id"], message_id(2))
        self.assertEqual(self.commands()[0]["envelope"]["message_id"], message_id(3))

    def test_prompt_and_facts_reach_the_model(self):
        provider = ScriptedProvider(decided("GRANT"))
        self.run_request(self.agent(provider, signals=[SIGNAL]), decide_data(assessment(10)))
        req, = provider.requests
        self.assertEqual(req.schema, DECISION_SCHEMA)
        self.assertEqual(req.messages[0], {"role": "system", "content": "Decide."})
        user = req.messages[1]["content"]
        for fragment in ('"device":{', '"device_id":"dev-0001"', '"riskAssessment":{"llmInteractionId":"llm-1"',
                         '"signalId":"sig-1"', '"signalType":"IMPOSSIBLE_TRAVEL"'):
            self.assertIn(fragment, user)

    def test_compromised_os_rejects_without_llm(self):
        provider = ScriptedProvider()
        signals = [{"device_id": "dev-0001", "signal_id": "sig-9", "signal_type": "COMPROMISED_OS"}]
        self.run_request(self.agent(provider, signals=signals), decide_data(assessment(10)))
        self.assertEqual(provider.calls, 0)
        self.assert_decision("DENIED", "BLOCK", REJECTED, ["COMPROMISED_OS"], command=("BLOCK", "DENIED"))
        self.assertEqual(produced(self.bus, "agent.llm-interactions"), [])

    def test_rooted_device_rejects_without_llm(self):
        provider = ScriptedProvider()
        devices = [{"device_id": "dev-0001", "trust_status": "PENDING", "rooted": True}]
        self.run_request(self.agent(provider, devices=devices), decide_data(assessment(10)))
        self.assertEqual(provider.calls, 0)
        self.assert_decision("DENIED", "BLOCK", REJECTED, ["COMPROMISED_OS"], command=("BLOCK", "DENIED"))

    def test_already_trusted_grants_without_llm(self):
        provider = ScriptedProvider()
        devices = [{"device_id": "dev-0001", "trust_status": "TRUSTED"}]
        self.run_request(self.agent(provider, devices=devices), decide_data(assessment(10)))
        self.assertEqual(provider.calls, 0)
        self.assert_decision("GRANTED", "ALLOW", COMPLETED, ["ALREADY_TRUSTED"], command=("ALLOW", "GRANTED"))

    def test_threshold_coerces_grant_to_challenge(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(60)))
        self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, ["RISK_THRESHOLD"])
        (step_up,) = self.step_ups()
        data = step_up["parts"][0]["data"]
        self.assertEqual(data["enrolmentTaskId"], TASK_ID)
        self.assertEqual(data["reason"], self.decision()["rationale"])
        self.assertEqual(data["signalIds"], [])
        self.assertEqual(step_up["referenceTaskIds"], [TASK_ID])
        (_, interaction), = produced(self.bus, "agent.llm-interactions")
        self.assertEqual(interaction["guardrail_result"], "RISK_THRESHOLD")
        self.assertEqual(self.events()[-1][1]["status"]["message"]["parts"][0]["text"], "Step-up challenge required")

    def test_step_up_is_handed_off_before_the_final_status(self):
        self.run_request(self.agent(ScriptedProvider(decided("CHALLENGE"))), decide_data(assessment(40, ["sig-1"])))
        self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, [])
        handoff = self.step_ups()[0]["metadata"][ENVELOPE]["messageId"]
        final = self.events()[-1][1]["metadata"][ENVELOPE]["messageId"]
        indexes = [ids.message_id("agent:enrolment", "router-0", "cfg-1", i) for i in range(6)]
        self.assertEqual((handoff, final), (indexes[4], indexes[5]))
        self.assertEqual(self.step_ups()[0]["parts"][0]["data"]["signalIds"], ["sig-1"])

    def test_signals_coerce_grant_to_challenge(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(10, ["sig-1"])))
        self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, ["UNRESOLVED_SIGNALS"])

    def test_missing_risk_assessment_challenges(self):
        for decision in ("GRANT", "GRANT_READ_ONLY"):
            with self.subTest(decision):
                self.bus = make_bus("enrolment")
                self.run_request(self.agent(ScriptedProvider(decided(decision))), decide_data())
                self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, ["NO_RISK_ASSESSMENT"])
                self.assertNotIn("riskScore", self.decision())

    def test_llm_invalid_output_challenges(self):
        provider = ScriptedProvider(LLMResponse(ERROR, None, None, 10, error="output is not JSON"))
        self.run_request(self.agent(provider), decide_data(assessment(10)))
        self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, ["LLM_OUTPUT_INVALID"])
        (_, interaction), = produced(self.bus, "agent.llm-interactions")
        self.assertEqual((interaction["status"], interaction["guardrail_result"]), ("ERROR", "LLM_OUTPUT_INVALID"))

    def test_first_firing_guardrail_is_the_only_one_recorded(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(90, ["sig-1"])))
        self.assertEqual(self.decision()["guardrails"], ["RISK_THRESHOLD"])

    def test_llm_deny_completes_with_block(self):
        self.run_request(self.agent(ScriptedProvider(decided("DENY"))), decide_data(assessment(95, ["sig-1"])))
        self.assert_decision("DENIED", "BLOCK", COMPLETED, [], command=("BLOCK", "DENIED"))

    def test_read_only(self):
        self.run_request(self.agent(ScriptedProvider(decided("GRANT_READ_ONLY"))), decide_data(assessment(70)))
        self.assert_decision("READ_ONLY", "READ_ONLY", COMPLETED, [], command=("READ_ONLY", "READ_ONLY"))

    def test_llm_challenge_is_kept(self):
        self.run_request(self.agent(ScriptedProvider(decided("CHALLENGE", "Moderate."))), decide_data(assessment(40)))
        self.assert_decision("CHALLENGED", "CHALLENGE", AUTH_REQUIRED, [])
        self.assertEqual(self.decision()["rationale"], "Moderate.")

    def test_threshold_from_settings(self):
        self.settings = settings("enrolment", prompt_text="Decide.", llm={"provider": "recorded", "model": "m"},
                                 guardrails={"challengeAtRiskScore": 80})
        self.run_request(self.agent(ScriptedProvider(decided("GRANT"))), decide_data(assessment(70)))
        self.assert_decision("GRANTED", "ALLOW", COMPLETED, [], command=("ALLOW", "GRANTED"))

    def test_default_threshold(self):
        self.assertEqual(DEFAULT_CHALLENGE_AT_RISK_SCORE, 60)

    def test_stale_context_fails_the_task_without_llm(self):
        provider = ScriptedProvider()
        data = decide_data(assessment(10))
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(self.agent(provider, devices=[]), data)
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])
        self.assertIn("device context not materialized", self.events()[-1][1]["status"]["message"]["parts"][0]["text"])
        self.assertEqual((provider.calls, self.commands(), self.step_ups()), (0, [], []))

    def test_remove_trust_blocks_unenrolled(self):
        provider = ScriptedProvider()
        self.run_request(self.agent(provider, devices=[]), {"taskType": "REMOVE_TRUST", "deviceId": "dev-0001"})
        self.assertEqual(provider.calls, 0)
        self.assert_decision("REMOVED", "BLOCK", COMPLETED, [], command=("BLOCK", "UNENROLLED"))
        self.assertEqual(self.decision()["rationale"], "Device unenrolled by request")
        self.assertEqual(self.events()[0][0], "task")

    def resolve(self, result, task_id="enrolment-task-1"):
        provider = ScriptedProvider()
        data = {"taskType": "RESOLVE_CHALLENGE", "deviceId": "dev-0001", "enrolmentTaskId": task_id,
                "challengeResult": result}
        self.run_request(self.agent(provider, devices=[]), data, task_id=task_id)
        self.assertEqual(provider.calls, 0)

    def test_resolve_passed_allows_on_existing_task(self):
        self.resolve("PASSED")
        self.assertEqual(self.states(), [WORKING, "enrolment-decision", COMPLETED])
        self.assertNotIn("task", [kind for kind, _ in self.events()])
        self.assertEqual({carrier["taskId"] for _, carrier in self.events()}, {"enrolment-task-1"})
        (record,) = self.commands()
        self.assertEqual((record["action"], record["reason"]), ("ALLOW", "CHALLENGE_PASSED"))
        data = self.decision()
        self.assertEqual((data["outcome"], data["policyAction"], data["rationale"], data["guardrails"]),
                         ("GRANTED", "ALLOW", "Step-up challenge passed", []))

    def test_resolve_failed_blocks(self):
        self.resolve("FAILED")
        self.assertEqual(self.states(), [WORKING, "enrolment-decision", COMPLETED])
        (record,) = self.commands()
        self.assertEqual((record["action"], record["reason"]), ("BLOCK", "CHALLENGE_FAILED"))
        data = self.decision()
        self.assertEqual((data["outcome"], data["policyAction"], data["rationale"]),
                         ("DENIED", "BLOCK", "Step-up challenge failed"))

    def test_resolve_without_task_id_fails(self):
        data = {"taskType": "RESOLVE_CHALLENGE", "deviceId": "dev-0001", "challengeResult": "PASSED"}
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(self.agent(ScriptedProvider()), data)
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])
        self.assertEqual({c["id"] if k == "task" else c["taskId"] for k, c in self.events()}, {TASK_ID})
        self.assertEqual(self.events()[-1][1]["status"]["message"]["parts"][0]["text"],
                         "ValueError: RESOLVE_CHALLENGE without taskId")
        self.assertEqual(self.commands(), [])

    def test_resolve_without_result_fails_the_existing_task(self):
        data = {"taskType": "RESOLVE_CHALLENGE", "deviceId": "dev-0001"}
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(self.agent(ScriptedProvider()), data, task_id="enrolment-task-1")
        self.assertEqual(self.states(), [WORKING, FAILED])
        self.assertEqual(self.commands(), [])

    def test_failed_policy_command_leaves_failed_as_the_only_terminal_state(self):
        agent = self.agent(ScriptedProvider(decided("GRANT")))
        original = self.bus.produce_avro

        def produce(topic, *args, **kwargs):
            if topic == "device.policy-commands":
                raise RuntimeError("producer down")
            return original(topic, *args, **kwargs)
        self.bus.produce_avro = produce
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(agent, decide_data(assessment(10)))
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])

    def test_invalid_request_is_skipped(self):
        agent = self.agent(ScriptedProvider())
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            agent(request_inbound(TOPIC, {"deviceId": "dev-0001"}), self.settings)
        self.assertEqual(self.events(), [])

    def test_card_describes_enrolment(self):
        description, skills = self.agent(ScriptedProvider()).describe(self.settings)
        card = a2a.agent_card("enrolment", description, skills, "cfg-1", "kafka:9092", EnrolmentAgent.request_topic)
        a2a.validate("agent.cards", card)
        self.assertEqual([s["id"] for s in skills], ["decide-enrolment", "remove-trust", "resolve-challenge"])

    def test_agent_identity(self):
        agent = self.agent(ScriptedProvider())
        self.assertEqual((agent.name, agent.topics, agent.request_topic), ("enrolment", None, a2a.SELF_TASK_TOPIC))

    def test_prompt_is_packaged(self):
        text = (importlib.resources.files("dtm") / "prompts" / "enrolment.v1").read_text(encoding="utf-8")
        for fragment in ("GRANT_READ_ONLY", "CHALLENGE", "DENY", "decision", "rationale"):
            self.assertIn(fragment, text)

    def test_serve_entry(self):
        from dtm.mesh import enrolment
        self.assertEqual(enrolment.EnrolmentAgent.name, "EnrolmentAgent")


if __name__ == "__main__":
    unittest.main()
