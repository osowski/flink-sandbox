import unittest

from dtm.ui.fake import FakeFeed
from dtm.ui.model import Board

T0_S = 1793448000.0
TICK_S = 0.25
ROUTER = "dtm-a2a-task-router"


class Clock:
    def __init__(self, now_s=T0_S):
        self.now_s = now_s

    def __call__(self):
        return self.now_s


def run(seconds: float, **kwargs):
    clock = Clock()
    board = Board(clock=clock)
    published: list[list] = []
    feed = FakeFeed(board, published.append, clock=clock, **kwargs)
    for i in range(round(seconds / TICK_S) + 1):
        clock.now_s = T0_S + i * TICK_S
        feed.step(clock.now_s)
    deltas = [d for batch in published for d in batch]
    return board, deltas


class FakeFeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board, cls.deltas = run(30)
        cls.snapshot = cls.board.snapshot()

    def of(self, kind):
        return [p for k, p in self.deltas if k == kind]

    def test_fake_feed_populates_board(self):
        snap = self.snapshot
        self.assertEqual(len(snap["devices"]), 20)
        self.assertTrue(snap["feed"])
        self.assertTrue(snap["pairs"]["ranking"])
        self.assertGreaterEqual(snap["summary"]["by_color"]["red"], 1)
        self.assertGreaterEqual(snap["summary"]["by_color"]["green"], 1)

    def test_covers_every_display_status(self):
        labels = {d["label"] for d in self.of("device")}
        self.assertLessEqual({"BLOCKED", "STEP-UP", "CLEARED", "TRUSTED", "PENDING"}, labels)

    def test_tasks_travel_the_mesh_edges_and_replies_find_their_requester(self):
        messages = self.of("message")
        edges = {(m["source"], m["target"]) for m in messages if m["kind"] == "task"}
        self.assertEqual(edges, {(ROUTER, "risk"), ("risk", "enrolment"), (ROUTER, "enrolment"),
                                 ("enrolment", "step-up"), ("enrolment", "compliance")})
        self.assertEqual({m["kind"] for m in messages}, {"task", "decision", "status"})
        for m in messages:
            with self.subTest(message=m["message_id"]):
                self.assertIsNotNone(m["target"])

    def test_first_leg_runs_through_risk_and_the_router_only_resumes_challenges(self):
        tasks = [m for m in self.of("message") if m["kind"] == "task"]
        challenged = set()
        for m in tasks:
            if (m["source"], m["target"]) == ("enrolment", "step-up"):
                challenged.add(m["device_id"])
            if (m["source"], m["target"]) == (ROUTER, "enrolment"):
                with self.subTest(device=m["device_id"]):
                    self.assertIn(m["device_id"], challenged)
        self.assertTrue(any((m["source"], m["target"]) == (ROUTER, "enrolment") for m in tasks))

    def test_enrolment_decisions_answer_risk_on_the_first_leg_and_the_router_on_the_second(self):
        targets = {m["target"] for m in self.of("message")
                   if m["kind"] == "decision" and m["source"] == "enrolment"}
        self.assertEqual(targets, {"risk", ROUTER})

    def test_record_positions_are_unique(self):
        positions = [(m["topic"], m["partition"], m["offset"]) for m in self.of("message")]
        self.assertEqual(len(positions), len(set(positions)))

    def test_pair_count_window_every_five_seconds(self):
        ends = sorted({p["window_end_ms"] for p in self.of("pairs")})
        self.assertGreaterEqual(len(ends), 5)
        self.assertEqual({b - a for a, b in zip(ends, ends[1:])}, {5000})

    def test_summary_has_both_latencies(self):
        summary = self.snapshot["summary"]
        self.assertIsNotNone(summary["decision_p50_ms"])
        self.assertIsNotNone(summary["freshness_p50_ms"])
        self.assertIsNotNone(summary["context_lag_p50_ms"])

    def test_same_time_twice_adds_nothing(self):
        clock = Clock()
        published = []
        feed = FakeFeed(Board(clock=clock), published.append, clock=clock)
        feed.step(T0_S + 5)
        count = len(published)
        feed.step(T0_S + 5)
        self.assertEqual(len(published), count)

    def test_device_count_follows_argument(self):
        board, _ = run(2, devices=5)
        self.assertEqual(len(board.snapshot()["devices"]), 5)

    def test_start_runs_a_daemon_thread_until_stopped(self):
        board = Board()
        published = []
        feed = FakeFeed(board, published.append, rate_hz=50.0)
        thread = feed.start()
        self.assertTrue(thread.daemon)
        feed.stop()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
