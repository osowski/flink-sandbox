import json
import unittest

from dtm.common.a2a import (A2AValidationError, CARDS_TOPIC, KAFKA_BINDING, REPLY_TOPIC, agent_card,
                            artifact_update, dumps, now_iso, parts, send_message_request, status_update,
                            task_event, task_topic, validate)
from dtm.common.envelope import ENVELOPE_URI, Envelope
from dtm.common.schemas import a2a_validator

ENV = Envelope("m-1", "c-1", None, "agent:risk", "dev", "abc123",
               1793323937770, "dev-0001", "cust-001", 5, 5)

VALID_RISK = {"deviceId": "dev-0001", "customerId": "cust-001", "requiredContextSeq": 5,
              "fingerprintSummary": "IOS 18.1, rooted=false"}
VALID_ASSESSMENT = {"score": 12, "rationale": "clean device", "signalIds": ["s-1"],
                    "asOf": {"contextSeq": 5, "materializedAt": 1793323937000,
                             "sourceOffsets": [{"topic": "device.events", "partition": 0, "offset": 41}]}}
TS = "2026-10-31T00:00:00.000Z"


class ConstantsTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(KAFKA_BINDING, "urn:device-trust-mesh:kafka:v1")
        self.assertEqual(REPLY_TOPIC, "agent.task-events")
        self.assertEqual(CARDS_TOPIC, "agent.cards")
        self.assertEqual(task_topic("risk"), "agent.tasks.risk")

    def test_parts_data_then_text(self):
        self.assertEqual(parts({"a": 1}, "hi"),
                         [{"data": {"a": 1}, "mediaType": "application/json"},
                          {"text": "hi", "mediaType": "text/plain"}])

    def test_now_iso_is_rfc3339_millis_utc(self):
        self.assertEqual(now_iso(1793323937770), "2026-10-30T01:32:17.770Z")
        self.assertEqual(now_iso(1793323937000), "2026-10-30T01:32:17.000Z")


class RequestTest(unittest.TestCase):
    def test_send_message_request_validates_for_risk(self):
        r = send_message_request(ENV, VALID_RISK, "Assess ...")
        validate("agent.tasks.risk", r)
        self.assertEqual(r["id"], ENV.message_id)
        self.assertEqual(r["params"]["message"]["messageId"], ENV.message_id)
        self.assertEqual(r["params"]["message"]["contextId"], ENV.correlation_id)
        self.assertEqual(r["params"]["message"]["role"], "ROLE_USER")
        self.assertEqual(r["params"]["message"]["extensions"], [ENVELOPE_URI])
        self.assertEqual(r["params"]["message"]["metadata"], {ENVELOPE_URI: ENV.to_a2a()})
        self.assertEqual(r["params"]["message"]["parts"][0]["mediaType"], "application/json")

    def test_task_and_reference_ids_are_optional(self):
        r = send_message_request(ENV, VALID_RISK, "x")
        self.assertNotIn("taskId", r["params"]["message"])
        self.assertNotIn("referenceTaskIds", r["params"]["message"])
        r = send_message_request(ENV, VALID_RISK, "x", task_id="t-9", reference_task_ids=["t-1"])
        self.assertEqual(r["params"]["message"]["taskId"], "t-9")
        self.assertEqual(r["params"]["message"]["referenceTaskIds"], ["t-1"])
        validate("agent.tasks.risk", r)

    def test_missing_required_payload_field_rejected(self):
        r = send_message_request(ENV, {"deviceId": "dev-0001"}, "x")
        with self.assertRaises(A2AValidationError) as ctx:
            validate("agent.tasks.risk", r)
        self.assertIn("agent.tasks.risk", str(ctx.exception))

    def test_unknown_member_rejected_by_sdk(self):
        r = send_message_request(ENV, VALID_RISK, "x")
        r["params"]["message"]["bogus"] = 1
        with self.assertRaises(A2AValidationError):
            validate("agent.tasks.risk", r)

    def test_sdk_rejection_when_jsonschema_allows(self):
        r = send_message_request(ENV, VALID_RISK, "x")
        r["params"]["message"]["parts"][0]["text"] = "both data and text"
        validator = a2a_validator("agent.tasks.risk")
        self.assertTrue(validator.is_valid(r))
        with self.assertRaises(A2AValidationError) as ctx:
            validate("agent.tasks.risk", r)
        self.assertIn("agent.tasks.risk", str(ctx.exception))
        self.assertIn("a2a-sdk", str(ctx.exception))

    def test_validate_does_not_mutate_the_record(self):
        r = send_message_request(ENV, VALID_RISK, "x")
        before = json.dumps(r, sort_keys=True)
        validate("agent.tasks.risk", r)
        self.assertEqual(json.dumps(r, sort_keys=True), before)


