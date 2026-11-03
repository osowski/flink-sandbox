import json
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


def capture(seconds: float):
    """Play the feed and return the raw task and decision values the board was handed."""
    clock = Clock()
    board = Board(clock=clock)
    tasks, decisions = [], []
    apply_task, apply_decision = board.apply_task, board.apply_decision

    def record_task(value, coord):
        tasks.append(value)
        return apply_task(value, coord)

    def record_decision(value, coord):
        decisions.append(value)
        return apply_decision(value, coord)

    board.apply_task, board.apply_decision = record_task, record_decision
    feed = FakeFeed(board, lambda deltas: None, clock=clock)
    for i in range(round(seconds / TICK_S) + 1):
        clock.now_s = T0_S + i * TICK_S
        feed.step(clock.now_s)
    return tasks, decisions


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
                                 ("enrolment", "step-up")})
        self.assertEqual({m["kind"] for m in messages}, {"task", "decision", "status"})
        for m in messages:
            if m["source"] == "compliance":
                continue
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

    def test_task_types_match_the_real_mesh(self):
        tasks, _ = capture(30)
        sent = {(v["from_agent"], v["to_agent"], v["task_type"]) for v in tasks}
        self.assertIn((ROUTER, "risk", "ASSESS_ENROLMENT"), sent)
        self.assertIn(("risk", "enrolment", "DECIDE_ENROLMENT"), sent)
        self.assertIn(("enrolment", "step-up", "STEP_UP"), sent)
        self.assertIn((ROUTER, "enrolment", "RESOLVE_CHALLENGE"), sent)
        self.assertFalse(any(to == "compliance" for _, to, _ in sent))

    def test_compliance_audits_every_artifact(self):
        _, decisions = capture(30)
        audits = [d for d in decisions if d["artifact_name"] == "audit-record"]
        audited = [d for d in decisions if d["artifact_name"] != "audit-record"]
        self.assertEqual({d["artifact_name"] for d in audited},
                         {"risk-assessment", "enrolment-decision", "step-up-challenge"})
        for d in audited:
            with self.subTest(artifact=d["artifact_name"], message=d["envelope"]["message_id"]):
                replies = [a for a in audits if a["envelope"]["causation_id"] == d["envelope"]["message_id"]]
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0]["agent"], "compliance")
                self.assertEqual(replies[0]["outcome"], d["outcome"] or d["method"])
        self.assertEqual(len(audits), len(audited))

    def test_risk_assessment_carries_signal_ids_unless_clean(self):
        _, decisions = capture(30)
        by_rationale = {}
        for d in decisions:
            if d["artifact_name"] == "risk-assessment":
                by_rationale[d["rationale"]] = d["signal_ids"]
        self.assertIsNone(by_rationale["CLEAN signals"])
        self.assertEqual(json.loads(by_rationale["OUTDATED_OS signals"]), ["OUTDATED_OS"])

    def test_step_up_challenge_carries_a_method(self):
        _, decisions = capture(30)
        challenges = [d for d in decisions if d["artifact_name"] == "step-up-challenge"]
        self.assertTrue(challenges)
        for d in challenges:
            self.assertEqual(d["method"], "OTP")
            self.assertIsNone(d["outcome"])
            self.assertIsNone(d["policy_action"])

    def test_task_summaries_are_sentences(self):
        tasks, _ = capture(30)
        self.assertTrue(tasks)
        for v in tasks:
            with self.subTest(summary=v["summary"]):
                self.assertIn(v["envelope"]["device_id"], v["summary"])
                self.assertIn(" ", v["summary"])
                self.assertNotEqual(v["summary"], f"{v['task_type']} {v['envelope']['device_id']}")

    def test_record_positions_are_unique(self):
        positions = [(m["topic"], m["partition"], m["offset"]) for m in self.of("message")]
        self.assertEqual(len(positions), len(set(positions)))

    def test_pair_count_window_every_five_seconds(self):
        ends = sorted({p["window_end_ms"] for p in self.of("pairs")})
        self.assertGreaterEqual(len(ends), 5)
        self.assertEqual({b - a for a, b in zip(ends, ends[1:])}, {5000})

    def test_pair_count_windows_are_five_minutes(self):
        pairs = self.of("pairs")
        self.assertEqual({p["window_ms"] for p in pairs}, {300_000})
        self.assertTrue(all(r["per_minute"] >= 0 for p in pairs for r in p["ranking"]))

    def test_summary_has_both_latencies(self):
        summary = self.snapshot["summary"]
        self.assertIsNotNone(summary["decision_p50_ms"])
        self.assertIsNotNone(summary["freshness_p50_ms"])
        self.assertIsNotNone(summary["context_lag_p50_ms"])

    def test_every_working_agent_ends_with_a_bubble(self):
        activity = self.snapshot["activity"]
        self.assertLessEqual({"risk", "enrolment", "step-up", "compliance"}, set(activity))
        for agent, bubble in activity.items():
            with self.subTest(agent=agent):
                self.assertNotIn(None, (bubble["to"], bubble["action"]))
                self.assertNotEqual(agent, ROUTER)
        json.dumps(self.snapshot, allow_nan=False)
        for bubble in self.of("activity"):
            json.dumps(bubble, allow_nan=False)

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
