import json
import unittest

import fastavro

from agent_fakes import RISK_REQUEST, make_bus, produced, request_inbound, settings
from dtm.common import ids
from dtm.common.a2a import agent_card, validate
from dtm.common.bus import Inbound
from dtm.mesh.agents.compliance import ComplianceAgent
from dtm.mesh.agents.kit import COMPLETED, Outputs, parse_request

CLOCK = lambda: 1793323940.5  # noqa: E731
AUDIT_SCHEMA = "dtm/schemas/avro/audit.log-value.avsc"
ASSESSMENT = {"score": 70, "rationale": "Impossible travel", "signalIds": ["sig-1"],
              "asOf": {"contextSeq": 7, "materializedAt": 1, "sourceOffsets": []}}
GRANT = {"outcome": "GRANTED", "policyAction": "ALLOW", "rationale": "Low risk", "guardrails": []}
CHALLENGE = {"outcome": "CHALLENGED", "policyAction": "CHALLENGE", "rationale": "Risk 70", "guardrails": []}
REQUESTS = {"risk": RISK_REQUEST,
            "enrolment": {"taskType": "DECIDE_ENROLMENT", "deviceId": "dev-0001"},
            "step-up": {"enrolmentTaskId": "enrolment-task-1", "reason": "Risk 70"}}
STEP_UP = {"method": "MFA_PUSH", "enrolmentTaskId": "enrolment-task-1"}


def observed(agent, name=None, data=None, *, offset=0, final=True):
    """Return the task-events records ``agent`` emits for one request, as Inbounds from ``agent.task-events``.

    A task with an artifact is submitted, working, the artifact, then the final status.
    """
    bus = make_bus(agent)
    request = parse_request(bus, request_inbound(f"agent.tasks.{agent}", REQUESTS[agent], offset=offset))
    outputs = Outputs(bus, agent, settings(agent), request, clock=CLOCK)
    task_id = outputs.new_task_id()
    outputs.submitted(task_id)
    outputs.status(task_id, "TASK_STATE_WORKING")
    if name is not None:
        outputs.artifact(task_id, name, data, "text")
    if final:
        outputs.status(task_id, COMPLETED)
    return [inbound("agent.task-events", value) for _, value in produced(bus, "agent.task-events")]


def inbound(topic, value, offset=0):
    return Inbound(topic, 0, offset, "dev-0001", {}, json.loads(json.dumps(value)), 0)


class ComplianceAgentTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("compliance")
        self.settings = settings("compliance")
        self.agent = ComplianceAgent(self.bus, clock=CLOCK)

    def feed(self, records, agent=None):
        for record in records:
            (agent or self.agent)(record, self.settings)

    def audits(self, bus=None):
        return [value for _, value in produced(bus or self.bus, "audit.log")]

    def events(self, bus=None):
        return [next(iter(record["result"].items())) for _, record in produced(bus or self.bus, "agent.task-events")]

    def artifact_event(self, agent="enrolment", name="enrolment-decision", data=GRANT, offset=0):
        (event,) = [r for r in observed(agent, name, data, offset=offset)
                    if "artifactUpdate" in r.value["result"]]
        return event

    def test_audits_enrolment_decision(self):
        event = self.artifact_event()
        observed_id = event.value["result"]["artifactUpdate"]["artifact"]["artifactId"]
        self.feed([event])
        (audit,) = self.audits()
        self.assertEqual({k: v for k, v in audit.items() if k != "envelope"},
                         {"audit_id": audit["envelope"]["message_id"],
                          "subject_task_id": event.value["result"]["artifactUpdate"]["taskId"],
                          "subject_agent": "enrolment", "artifact_name": "enrolment-decision",
                          "outcome": "GRANTED", "policy_action": "ALLOW", "rationale": "Low risk",
                          "recorded_at": audit["envelope"]["event_time"]})
        self.assertEqual(audit["envelope"]["agent_id"], "agent:compliance")
        self.assertEqual(audit["envelope"]["causation_id"], observed_id)
        self.assertEqual(audit["recorded_at"], 1793323940500)
        self.assertEqual({k for k, _ in produced(self.bus, "audit.log")}, {"dev-0001"})

        events = self.events()
        self.assertEqual([k for k, _ in events], ["artifactUpdate", "statusUpdate"])
        artifact = events[0][1]["artifact"]
        self.assertEqual(artifact["name"], "audit-record")
        self.assertEqual(artifact["parts"][0]["data"],
                         {"auditId": audit["audit_id"], "subjectTaskId": audit["subject_task_id"]})
        self.assertEqual(artifact["parts"][1]["text"], "Audited enrolment-decision for dev-0001")
        self.assertEqual(events[1][1]["status"]["state"], COMPLETED)
        self.assertEqual({k for k, _ in produced(self.bus, "agent.task-events")}, {"dev-0001"})
        task_id = ids.task_id("agent:compliance", observed_id)
        self.assertEqual({carrier["taskId"] for _, carrier in events}, {task_id})

    def test_audits_risk_and_step_up_artifacts(self):
        self.feed([self.artifact_event("risk", "risk-assessment", ASSESSMENT),
                   self.artifact_event("step-up", "step-up-challenge", STEP_UP, offset=1),
                   self.artifact_event("enrolment", "enrolment-decision", CHALLENGE, offset=2)])
        risk, step_up, challenge = self.audits()
        self.assertEqual((risk["subject_agent"], risk["artifact_name"], risk["outcome"], risk["policy_action"],
                          risk["rationale"]),
                         ("risk", "risk-assessment", None, None, "Impossible travel"))
        self.assertEqual((step_up["subject_agent"], step_up["artifact_name"], step_up["outcome"],
                          step_up["policy_action"], step_up["rationale"]),
                         ("step-up", "step-up-challenge", "MFA_PUSH", None, None))
        self.assertEqual((challenge["outcome"], challenge["policy_action"]), ("CHALLENGED", "CHALLENGE"))
        self.assertEqual(len({a["audit_id"] for a in self.audits()}), 3)
        self.assertEqual([carrier["artifact"]["name"] for kind, carrier in self.events() if kind == "artifactUpdate"],
                         ["audit-record"] * 3)

    def test_audit_log_value_matches_the_avro_schema(self):
        self.feed([self.artifact_event("risk", "risk-assessment", ASSESSMENT)])
        with open(AUDIT_SCHEMA) as f:
            schema = fastavro.parse_schema(json.load(f))
        for audit in self.audits():
            self.assertTrue(fastavro.validate(audit, schema))

    def test_each_reply_uses_its_own_message_id_as_the_rpc_id(self):
        self.feed([self.artifact_event()])
        for _, record in produced(self.bus, "agent.task-events"):
            carrier = next(iter(record["result"].values()))
            self.assertEqual(record["id"], carrier["metadata"]["urn:device-trust-mesh:envelope:v1"]["messageId"])

    def test_ignores_status_and_task_events(self):
        self.feed(observed("enrolment"))
        self.assertEqual(self.audits(), [])
        self.assertEqual(produced(self.bus, "agent.task-events"), [])

    def test_ignores_own_events(self):
        self.feed([self.artifact_event()])
        other_bus = make_bus("compliance")
        other = ComplianceAgent(other_bus, clock=CLOCK)
        self.feed([inbound("agent.task-events", value) for _, value in produced(self.bus, "agent.task-events")],
                  agent=other)
        self.assertEqual(self.audits(other_bus), [])
        self.assertEqual(produced(other_bus, "agent.task-events"), [])

    def test_dedupes_on_message_id(self):
        event = self.artifact_event()
        self.feed([event, event])
        self.assertEqual(len(self.audits()), 1)
        self.assertEqual(len(self.events()), 2)

    def test_dedupe_remembers_the_most_recent_seen_limit_ids(self):
        agent = ComplianceAgent(self.bus, clock=CLOCK, seen_limit=2)
        first, second, third = (self.artifact_event(offset=i) for i in range(3))
        self.feed([first, second, third, second, first], agent=agent)
        self.assertEqual(len(self.audits()), 4)

    def test_redelivery_after_restart_reproduces_ids(self):
        event = self.artifact_event()
        self.feed([event])
        restarted_bus = make_bus("compliance")
        self.feed([event], agent=ComplianceAgent(restarted_bus, clock=CLOCK))
        self.assertEqual(self.audits(restarted_bus)[0]["audit_id"], self.audits()[0]["audit_id"])
        self.assertEqual(produced(restarted_bus, "agent.task-events"), produced(self.bus, "agent.task-events"))

    def test_invalid_record_is_skipped(self):
        bad = Inbound("agent.task-events", 0, 0, None, {}, {"jsonrpc": "2.0", "id": "x"}, 0)
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.agent(bad, self.settings)
        self.assertEqual(self.audits(), [])

    def test_card_has_no_interfaces(self):
        self.assertEqual((self.agent.name, self.agent.topics, self.agent.request_topic),
                         ("compliance", ["agent.task-events"], None))
        description, skills = self.agent.describe(self.settings)
        card = agent_card(self.agent.name, description, skills, "cfg-1", "broker:9092",
                          request_topic=self.agent.request_topic)
        self.assertEqual(card["supportedInterfaces"], [])
        validate("agent.cards", card)

    def test_serve_entry(self):
        from dtm.mesh import compliance
        self.assertEqual(compliance.ComplianceAgent.name, "ComplianceAgent")
        agent = compliance.make(self.bus, None, None)
        self.assertEqual(agent.name, "compliance")


if __name__ == "__main__":
    unittest.main()
