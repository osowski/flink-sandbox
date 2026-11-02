import json
import unittest

from prometheus_client import REGISTRY

from agent_fakes import RISK_REQUEST, make_bus, produced, request_inbound, settings
from dtm.common import ids
from dtm.common.a2a import artifact_update, now_iso
from dtm.common.bus import Inbound
from dtm.common.envelope import Envelope
from dtm.mesh.agents.kit import (AGENT_VERSION, FAILED, Outputs, SUBMITTED, WORKING, parse_request,
                                 parse_task_event, run_task)
from dtm.mesh.llm import OK, LLMRequest, LLMResponse

CLOCK = lambda: 1793323940.5  # noqa: E731
ENROLMENT_REQUEST = {"taskType": "DECIDE_ENROLMENT", "deviceId": "dev-0001"}
ASSESSMENT = {"score": 12, "rationale": "r", "signalIds": [],
              "asOf": {"contextSeq": 7, "materializedAt": 1, "sourceOffsets": []}}


def invalid_count(agent):
    return REGISTRY.get_sample_value("dtm_invalid_requests_total", {"agent": agent}) or 0.0


class KitTest(unittest.TestCase):
    def setUp(self):
        self.bus = make_bus("risk")
        self.settings = settings("risk")

    def inbound(self, **kwargs):
        return request_inbound("agent.tasks.risk", RISK_REQUEST, **kwargs)

    def outputs(self, inbound=None, bus=None):
        bus = bus or self.bus
        request = parse_request(bus, inbound or self.inbound())
        return request, Outputs(bus, "risk", self.settings, request, clock=CLOCK)

    def test_parse_request_returns_typed_fields(self):
        request = parse_request(self.bus, self.inbound(task_id="t-9"))
        self.assertEqual(request.data, RISK_REQUEST)
        self.assertEqual(request.task_id, "t-9")
        self.assertEqual(request.envelope.device_id, "dev-0001")
        self.assertEqual(request.rpc_id, "router-0")
        self.assertEqual((request.message_id, request.context_id), ("router-0", "corr-1"))
        self.assertEqual(request.text, "Assess the device")
        self.assertEqual(request.reference_task_ids, [])

    def test_invalid_request_is_counted_and_skipped(self):
        before = invalid_count("risk")
        bad = request_inbound("agent.tasks.risk", {"deviceId": "dev-0001", "customerId": "cust-001",
                                                   "requiredContextSeq": 7})
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.assertIsNone(parse_request(self.bus, bad))
        self.assertEqual(invalid_count("risk") - before, 1)
        tombstone = Inbound("agent.tasks.risk", 0, 1, "dev-0001", {}, None, 0)
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.assertIsNone(parse_request(self.bus, tombstone))
        self.assertEqual(invalid_count("risk") - before, 2)

    def test_non_send_message_record_is_counted_and_skipped(self):
        before = invalid_count("risk")
        inbound = self.inbound()
        other = Inbound(inbound.topic, 0, 0, None, {}, {**inbound.value, "method": "GetTask"}, 0)
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.assertIsNone(parse_request(self.bus, other))
        self.assertEqual(invalid_count("risk") - before, 1)

    def test_output_envelope_copies_inbound_and_derives_ids(self):
        request, outputs = self.outputs()
        first, second = outputs.next_envelope(), outputs.next_envelope()
        self.assertEqual(first.message_id, ids.message_id("agent:risk", request.message_id, "cfg-1", 0))
        self.assertEqual(second.message_id, ids.message_id("agent:risk", request.message_id, "cfg-1", 1))
        self.assertEqual(first.causation_id, request.message_id)
        self.assertEqual(first.agent_id, "agent:risk")
        self.assertEqual(first.agent_version, AGENT_VERSION)
        self.assertEqual(first.config_version, "cfg-1")
        self.assertEqual(first.event_time, 1793323940500)
        self.assertEqual((first.correlation_id, first.customer_id, first.device_id, first.device_seq,
                          first.required_context_seq),
                         ("corr-1", "cust-001", "dev-0001", 7, 7))
        self.assertEqual(outputs.new_task_id(), ids.task_id("agent:risk", request.message_id))

    def test_replay_mode_and_run_id_propagate(self):
        _, outputs = self.outputs(self.inbound(mode="REPLAY", replay_run_id="r1"))
        envelope = outputs.next_envelope()
        self.assertEqual((envelope.mode, envelope.replay_run_id), ("REPLAY", "r1"))

    def test_task_events_and_handoff_validate_and_key_by_device(self):
        request, outputs = self.outputs()
        task_id = outputs.new_task_id()
        outputs.submitted(task_id)
        outputs.status(task_id, WORKING, "Assessing")
        outputs.artifact(task_id, "risk-assessment", ASSESSMENT, "Low risk")
        outputs.send("enrolment", ENROLMENT_REQUEST, "Decide", task_id=None, reference_task_ids=[task_id])
        events = produced(self.bus, "agent.task-events")
        self.assertEqual([k for k, _ in events], ["dev-0001"] * 3)
        self.assertEqual({e["id"] for _, e in events}, {request.rpc_id})
        self.assertEqual(events[0][1]["result"]["task"]["status"]["state"], SUBMITTED)
        self.assertEqual(events[0][1]["result"]["task"]["status"]["timestamp"], now_iso(1793323940500))
        self.assertEqual(events[1][1]["result"]["statusUpdate"]["status"]["message"]["parts"][0]["text"],
                         "Assessing")
        artifact = events[2][1]["result"]["artifactUpdate"]
        envelope = artifact["metadata"]["urn:device-trust-mesh:envelope:v1"]
        self.assertEqual(artifact["artifact"]["artifactId"], envelope["messageId"])
        ((key, handoff),) = produced(self.bus, "agent.tasks.enrolment")
        self.assertEqual(key, "dev-0001")
        self.assertEqual(handoff["params"]["message"]["referenceTaskIds"], [task_id])
        self.assertEqual(handoff["id"], ids.message_id("agent:risk", request.message_id, "cfg-1", 3))

    def test_policy_command_record_shape(self):
        _, outputs = self.outputs()
        outputs.policy_command("CHALLENGE", "x", challenge_method="OTP", enrolment_task_id="t")
        ((key, value),) = produced(self.bus, "device.policy-commands")
        self.assertEqual(key, "dev-0001")
        self.assertEqual({k: v for k, v in value.items() if k != "envelope"},
                         {"action": "CHALLENGE", "reason": "x", "challenge_method": "OTP",
                          "enrolment_task_id": "t"})
        self.assertEqual(value["envelope"]["agent_id"], "agent:risk")
        self.assertEqual(value["envelope"]["causation_id"], "router-0")

    def test_avro_returns_its_envelope(self):
        _, outputs = self.outputs()
        envelope = outputs.avro("agent.llm-interactions", {"status": "OK"})
        ((_, value),) = produced(self.bus, "agent.llm-interactions")
        self.assertEqual(value, {"envelope": envelope.to_avro(), "status": "OK"})

    def test_llm_interaction_is_keyed_and_returns_its_message_id(self):
        _, outputs = self.outputs()
        req = LLMRequest("ollama", "m", [{"role": "user", "content": "hi"}], {"type": "object"}, {}, "risk.v1",
                         "router-0")
        message_id = outputs.llm_interaction(req, LLMResponse(OK, {"a": 1}, {"a": 1}, 12), "none")
        ((key, value),) = produced(self.bus, "agent.llm-interactions")
        self.assertEqual((key, value["envelope"]["message_id"], value["guardrail_result"]),
                         ("dev-0001", message_id, "none"))
        self.assertEqual(message_id, ids.message_id("agent:risk", "router-0", "cfg-1", 0))

    def test_redelivery_reproduces_identical_records(self):
        def run():
            bus = make_bus("risk")
            _, outputs = self.outputs(bus=bus)
            task_id = outputs.new_task_id()
            outputs.submitted(task_id)
            outputs.artifact(task_id, "risk-assessment", ASSESSMENT, "Low risk")
            outputs.policy_command("ALLOW", "GRANTED")
            outputs.send("enrolment", ENROLMENT_REQUEST, "Decide")
            return [(r.topic, r.key, r.value) for r in bus._producer.records]
        self.assertEqual(run(), run())

    def test_run_task_reports_failure_and_swallows(self):
        _, outputs = self.outputs()
        task_id = outputs.new_task_id()

        def body():
            outputs.status(task_id, WORKING)
            raise RuntimeError("boom")

        with self.assertLogs("dtm.mesh.agents.kit", "ERROR"):
            run_task(outputs, task_id, body)
        last = produced(self.bus, "agent.task-events")[-1][1]["result"]["statusUpdate"]
        self.assertEqual(last["status"]["state"], FAILED)
        self.assertEqual(last["status"]["message"]["parts"][0]["text"], "RuntimeError: boom")

    def test_run_task_leaves_a_clean_body_alone(self):
        _, outputs = self.outputs()
        run_task(outputs, "t", lambda: None)
        self.assertEqual(produced(self.bus, "agent.task-events"), [])

    def test_parse_task_event_reads_artifact(self):
        envelope = Envelope("m-1", "corr-1", "router-0", "agent:risk", "1", "cfg-1", 1793323940500, "dev-0001",
                            "cust-001", 7, 7)
        record = artifact_update("router-0", "t-1", "corr-1", "m-1", "risk-assessment", ASSESSMENT, "Low", envelope)
        inbound = Inbound("agent.task-events", 0, 3, "dev-0001", {}, json.loads(json.dumps(record)), 0)
        event = parse_task_event(self.bus, inbound)
        self.assertEqual((event.kind, event.task_id, event.state), ("artifactUpdate", "t-1", None))
        self.assertEqual((event.artifact_name, event.artifact_data), ("risk-assessment", ASSESSMENT))
        self.assertEqual(event.envelope.agent_id, "agent:risk")
        self.assertEqual((event.message_id, event.rpc_id, event.context_id), ("m-1", "router-0", "corr-1"))

    def test_parse_task_event_reads_task_and_status(self):
        outputs_bus = make_bus("risk")
        _, outputs = self.outputs(bus=outputs_bus)
        outputs.submitted("t-1")
        outputs.status("t-1", FAILED, "x")
        events = [parse_task_event(self.bus, Inbound("agent.task-events", 0, i, "dev-0001", {}, value, 0))
                  for i, (_, value) in enumerate(produced(outputs_bus, "agent.task-events"))]
        self.assertEqual([(e.kind, e.task_id, e.state) for e in events],
                         [("task", "t-1", SUBMITTED), ("statusUpdate", "t-1", FAILED)])

    def test_invalid_task_event_is_counted_and_skipped(self):
        before = invalid_count("risk")
        with self.assertLogs("dtm.mesh.agents.kit", "WARNING"):
            self.assertIsNone(parse_task_event(self.bus, Inbound("agent.task-events", 0, 0, None, {},
                                                                 {"jsonrpc": "2.0", "id": "x"}, 0)))
        self.assertEqual(invalid_count("risk") - before, 1)


if __name__ == "__main__":
    unittest.main()
