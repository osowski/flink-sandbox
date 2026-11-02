import unittest
from datetime import datetime, timezone

from dtm.ui.model import Board, Coord, display_status, percentile

T0 = 1793448000000
T0_DT = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)


def coord(offset=0, topic="agent.tasks_human_readable", partition=0, ts=0):
    return Coord(topic, partition, offset, ts)


def envelope(message_id="m-1", causation_id=None, device_id="dev-0001", event_time=T0_DT,
             customer_id="cust-001", device_seq=1):
    return {"message_id": message_id, "correlation_id": "corr-1", "causation_id": causation_id,
            "agent_id": "agent:risk", "agent_version": "1", "config_version": "c1",
            "event_time": event_time, "device_id": device_id, "customer_id": customer_id,
            "device_seq": device_seq, "required_context_seq": None, "mode": "LIVE",
            "replay_run_id": None}


def task(message_id="m-1", from_agent="dtm-a2a-task-router", to_agent="risk", device_id="dev-0001",
         task_id=None, event_time=T0_DT):
    return {"envelope": envelope(message_id, None, device_id, event_time), "from_agent": from_agent,
            "to_agent": to_agent, "task_type": "ENROL", "task_id": task_id, "message_id": message_id,
            "reference_task_ids": None, "summary": "s"}


def decision(message_id="m-2", causation_id="m-1", agent="risk", device_id="dev-0001", event_time=T0_DT,
             outcome="GRANTED", policy_action="ALLOW", **over):
    return {"envelope": envelope(message_id, causation_id, device_id, event_time), "agent": agent,
            "task_id": "t-1", "artifact_name": "enrolment-decision", "outcome": outcome,
            "policy_action": policy_action, "risk_score": 10, "rationale": "looks fine",
            "signal_ids": None, "method": None, **over}


def status(message_id="m-3", causation_id="m-1", agent="risk", device_id="dev-0001", event_time=T0_DT):
    return {"envelope": envelope(message_id, causation_id, device_id, event_time), "agent": agent,
            "task_id": "t-1", "state": "TASK_STATE_COMPLETED", "status_time": None, "status_text": None}


def device(device_id="dev-0001", status="TRUSTED", seq=1, last_event_time=T0, customer_id="cust-001"):
    return {"device_id": device_id, "customer_id": customer_id, "trust_status": status,
            "context_seq": seq, "last_event_time": last_event_time}


def pair_count(window_end, from_agent, to_agent, count):
    return {"window_start": datetime.fromtimestamp((window_end - 60000) / 1000, tz=timezone.utc),
            "window_end": datetime.fromtimestamp(window_end / 1000, tz=timezone.utc),
            "from_agent": from_agent, "to_agent": to_agent, "message_count": count}


def latency(device_id="dev-0001", latency_ms=120, artifact_name="enrolment-decision", outcome="GRANTED",
            policy_action="ALLOW", rationale="looks fine", decision_time=T0 + 1000):
    return {"device_id": device_id, "customer_id": "cust-001", "correlation_id": "corr-1",
            "device_seq": 1, "agent": "enrolment", "task_id": "t-1", "artifact_name": artifact_name,
            "outcome": outcome, "policy_action": policy_action, "rationale": rationale,
            "event_time": T0_DT, "decision_time": datetime.fromtimestamp(decision_time / 1000, tz=timezone.utc),
            "latency_ms": latency_ms}


class Clock:
    def __init__(self, now_ms=T0):
        self.now_ms = now_ms

    def __call__(self):
        return self.now_ms / 1000


def kinds(deltas):
    return [k for k, _ in deltas]


def payload(deltas, kind):
    return next(p for k, p in deltas if k == kind)


