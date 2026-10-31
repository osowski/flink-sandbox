import unittest
from dataclasses import replace
from datetime import datetime, timezone

import fastavro
import fastavro.validation
from jsonschema import Draft202012Validator

from dtm.common.envelope import ENVELOPE_URI, Envelope, to_epoch_ms
from dtm.common.schemas import load_a2a_schema, load_avro_schema

ENV = Envelope("m-1", "c-1", None, "agent:risk", "dev", "abc123",
               1793323937770, "dev-0001", "cust-001", 5, 5)


def device_event(envelope: dict) -> dict:
    return {
        "envelope": envelope,
        "event_type": "ENROL_REQUESTED",
        "fingerprint": {"platform": "IOS", "os_version": "18.1", "rooted": False,
                        "browser_signals": None, "network_type": "wifi"},
        "geo": {"country": "US", "lat": 37.77, "lon": -122.42},
        "account_age_days": 400,
        "enrolment_task_id": None,
    }


class EnvelopeTest(unittest.TestCase):
    def test_envelope_uri(self):
        self.assertEqual(ENVELOPE_URI, "urn:device-trust-mesh:envelope:v1")

    def test_defaults(self):
        self.assertEqual(ENV.mode, "LIVE")
        self.assertIsNone(ENV.replay_run_id)

    def test_to_a2a_omits_none_and_keeps_ints(self):
        d = ENV.to_a2a()
        self.assertNotIn("causationId", d)
        self.assertNotIn("replayRunId", d)
        self.assertIs(type(d["eventTime"]), int)
        self.assertEqual(d["deviceSeq"], 5)
        self.assertEqual(d["requiredContextSeq"], 5)
        self.assertEqual(d["messageId"], "m-1")
        self.assertEqual(d["mode"], "LIVE")

    def test_to_a2a_validates_against_envelope_schema(self):
        schema = load_a2a_schema("agent.tasks.risk")
        validator = Draft202012Validator({**schema["$defs"]["Envelope"], "$defs": schema["$defs"]})
        validator.validate(ENV.to_a2a())
        validator.validate(replace(ENV, causation_id="m-0", mode="REPLAY", replay_run_id="r-1").to_a2a())

    def test_to_avro_has_every_field(self):
        d = ENV.to_avro()
        self.assertIsNone(d["causation_id"])
        self.assertIsNone(d["replay_run_id"])
        self.assertEqual(d["event_time"], 1793323937770)
        self.assertEqual(len(d), 13)

    def test_to_avro_validates_against_device_event_envelope(self):
        fastavro.validation.validate(
            device_event(ENV.to_avro()),
            fastavro.parse_schema(load_avro_schema("device.events-value")),
            raise_errors=True)

    def test_round_trips(self):
        self.assertEqual(Envelope.from_a2a(ENV.to_a2a()), ENV)
        avro = ENV.to_avro() | {"event_time": datetime.fromtimestamp(1793323937.770, tz=timezone.utc)}
        self.assertEqual(Envelope.from_avro(avro), ENV)
        self.assertEqual(Envelope.from_avro(ENV.to_avro()), ENV)

    def test_to_epoch_ms(self):
        self.assertIsNone(to_epoch_ms(None))
        self.assertEqual(to_epoch_ms(5), 5)
        self.assertEqual(to_epoch_ms(datetime.fromtimestamp(1793323937.770, tz=timezone.utc)), 1793323937770)


if __name__ == "__main__":
    unittest.main()
