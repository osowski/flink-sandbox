import unittest
from unittest import mock

import fastavro
import fastavro.validation

from dtm.common.schemas import load_avro_schema
from dtm.simulator import ctl
from dtm.simulator.ctl import control_record, parse

SCHEMA = fastavro.parse_schema(load_avro_schema("simulator.control-value"))
NOW = 1793323937770


class ControlRecordTest(unittest.TestCase):
    def test_control_record_validates(self):
        rec = control_record("INJECT", scenario="IMPOSSIBLE_TRAVEL", count=2, now_ms=NOW)
        fastavro.validation.validate(rec, SCHEMA, raise_errors=True)

    def test_every_command_shape_validates(self):
        for rec in (control_record("START", now_ms=NOW), control_record("STOP", now_ms=NOW),
                    control_record(None, rate_per_sec=2.5, now_ms=NOW),
                    control_record("INJECT", scenario="OUTDATED_OS", now_ms=NOW)):
            fastavro.validation.validate(rec, SCHEMA, raise_errors=True)

    def test_envelope_identifies_the_cli(self):
        env = control_record("START", now_ms=NOW)["envelope"]
        self.assertEqual((env["agent_id"], env["device_id"], env["event_time"]), ("cli:dtm-ctl", "n/a", NOW))
        self.assertNotEqual(env["message_id"], control_record("START", now_ms=NOW)["envelope"]["message_id"])

    def test_fields_are_carried(self):
        rec = control_record("INJECT", scenario="CHALLENGE", rate_per_sec=1.0, count=4, now_ms=NOW)
        self.assertEqual((rec["command"], rec["scenario"], rec["rate_per_sec"], rec["count"]),
                         ("INJECT", "CHALLENGE", 1.0, 4))


class CliTest(unittest.TestCase):
    def test_cli_parses(self):
        self.assertEqual(parse(["inject", "OUTDATED_OS", "--count", "3"]), ("INJECT", "OUTDATED_OS", None, 3))
        self.assertEqual(parse(["inject", "CLEAN"]), ("INJECT", "CLEAN", None, None))
        self.assertEqual(parse(["start"]), ("START", None, None, None))
        self.assertEqual(parse(["stop"]), ("STOP", None, None, None))
        self.assertEqual(parse(["rate", "0.5"]), (None, None, 0.5, None))

    def test_unknown_scenario_is_rejected(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parse(["inject", "NOPE"])

    def test_main_produces_keyed_by_scenario(self):
        with mock.patch.object(ctl, "_send") as send:
            self.assertEqual(ctl.main(["inject", "IMPOSSIBLE_TRAVEL", "--count", "2"]), 0)
        record, key = send.call_args.args
        self.assertEqual(key, "IMPOSSIBLE_TRAVEL")
        self.assertEqual((record["command"], record["scenario"], record["count"]), ("INJECT", "IMPOSSIBLE_TRAVEL", 2))

    def test_main_keys_scenario_less_commands_as_control(self):
        with mock.patch.object(ctl, "_send") as send:
            ctl.main(["stop"])
        self.assertEqual(send.call_args.args[1], "control")

    def test_main_reports_a_failed_send(self):
        with mock.patch.object(ctl, "_send", side_effect=RuntimeError("broker down")), mock.patch("sys.stderr"):
            self.assertEqual(ctl.main(["start"]), 1)
