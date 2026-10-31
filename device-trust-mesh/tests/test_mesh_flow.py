"""In-process end-to-end flows: the agents chained through the records they produce, with no Kafka."""
import json
import unittest

from agent_fakes import (ROUTER_ENVELOPE, ScriptedProvider, context_table, make_bus, produced, pump,
                         request_inbound, settings)
from dtm.mesh.agents.compliance import ComplianceAgent
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.enrolment import EnrolmentAgent
from dtm.mesh.agents.kit import AUTH_REQUIRED, COMPLETED, SUBMITTED, WORKING
from dtm.mesh.agents.reasoning import AgentLlm
from dtm.mesh.agents.risk import RiskAgent
from dtm.mesh.agents.step_up import StepUpAgent
from dtm.mesh.context.server import build_mcp
from dtm.mesh.llm import OK, LLMResponse

CLOCK = lambda: 1793323940.5  # noqa: E731
ENVELOPE = "urn:device-trust-mesh:envelope:v1"
DEVICES = [{"device_id": "dev-0001", "trust_status": "PENDING"}]
CUSTOMERS = [{"customer_id": "cust-001"}]
SIGNALS = [{"device_id": "dev-0001", "signal_id": "sig-1", "signal_type": "IMPOSSIBLE_TRAVEL",
            "severity": "HIGH", "detail": "login from AU 10 minutes after US"}]
RISK_REQUEST = {"deviceId": "dev-0001", "customerId": "cust-001", "requiredContextSeq": 7,
                "fingerprintSummary": "Pixel 8, Android 14, rooted=false"}


def answer(content: dict) -> LLMResponse:
    return LLMResponse(OK, content, content, 120, 50, 20)