class DisplayStatusTest(unittest.TestCase):
    def test_display_status_mapping(self):
        cases = [
            (("BLOCKED", None), ("BLOCKED", "red")),
            (("UNENROLLED", None), ("UNENROLLED", "red")),
            (("TRUSTED", "CHALLENGED"), ("CLEARED", "green")),
            (("TRUSTED", "PENDING"), ("CLEARED", "green")),
            (("TRUSTED", "TRUSTED"), ("TRUSTED", "green")),
            (("TRUSTED", None), ("TRUSTED", "green")),
            (("TRUSTED", "READ_ONLY"), ("TRUSTED", "green")),
            (("CHALLENGED", None), ("STEP-UP", "amber")),
            (("READ_ONLY", None), ("READ-ONLY", "amber")),
            (("PENDING", None), ("PENDING", "grey")),
            (("UNKNOWN", None), ("UNKNOWN", "grey")),
            ((None, None), ("UNKNOWN", "grey")),
        ]
        for (trust, previous), expected in cases:
            with self.subTest(trust=trust, previous=previous):
                self.assertEqual(display_status(trust, previous), expected)


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertEqual(percentile(values, 50), 50)
        self.assertEqual(percentile(values, 95), 100)
        self.assertEqual(percentile([5], 95), 5)
        self.assertEqual(percentile([3, 1, 2], 50), 2)
        self.assertIsNone(percentile([], 50))


