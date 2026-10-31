import unittest
import uuid

import fastavro
import fastavro.validation

from dtm.common.schemas import load_avro_schema
from dtm.simulator.events import AGENT_ID, device_event
from dtm.simulator.fingerprints import GEO, fingerprint
from dtm.simulator.pool import DevicePool

NOW = 1793323937770


class DeviceEventTest(unittest.TestCase):
    def setUp(self):
        self.device = DevicePool(1).devices[0]

    def test_device_event_validates(self):
        ev = device_event(DevicePool(1).devices[0], "ENROL_REQUESTED", seq=5, now_ms=1793323937770,
                          correlation_id="c-1", fp=fingerprint("IOS", "CLEAN"), country="GB")
        fastavro.validation.validate(ev, fastavro.parse_schema(load_avro_schema("device.events-value")),
                                     raise_errors=True)
        self.assertEqual(ev["envelope"]["device_seq"], 5)

    def test_every_event_type_validates(self):
        schema = fastavro.parse_schema(load_avro_schema("device.events-value"))
        for t in ("ENROL_REQUESTED", "UNENROL_REQUESTED", "CHALLENGE_PASSED", "CHALLENGE_FAILED"):
            fastavro.validation.validate(self._event_of(t), schema, raise_errors=True)

    def _event_of(self, event_type, **kw):
        args = dict(seq=5, now_ms=NOW, correlation_id="c-1", fp=fingerprint("IOS", "CLEAN"), country="GB")
        args.update(kw)
        return device_event(self.device, event_type, **args)

    def test_envelope_values(self):
        env = self._event_of("ENROL_REQUESTED", seq=9, version="abc")["envelope"]
        self.assertEqual(env["agent_id"], AGENT_ID)
        self.assertEqual(AGENT_ID, "simulator:device-simulator")
        self.assertEqual(env["agent_version"], "abc")
        self.assertEqual(env["config_version"], "n/a")
        self.assertEqual(env["event_time"], NOW)
        self.assertEqual(env["device_id"], "dev-0001")
        self.assertEqual(env["customer_id"], "cust-001")
        self.assertEqual(env["device_seq"], 9)
        self.assertIsNone(env["required_context_seq"])
        self.assertIsNone(env["causation_id"])
        self.assertEqual(env["correlation_id"], "c-1")
        self.assertEqual(env["mode"], "LIVE")
        self.assertIsNone(env["replay_run_id"])

    def test_default_version_is_dev(self):
        self.assertEqual(self._event_of("ENROL_REQUESTED")["envelope"]["agent_version"], "dev")

    def test_message_id_is_a_fresh_uuid4(self):
        a = self._event_of("ENROL_REQUESTED")["envelope"]["message_id"]
        b = self._event_of("ENROL_REQUESTED")["envelope"]["message_id"]
        self.assertNotEqual(a, b)
        self.assertEqual(uuid.UUID(a).version, 4)

    def test_causation_and_task_id_pass_through(self):
        ev = self._event_of("CHALLENGE_PASSED", causation_id="m-0", enrolment_task_id="t-1")
        self.assertEqual(ev["envelope"]["causation_id"], "m-0")
        self.assertEqual(ev["enrolment_task_id"], "t-1")
        self.assertIsNone(self._event_of("ENROL_REQUESTED")["enrolment_task_id"])

    def test_body_fields(self):
        fp = fingerprint("ANDROID", "ROOTED")
        ev = self._event_of("ENROL_REQUESTED", fp=fp, country="AU")
        lat, lon = GEO["AU"]
        self.assertEqual(ev["event_type"], "ENROL_REQUESTED")
        self.assertEqual(ev["fingerprint"], fp)
        self.assertEqual(ev["geo"], {"country": "AU", "lat": lat, "lon": lon})
        self.assertEqual(ev["account_age_days"], self.device.account_age_days)

    def test_geo_covers_the_scenario_countries(self):
        self.assertEqual(set(GEO), {"GB", "US", "DE", "FR", "CA", "AU"})

    def test_unknown_country_rejected(self):
        self.assertRaises(ValueError, self._event_of, "ENROL_REQUESTED", country="ZZ")


if __name__ == "__main__":
    unittest.main()
