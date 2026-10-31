import importlib.resources
import unittest

from agent_fakes import RISK_REQUEST, ScriptedProvider, context_table, make_bus, produced, request_inbound, settings
from dtm.common import a2a, ids
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.kit import COMPLETED, FAILED, SUBMITTED, WORKING
from dtm.mesh.agents.reasoning import AgentLlm
from dtm.mesh.agents.risk import RISK_SCHEMA, RiskAgent
from dtm.mesh.context.server import build_mcp
from dtm.mesh.llm import OK, LLMResponse

CLOCK = lambda: 1793323940.5  # noqa: E731
SCORED = {"score": 72, "rationale": "Impossible travel."}
DEVICES = [{"device_id": "dev-0001"}]
CUSTOMERS = [{"customer_id": "cust-001"}]
SIGNALS = [{"device_id": "dev-0001", "signal_id": "sig-1", "signal_type": "IMPOSSIBLE_TRAVEL",
            "severity": "HIGH", "detail": "login from DE 10 minutes after US"}]
ENVELOPE = "urn:device-trust-mesh:envelope:v1"


def scored(content=SCORED) -> LLMResponse:
    return LLMResponse(OK, content, content, 120, 50, 20)


class RiskAgentTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("risk")
        self.settings = settings("risk", prompt_ref="risk.v1", prompt_text="Assess the risk.",
                                 llm={"provider": "recorded", "model": "m"})

    def agent(self, provider, devices=DEVICES):
        table = context_table(devices=devices, customers=CUSTOMERS, signals=SIGNALS)
        context = SyncContext(build_mcp(table, wait_timeout_ms=50), timeout_s=5.0)
        self.addCleanup(context.close)
        return RiskAgent(self.bus, context, AgentLlm("risk", lambda llm: provider), clock=CLOCK)

    def run_request(self, agent, data=RISK_REQUEST):
        agent(request_inbound("agent.tasks.risk", data), self.settings)

    def events(self):
        return [next(iter(record["result"].items())) for _, record in produced(self.bus, "agent.task-events")]

    def states(self):
        return [carrier["artifact"]["name"] if kind == "artifactUpdate" else carrier["status"]["state"]
                for kind, carrier in self.events()]

    def test_happy_path_emits_lifecycle_artifact_and_handoff(self):
        self.run_request(self.agent(ScriptedProvider(scored())))
        self.assertEqual(self.states(), [SUBMITTED, WORKING, "risk-assessment", COMPLETED])
        data = self.events()[2][1]["artifact"]["parts"][0]["data"]
        self.assertEqual((data["score"], data["signalIds"], data["asOf"]["contextSeq"]), (72, ["sig-1"], 7))
        self.assertTrue(data["asOf"]["sourceOffsets"])
        self.assertIsInstance(data["asOf"]["materializedAt"], int)
        interactions = produced(self.bus, "agent.llm-interactions")
        self.assertEqual([i["status"] for _, i in interactions], ["OK"])
        handoffs = produced(self.bus, "agent.tasks.enrolment")
        self.assertEqual([key for key, _ in handoffs], ["dev-0001"])
        message = handoffs[0][1]["params"]["message"]
        handoff = message["parts"][0]["data"]
        self.assertEqual(handoff["taskType"], "DECIDE_ENROLMENT")
        self.assertEqual((handoff["deviceId"], handoff["customerId"]), ("dev-0001", "cust-001"))
        self.assertEqual(handoff["riskAssessment"],
                         {"score": 72, "rationale": "Impossible travel.", "signalIds": ["sig-1"],
                          "llmInteractionId": interactions[0][1]["envelope"]["message_id"]})
        self.assertEqual(message["referenceTaskIds"], [ids.task_id("agent:risk", "router-0")])
        self.assertEqual(message["parts"][1]["text"], "Decide enrolment for dev-0001 (risk 72)")

    def test_every_output_is_caused_by_the_inbound_message(self):
        self.run_request(self.agent(ScriptedProvider(scored())))
        causes = [carrier["metadata"][ENVELOPE]["causationId"] for _, carrier in self.events()]
        handoff = produced(self.bus, "agent.tasks.enrolment")[0][1]["params"]["message"]
        causes.append(handoff["metadata"][ENVELOPE]["causationId"])
        causes.append(produced(self.bus, "agent.llm-interactions")[0][1]["envelope"]["causation_id"])
        self.assertEqual(causes, ["router-0"] * 6)

    def test_prompt_and_facts_reach_the_model(self):
        provider = ScriptedProvider(scored())
        self.run_request(self.agent(provider))
        req, = provider.requests
        self.assertEqual(req.schema, RISK_SCHEMA)
        self.assertEqual(req.messages[0], {"role": "system", "content": "Assess the risk."})
        user = req.messages[1]["content"]
        for fragment in ('"signalId":"sig-1"', '"signalType":"IMPOSSIBLE_TRAVEL"', '"severity":"HIGH"',
                         '"fingerprintSummary":"Pixel 8, Android 14, rooted=false"', '"device_id":"dev-0001"',
                         '"customer":{'):
            self.assertIn(fragment, user)

    def test_llm_error_fails_task_without_handoff(self):
        provider = ScriptedProvider(LLMResponse("TIMEOUT", None, None, 30000, error="timed out"))
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(self.agent(provider))
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])
        self.assertEqual(self.events()[-1][1]["status"]["message"]["parts"][0]["text"],
                         "RuntimeError: risk model failed: TIMEOUT timed out")
        self.assertEqual([i["status"] for _, i in produced(self.bus, "agent.llm-interactions")], ["TIMEOUT"])
        self.assertEqual(produced(self.bus, "agent.tasks.enrolment"), [])

    def test_context_error_fails_task(self):
        provider = ScriptedProvider()
        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            self.run_request(self.agent(provider, devices=[]))
        self.assertEqual(self.states(), [SUBMITTED, WORKING, FAILED])
        self.assertIn("device context not materialized", self.events()[-1][1]["status"]["message"]["parts"][0]["text"])
        self.assertEqual(provider.calls, 0)
        self.assertEqual(produced(self.bus, "agent.tasks.enrolment"), [])

    def test_invalid_request_is_skipped(self):
        agent = self.agent(ScriptedProvider())
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            agent(request_inbound("agent.tasks.risk", {"deviceId": "dev-0001"}), self.settings)
        self.assertEqual(self.events(), [])

    def test_card_describes_risk(self):
        description, skills = self.agent(ScriptedProvider()).describe(self.settings)
        card = a2a.agent_card("risk", description, skills, "cfg-1", "kafka:9092", RiskAgent.request_topic)
        a2a.validate("agent.cards", card)
        self.assertEqual([s["id"] for s in skills], ["assess-enrolment"])

    def test_agent_identity(self):
        agent = self.agent(ScriptedProvider())
        self.assertEqual((agent.name, agent.topics, agent.request_topic), ("risk", None, a2a.SELF_TASK_TOPIC))

    def test_prompt_is_packaged(self):
        text = (importlib.resources.files("dtm") / "prompts" / "risk.v1").read_text(encoding="utf-8")
        self.assertTrue(text.strip())

    def test_serve_entry(self):
        from dtm.mesh import risk
        self.assertEqual(risk.RiskAgent.name, "RiskAgent")


if __name__ == "__main__":
    unittest.main()
