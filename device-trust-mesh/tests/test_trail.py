import logging
import unittest

from dtm.mesh import trail
from trail_capture import capture

IDS = {"agent": "risk", "task_id": "task-1", "context_id": "ctx-1", "device_id": "dev-0001"}


class TrailTest(unittest.TestCase):
    def setUp(self):
        self.cap = capture(self, with_scope_filter=False)

    def test_step_carries_scope_ids_under_dtm(self):
        with trail.message_scope("t", 3, 42):
            trail.establish("risk", "task-1", "ctx-1", "dev-0001")
            trail.step("decision", artifact="risk-assessment", outcome="SCORED")
        self.assertEqual(len(self.cap.records), 2)
        record = self.cap.records[1]
        self.assertEqual(record.dtm, {**IDS, "step": "decision", "artifact": "risk-assessment", "outcome": "SCORED"})
        self.assertNotIn("task_id", vars(record))
        self.assertEqual(record.levelno, logging.INFO)

    def test_establish_logs_received_with_position(self):
        with trail.message_scope("t", 3, 42):
            trail.establish("risk", "task-1", "ctx-1", "dev-0001")
        record = self.cap.records[0]
        self.assertEqual(record.getMessage(), "received")
        self.assertEqual(record.dtm, {**IDS, "step": "received", "topic": "t", "partition": 3, "offset": 42})
        self.assertEqual(record.name, "dtm.trail")

    def test_establish_outside_a_scope_logs_received_without_position(self):
        trail.establish("risk", "task-1", "ctx-1", "dev-0001")
        self.assertEqual(self.cap.records[0].dtm, {**IDS, "step": "received"})
        self.assertFalse(trail.established())

    def test_scope_resets_after_exception(self):
        try:
            with trail.message_scope("t", 0, 1):
                trail.establish("risk", "task-1", "ctx-1", "dev-0001")
                self.assertTrue(trail.established())
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(trail.established())
        self.assertEqual(trail.ids(), {})
        self.assertEqual(trail.position(), {})

    def test_nested_scope_replaces_then_restores(self):
        with trail.message_scope("outer", 0, 1):
            trail.establish("risk", "task-1", "ctx-1", "dev-0001")
            with trail.message_scope("inner", 0, 2):
                self.assertEqual(trail.ids(), {})
                self.assertEqual(trail.position(), {"topic": "inner", "partition": 0, "offset": 2})
            self.assertEqual(trail.ids(), IDS)

    def test_none_fields_dropped(self):
        trail.step("context", seq=None, status="OK")
        self.assertEqual(self.cap.records[0].dtm, {"step": "context", "status": "OK"})

    def test_device_id_none_is_omitted(self):
        with trail.message_scope("t", 0, 1):
            trail.establish("compliance", "task-1", "ctx-1", None)
            self.assertEqual(trail.ids(), {"agent": "compliance", "task_id": "task-1", "context_id": "ctx-1"})

    def test_scope_filter_adds_ids_to_plain_records(self):
        self.cap.addFilter(trail.ScopeFilter())
        with trail.message_scope("t", 0, 1):
            trail.establish("risk", "task-1", "ctx-1", "dev-0001")
            logging.getLogger("dtm.mesh.x").warning("boom")
            logging.getLogger("dtm.mesh.x").error("failed", extra={"dtm": {"step": "x", "task_id": "own"}})
        plain, own = self.cap.records[1], self.cap.records[2]
        self.assertEqual(plain.dtm, IDS)
        self.assertEqual(own.dtm["step"], "x")
        self.assertEqual(own.dtm["task_id"], "own")
        self.assertEqual(own.dtm["context_id"], "ctx-1")

    def test_scope_filter_without_scope_leaves_record_unchanged(self):
        self.cap.addFilter(trail.ScopeFilter())
        logging.getLogger("dtm.mesh.x").warning("boom")
        self.assertFalse(hasattr(self.cap.records[0], "dtm"))

    def test_parse_level(self):
        self.assertEqual(trail.parse_level(None), logging.INFO)
        self.assertEqual(trail.parse_level(""), logging.INFO)
        self.assertEqual(trail.parse_level("warning"), logging.WARNING)
        self.assertEqual(trail.parse_level("DEBUG"), logging.DEBUG)
        with self.assertRaises(ValueError) as e:
            trail.parse_level("verbose")
        self.assertIn("'verbose'", str(e.exception))
        self.assertIn("DTM_LOG_LEVEL", str(e.exception))


if __name__ == "__main__":
    unittest.main()