class DevicesTest(unittest.TestCase):
    def setUp(self):
        self.board = Board(clock=Clock())

    def test_first_poll_is_baseline_without_freshness(self):
        deltas = self.board.apply_devices([device("a", seq=3), device("b", seq=5)], T0 + 100)
        self.assertEqual(kinds(deltas), ["device", "device", "summary"])
        self.assertEqual([d["freshness_ms"] for d in self.board.snapshot()["devices"]], [None, None])

    def test_freshness_when_context_seq_advances(self):
        self.board.apply_devices([device(seq=3)], T0)
        deltas = self.board.apply_devices([device(seq=4, last_event_time=1000)], 1350)
        self.assertEqual(payload(deltas, "device")["freshness_ms"], 350)
        self.assertEqual(self.board.snapshot()["summary"]["freshness_p50_ms"], 350)

    def test_freshness_never_negative(self):
        self.board.apply_devices([device(seq=3)], T0)
        deltas = self.board.apply_devices([device(seq=4, last_event_time=2000)], 1000)
        self.assertEqual(payload(deltas, "device")["freshness_ms"], 0)

    def test_unchanged_poll_emits_only_summary_and_keeps_freshness(self):
        self.board.apply_devices([device(seq=3)], T0)
        self.board.apply_devices([device(seq=4, last_event_time=1000)], 1350)
        deltas = self.board.apply_devices([device(seq=4, last_event_time=1000)], 1850)
        self.assertEqual(kinds(deltas), ["summary"])
        self.assertEqual(self.board.snapshot()["devices"][0]["freshness_ms"], 350)

    def test_device_row_fields(self):
        self.board.apply_devices([device(seq=3, status="PENDING")], T0 + 5)
        self.assertEqual(self.board.snapshot()["devices"][0], {
            "device_id": "dev-0001", "customer_id": "cust-001", "trust_status": "PENDING",
            "context_seq": 3, "last_event_time_ms": T0, "label": "PENDING", "color": "grey",
            "reason": None, "latency_ms": None, "freshness_ms": None, "changed_at_ms": T0 + 5})

    def test_status_change_moves_row_to_top_and_emits_device_delta(self):
        self.board.apply_devices([device("a", "TRUSTED"), device("b", "TRUSTED")], 1000)
        self.assertEqual([d["device_id"] for d in self.board.snapshot()["devices"]], ["a", "b"])
        deltas = self.board.apply_devices(
            [device("a", "TRUSTED"), device("b", "BLOCKED", seq=2)], 2000)
        self.assertEqual(kinds(deltas), ["device", "summary"])
        self.assertEqual(payload(deltas, "device")["device_id"], "b")
        self.assertEqual(payload(deltas, "device")["changed_at_ms"], 2000)
        self.assertEqual([d["device_id"] for d in self.board.snapshot()["devices"]], ["b", "a"])

    def test_blocked_then_cleared_colors(self):
        seen = []
        for seq, st in enumerate(["PENDING", "CHALLENGED", "TRUSTED"], start=1):
            self.board.apply_devices([device(status=st, seq=seq)], T0 + seq)
            row = self.board.snapshot()["devices"][0]
            seen.append((row["label"], row["color"]))
        self.assertEqual(seen, [("PENDING", "grey"), ("STEP-UP", "amber"), ("CLEARED", "green")])

    def test_summary_counts_by_color(self):
        self.board.apply_devices([device("a", "BLOCKED"), device("b", "TRUSTED"), device("c", "TRUSTED"),
                                  device("d", "CHALLENGED"), device("e", None)], T0)
        summary = self.board.snapshot()["summary"]
        self.assertEqual(summary["device_count"], 5)
        self.assertEqual(summary["by_color"], {"red": 1, "green": 2, "amber": 1, "grey": 1})


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.board = Board(clock=Clock())

    def test_datetime_and_int_times_agree(self):
        from_dt = self.board.apply_task(task(event_time=T0_DT), coord())
        from_int = self.board.apply_task(task(event_time=T0), coord())
        self.assertEqual(payload(from_dt, "message")["time_ms"], T0)
        self.assertEqual(payload(from_dt, "message")["time_ms"], payload(from_int, "message")["time_ms"])

    def test_task_message_payload(self):
        deltas = self.board.apply_task(task(task_id="t-9"), coord(7, partition=2))
        self.assertEqual(deltas, [("message", {
            "kind": "task", "time_ms": T0, "source": "dtm-a2a-task-router", "target": "risk",
            "device_id": "dev-0001", "task_id": "t-9", "message_id": "m-1",
            "topic": "agent.tasks_human_readable", "partition": 2, "offset": 7})])

    def test_task_then_decision_resolves_requester_by_causation_id(self):
        self.board.apply_task(task("m-1", from_agent="dtm-a2a-task-router", to_agent="risk"), coord())
        deltas = self.board.apply_decision(decision("m-2", causation_id="m-1", agent="risk"),
                                           coord(1, "agent.decisions_human_readable"))
        msg = payload(deltas, "message")
        self.assertEqual((msg["kind"], msg["source"], msg["target"]), ("decision", "risk", "dtm-a2a-task-router"))
        self.assertEqual((msg["message_id"], msg["task_id"], msg["topic"], msg["offset"]),
                         ("m-2", "t-1", "agent.decisions_human_readable", 1))

    def test_status_resolves_requester(self):
        self.board.apply_task(task("m-1", from_agent="enrolment"), coord())
        msg = payload(self.board.apply_status(status(causation_id="m-1", agent="risk"), coord()), "message")
        self.assertEqual((msg["kind"], msg["source"], msg["target"]), ("status", "risk", "enrolment"))

    def test_decision_with_unknown_cause_has_none_target(self):
        msg = payload(self.board.apply_decision(decision(causation_id="never-seen"), coord()), "message")
        self.assertIsNone(msg["target"])

    def test_decision_with_null_fields(self):
        value = decision(outcome=None, policy_action=None)
        value["envelope"]["customer_id"] = None
        value["envelope"]["device_seq"] = None
        value["envelope"]["causation_id"] = None
        deltas = self.board.apply_decision(value, coord())
        self.assertEqual(kinds(deltas), ["message"])

    def test_feed_is_bounded_newest_first(self):
        board = Board(clock=Clock(), feed_limit=3)
        for i in range(5):
            board.apply_task(task(f"m-{i}"), coord(i))
        self.assertEqual([m["message_id"] for m in board.snapshot()["feed"]], ["m-4", "m-3", "m-2"])

    def test_requester_map_is_bounded(self):
        board = Board(clock=Clock(), feed_limit=1)
        for i in range(10):
            board.apply_task(task(f"m-{i}", from_agent=f"a{i}"), coord(i))
        old = payload(board.apply_decision(decision(causation_id="m-0"), coord()), "message")
        new = payload(board.apply_decision(decision(causation_id="m-9"), coord()), "message")
        self.assertIsNone(old["target"])
        self.assertEqual(new["target"], "a9")