class EventTest(unittest.TestCase):
    def test_task_events_shapes_validate(self):
        for rec in (task_event("m-1", "t-1", "c-1", "TASK_STATE_SUBMITTED", ENV, TS),
                    status_update("m-1", "t-1", "c-1", "TASK_STATE_WORKING", ENV, TS, "working"),
                    status_update("m-1", "t-1", "c-1", "TASK_STATE_WORKING", ENV, TS),
                    artifact_update("m-1", "t-1", "c-1", "a-1", "risk-assessment",
                                    VALID_ASSESSMENT, "score 12", ENV)):
            validate("agent.task-events", rec)

    def test_event_shapes(self):
        t = task_event("m-1", "t-1", "c-1", "TASK_STATE_SUBMITTED", ENV, TS)
        self.assertEqual(t["id"], "m-1")
        self.assertEqual(t["result"]["task"]["id"], "t-1")
        self.assertEqual(t["result"]["task"]["status"], {"state": "TASK_STATE_SUBMITTED", "timestamp": TS})
        self.assertEqual(t["result"]["task"]["metadata"], {ENVELOPE_URI: ENV.to_a2a()})
        s = status_update("m-1", "t-1", "c-1", "TASK_STATE_WORKING", ENV, TS, "working")
        msg = s["result"]["statusUpdate"]["status"]["message"]
        self.assertEqual(msg["role"], "ROLE_AGENT")
        self.assertEqual(msg["parts"], [{"text": "working", "mediaType": "text/plain"}])
        bare = status_update("m-1", "t-1", "c-1", "TASK_STATE_WORKING", ENV, TS)
        self.assertNotIn("message", bare["result"]["statusUpdate"]["status"])
        a = artifact_update("m-1", "t-1", "c-1", "a-1", "risk-assessment", VALID_ASSESSMENT, "x", ENV)
        art = a["result"]["artifactUpdate"]["artifact"]
        self.assertEqual((art["artifactId"], art["name"]), ("a-1", "risk-assessment"))
        self.assertEqual(art["parts"], parts(VALID_ASSESSMENT, "x"))

    def test_artifact_payload_selected_by_name(self):
        rec = artifact_update("m-1", "t-1", "c-1", "a-1", "enrolment-decision", VALID_ASSESSMENT, "x", ENV)
        with self.assertRaises(A2AValidationError):
            validate("agent.task-events", rec)

    def test_unknown_state_rejected(self):
        rec = task_event("m-1", "t-1", "c-1", "TASK_STATE_BOGUS", ENV, TS)
        with self.assertRaises(A2AValidationError):
            validate("agent.task-events", rec)


class CardTest(unittest.TestCase):
    SKILLS = [{"id": "assess-enrolment", "name": "Assess enrolment", "description": "...", "tags": ["risk"]}]

    def test_agent_card_validates(self):
        validate("agent.cards", agent_card("risk", "Scores enrolment risk", self.SKILLS,
                                           "abc123", "kafka.kafka.svc.cluster.local:9071"))

    def test_agent_card_shape(self):
        c = agent_card("risk", "Scores enrolment risk", self.SKILLS, "abc123", "b:9071")
        self.assertEqual(c["supportedInterfaces"][0],
                         {"url": "kafka://b:9071/agent.tasks.risk", "protocolBinding": KAFKA_BINDING,
                          "protocolVersion": "1.0"})
        self.assertEqual(c["capabilities"]["extensions"], [{"uri": ENVELOPE_URI}])
        self.assertEqual(c["defaultInputModes"], ["application/json", "text/plain"])
        self.assertEqual(c["defaultOutputModes"], ["application/json", "text/plain"])
        self.assertEqual((c["name"], c["version"], c["skills"]), ("risk", "abc123", self.SKILLS))

    def test_card_missing_required_rejected(self):
        c = agent_card("risk", "d", self.SKILLS, "v", "b:1")
        del c["skills"]
        with self.assertRaises(A2AValidationError):
            validate("agent.cards", c)


class DumpsTest(unittest.TestCase):
    def test_dumps_keeps_integers(self):
        self.assertIn(b'"eventTime":1793323937770', dumps(send_message_request(ENV, VALID_RISK, "x")))

    def test_dumps_is_compact_utf8(self):
        out = dumps({"a": "é", "b": [1, 2]})
        self.assertEqual(out, '{"a":"é","b":[1,2]}'.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