class MeshFlowTest(unittest.TestCase):
    def setUp(self):
        table = context_table(devices=DEVICES, customers=CUSTOMERS, signals=SIGNALS)
        context = SyncContext(build_mcp(table, wait_timeout_ms=50), timeout_s=5.0)
        self.addCleanup(context.close)
        self.buses = {name: make_bus(name) for name in ("risk", "enrolment", "step-up", "compliance")}
        self.settings = {
            "risk": settings("risk", prompt_ref="risk.v1", prompt_text="Assess the risk.",
                             llm={"provider": "recorded", "model": "m"}),
            "enrolment": settings("enrolment", prompt_ref="enrolment.v1", prompt_text="Decide.",
                                  llm={"provider": "recorded", "model": "m"}),
            "step-up": settings("step-up"),
            "compliance": settings("compliance"),
        }
        self.risk_llm = ScriptedProvider(answer({"score": 70, "rationale": "Impossible travel."}))
        self.enrolment_llm = ScriptedProvider(answer({"decision": "GRANT", "rationale": "Looks fine."}))
        self.agents = {
            "risk": RiskAgent(self.buses["risk"], context, AgentLlm("risk", lambda llm: self.risk_llm), CLOCK),
            "enrolment": EnrolmentAgent(self.buses["enrolment"], context,
                                        AgentLlm("enrolment", lambda llm: self.enrolment_llm), CLOCK),
            "step-up": StepUpAgent(self.buses["step-up"], context, clock=CLOCK),
            "compliance": ComplianceAgent(self.buses["compliance"], clock=CLOCK),
        }

    def run_agent(self, name, inbound):
        self.agents[name](inbound, self.settings[name])

    def deliver(self, src, topic, dst, *, skip=0):
        return pump(self.buses[src], topic, self.agents[dst], self.settings[dst], skip=skip)

    def audit(self, src, skip=0):
        """Deliver ``src``'s task events to compliance, from record ``skip``; returns the new cursor."""
        return self.deliver(src, "agent.task-events", "compliance", skip=skip)

    def events(self, name):
        return [next(iter(value["result"].items())) for _, value in produced(self.buses[name], "agent.task-events")]

    def audits(self):
        return [(a["subject_agent"], a["artifact_name"], a["outcome"])
                for _, a in produced(self.buses["compliance"], "audit.log")]

    def commands(self, *names):
        return [(a["action"], a["reason"]) for name in names
                for _, a in produced(self.buses[name], "device.policy-commands")]

    def a2a_records(self):
        """Every A2A record produced by any agent, as ``(contextId, envelope)``."""
        out = []
        for bus in self.buses.values():
            for r in bus._producer.records:
                if not r.topic.startswith("agent.task"):
                    continue
                value = json.loads(r.value)
                if "method" in value:
                    message = value["params"]["message"]
                    out.append((message["contextId"], message["metadata"][ENVELOPE]))
                else:
                    carrier = next(iter(value["result"].values()))
                    out.append((carrier["contextId"], carrier["metadata"][ENVELOPE]))
        return out

    def avro_records(self):
        return [value for bus in self.buses.values() for topic in ("device.policy-commands", "audit.log",
                                                                    "agent.llm-interactions")
                for _, value in produced(bus, topic)]

    def test_challenge_flow_end_to_end(self):
        risk_request = request_inbound("agent.tasks.risk", RISK_REQUEST, offset=0)
        self.run_agent("risk", risk_request)
        self.audit("risk")

        self.deliver("risk", "agent.tasks.enrolment", "enrolment")
        enrolment_cursor = self.audit("enrolment")
        self.assertEqual(self.enrolment_llm.calls, 1)

        self.deliver("enrolment", "agent.tasks.step-up", "step-up")
        self.audit("step-up")

        enrolment_task_id = self.events("enrolment")[0][1]["id"]
        resolve = request_inbound(
            "agent.tasks.enrolment",
            {"taskType": "RESOLVE_CHALLENGE", "deviceId": "dev-0001", "customerId": "cust-001",
             "enrolmentTaskId": enrolment_task_id, "challengeResult": "PASSED"},
            task_id=enrolment_task_id, offset=1)
        self.run_agent("enrolment", resolve)
        self.audit("enrolment", skip=enrolment_cursor)

        # one conversation: every A2A record's contextId and every Avro record's correlation_id
        context_ids = {context_id for context_id, _ in self.a2a_records()}
        context_ids |= {record["envelope"]["correlation_id"] for record in self.avro_records()}
        self.assertEqual(context_ids, {ROUTER_ENVELOPE.correlation_id})

        states = [carrier["artifact"]["name"] if kind == "artifactUpdate" else carrier["status"]["state"]
                  for kind, carrier in self.events("enrolment")]
        enrolment_states = [s for s in states if s.startswith("TASK_STATE")]
        self.assertEqual(enrolment_states, [SUBMITTED, WORKING, AUTH_REQUIRED, WORKING, COMPLETED])
        self.assertEqual({carrier.get("taskId", carrier.get("id")) for _, carrier in self.events("enrolment")},
                         {enrolment_task_id})

        self.assertEqual(self.commands("step-up", "enrolment"),
                         [("CHALLENGE", "Guardrail RISK_THRESHOLD turned GRANT into CHALLENGE: Looks fine."),
                          ("ALLOW", "CHALLENGE_PASSED")])

        self.assertEqual(self.audits(), [("risk", "risk-assessment", None),
                                         ("enrolment", "enrolment-decision", "CHALLENGED"),
                                         ("step-up", "step-up-challenge", "BIOMETRIC"),
                                         ("enrolment", "enrolment-decision", "GRANTED")])

        # each hand-off's causationId is the messageId of the request that produced it
        (_, risk_handoff), = produced(self.buses["risk"], "agent.tasks.enrolment")
        (_, step_up_handoff), = produced(self.buses["enrolment"], "agent.tasks.step-up")
        risk_handoff, step_up_handoff = (m["params"]["message"] for m in (risk_handoff, step_up_handoff))
        self.assertEqual(risk_handoff["metadata"][ENVELOPE]["causationId"], "router-0")
        self.assertEqual(step_up_handoff["metadata"][ENVELOPE]["causationId"], risk_handoff["messageId"])
        step_up_task_event = self.events("step-up")[0][1]
        self.assertEqual(step_up_task_event["metadata"][ENVELOPE]["causationId"], step_up_handoff["messageId"])

    def test_unenrol_flow_end_to_end(self):
        unenrol = request_inbound("agent.tasks.enrolment",
                                  {"taskType": "REMOVE_TRUST", "deviceId": "dev-0001", "customerId": "cust-001"})
        self.run_agent("enrolment", unenrol)
        self.audit("enrolment")

        self.assertEqual(self.enrolment_llm.calls, 0)
        self.assertEqual(self.commands("enrolment"), [("BLOCK", "UNENROLLED")])
        self.assertEqual(self.audits(), [("enrolment", "enrolment-decision", "REMOVED")])


if __name__ == "__main__":
    unittest.main()