class PairsTest(unittest.TestCase):
    def test_pairs_rank_from_newest_window_with_history(self):
        board = Board(clock=Clock(), history_windows=3)
        w1, w2 = T0 + 5000, T0 + 10000
        board.apply_pair_counts(pair_count(w1, "router", "risk", 4), coord())
        board.apply_pair_counts(pair_count(w1, "risk", "enrolment", 9), coord())
        board.apply_pair_counts(pair_count(w2, "router", "risk", 6), coord())
        deltas = board.apply_pair_counts(pair_count(w2, "risk", "enrolment", 6), coord())
        pairs = payload(deltas, "pairs")
        self.assertEqual(pairs["window_end_ms"], w2)
        self.assertEqual(pairs["ranking"], [
            {"from_agent": "risk", "to_agent": "enrolment", "message_count": 6, "history": [9, 6]},
            {"from_agent": "router", "to_agent": "risk", "message_count": 6, "history": [4, 6]}])
        self.assertEqual(board.snapshot()["pairs"], pairs)

    def test_late_older_window_does_not_change_ranking(self):
        board = Board(clock=Clock())
        board.apply_pair_counts(pair_count(T0 + 10000, "a", "b", 1), coord())
        deltas = board.apply_pair_counts(pair_count(T0 + 5000, "a", "b", 99), coord())
        pairs = payload(deltas, "pairs")
        self.assertEqual(pairs["window_end_ms"], T0 + 10000)
        self.assertEqual(pairs["ranking"][0]["message_count"], 1)
        self.assertEqual(pairs["ranking"][0]["history"], [99, 1])

    def test_only_last_history_windows_are_kept(self):
        board = Board(clock=Clock(), history_windows=2)
        for i in range(4):
            board.apply_pair_counts(pair_count(T0 + i * 5000, "a", "b", i + 1), coord())
        self.assertEqual(board.snapshot()["pairs"]["ranking"][0]["history"], [3, 4])

    def test_redelivered_row_does_not_double_count(self):
        board = Board(clock=Clock())
        board.apply_pair_counts(pair_count(T0, "a", "b", 5), coord())
        deltas = board.apply_pair_counts(pair_count(T0, "a", "b", 5), coord())
        self.assertEqual(payload(deltas, "pairs")["ranking"][0]["message_count"], 5)

    def test_ranking_ties_sort_by_from_then_to(self):
        board = Board(clock=Clock())
        for f, t in [("b", "a"), ("a", "z"), ("a", "c")]:
            board.apply_pair_counts(pair_count(T0, f, t, 2), coord())
        self.assertEqual([(r["from_agent"], r["to_agent"]) for r in board.snapshot()["pairs"]["ranking"]],
                         [("a", "c"), ("a", "z"), ("b", "a")])


class LatencyTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.board = Board(clock=self.clock, window_ms=300_000)
        self.board.apply_devices([device()], T0)

    def test_latency_sets_reason_and_latency(self):
        deltas = self.board.apply_latency(latency(latency_ms=250), coord())
        self.assertEqual(kinds(deltas), ["device", "summary"])
        row = payload(deltas, "device")
        self.assertEqual((row["latency_ms"], row["reason"]), (250, "GRANTED · ALLOW — looks fine"))
        self.assertEqual(self.board.snapshot()["summary"]["decision_p50_ms"], 250)

    def test_reason_leaves_out_null_parts_and_truncates_rationale(self):
        self.board.apply_latency(latency(outcome=None, rationale="x" * 200), coord())
        self.assertEqual(self.board.snapshot()["devices"][0]["reason"], "ALLOW — " + "x" * 80)
        self.board.apply_latency(latency(outcome="DENIED", policy_action=None, rationale=None,
                                         decision_time=T0 + 2000), coord())
        self.assertEqual(self.board.snapshot()["devices"][0]["reason"], "DENIED")

    def test_latency_only_enrolment_decision_updates_device(self):
        self.board.apply_latency(latency(latency_ms=100), coord())
        before = self.board.snapshot()
        deltas = self.board.apply_latency(latency(artifact_name="audit-record", latency_ms=9999), coord())
        self.assertEqual(deltas, [])
        self.assertEqual(self.board.snapshot(), before)

    def test_latency_before_device_attaches_later(self):
        deltas = self.board.apply_latency(latency(device_id="dev-0002", latency_ms=77), coord())
        self.assertNotIn("device", kinds(deltas))
        self.assertEqual(len(self.board.snapshot()["devices"]), 1)
        deltas = self.board.apply_devices([device(), device("dev-0002")], T0 + 500)
        row = next(p for k, p in deltas if k == "device" and p["device_id"] == "dev-0002")
        self.assertEqual((row["latency_ms"], row["reason"]), (77, "GRANTED · ALLOW — looks fine"))

    def test_redelivered_rows_leave_device_row_correct(self):
        first = latency(latency_ms=100, outcome="CHALLENGED", decision_time=T0 + 1000)
        second = latency(latency_ms=300, outcome="GRANTED", decision_time=T0 + 3000)
        for value in (first, second, first, second):
            self.board.apply_latency(value, coord())
        row = self.board.snapshot()["devices"][0]
        self.assertEqual((row["latency_ms"], row["reason"]), (300, "GRANTED · ALLOW — looks fine"))

    def test_redelivered_rows_before_device_attaches_keep_newest(self):
        first = latency(device_id="dev-0002", latency_ms=100, decision_time=T0 + 1000)
        second = latency(device_id="dev-0002", latency_ms=300, decision_time=T0 + 3000)
        for value in (first, second, first):
            self.board.apply_latency(value, coord())
        self.board.apply_devices([device(), device("dev-0002")], T0 + 500)
        row = next(d for d in self.board.snapshot()["devices"] if d["device_id"] == "dev-0002")
        self.assertEqual(row["latency_ms"], 300)

    def test_summary_percentiles_window(self):
        self.board.apply_latency(latency(latency_ms=1000, decision_time=T0 + 1000), coord())
        self.clock.now_ms = T0 + 200_000
        self.board.apply_latency(latency(latency_ms=100, decision_time=T0 + 2000), coord())
        self.assertEqual(self.board.snapshot()["summary"]["decision_p95_ms"], 1000)
        self.clock.now_ms = T0 + 400_000
        summary = self.board.snapshot()["summary"]
        self.assertEqual((summary["decision_p50_ms"], summary["decision_p95_ms"]), (100, 100))
        self.clock.now_ms = T0 + 600_000
        self.assertIsNone(self.board.snapshot()["summary"]["decision_p50_ms"])

    def test_freshness_percentiles_window(self):
        board = Board(clock=self.clock, window_ms=1000)
        board.apply_devices([device(seq=1)], T0)
        board.apply_devices([device(seq=2, last_event_time=T0)], T0 + 400)
        self.assertEqual(board.snapshot()["summary"]["freshness_p50_ms"], 400)
        self.clock.now_ms = T0 + 5000
        self.assertIsNone(board.snapshot()["summary"]["freshness_p50_ms"])

    def test_context_lag(self):
        deltas = self.board.apply_context_lag(12.0, 80.5)
        summary = payload(deltas, "summary")
        self.assertEqual((summary["context_lag_p50_ms"], summary["context_lag_p95_ms"]), (12.0, 80.5))
        self.assertEqual(self.board.snapshot()["summary"], summary)


class StaleTest(unittest.TestCase):
    def test_mark_stale_then_fresh(self):
        board = Board(clock=Clock())
        self.assertEqual(board.mark_stale("kafka", 5000), [("stale", {"source": "kafka", "since_ms": 5000})])
        self.assertEqual(board.mark_stale("kafka", 6000), [])
        self.assertEqual(board.snapshot()["stale"], {"kafka": 5000, "context": None})
        self.assertEqual(board.mark_fresh("kafka"), [("stale", {"source": "kafka", "since_ms": None})])
        self.assertEqual(board.mark_fresh("kafka"), [])
        self.assertEqual(board.mark_fresh("context"), [])
        self.assertEqual(board.snapshot()["stale"], {"kafka": None, "context": None})

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            Board(clock=Clock()).mark_stale("disk", 1)


class SnapshotTest(unittest.TestCase):
    def test_snapshot_when_empty(self):
        snap = Board(clock=Clock()).snapshot()
        self.assertEqual((snap["devices"], snap["feed"], snap["pairs"]["ranking"]), ([], [], []))
        self.assertEqual(snap["summary"], {
            "device_count": 0, "by_color": {"red": 0, "green": 0, "amber": 0, "grey": 0},
            "decision_p50_ms": None, "decision_p95_ms": None, "freshness_p50_ms": None,
            "freshness_p95_ms": None, "context_lag_p50_ms": None, "context_lag_p95_ms": None})
        self.assertEqual(snap["stale"], {"kafka": None, "context": None})

    def test_snapshot_is_a_copy(self):
        board = Board(clock=Clock())
        board.apply_devices([device()], T0)
        board.snapshot()["devices"][0]["label"] = "mutated"
        self.assertEqual(board.snapshot()["devices"][0]["label"], "TRUSTED")


if __name__ == "__main__":
    unittest.main()
