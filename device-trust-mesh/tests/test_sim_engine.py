import random
import unittest
import uuid
from datetime import datetime, timezone

import fastavro
import fastavro.validation

from dtm.common.schemas import load_avro_schema
from dtm.simulator.engine import (DEFAULT_MIX, DEFAULT_RATE, MAX_INJECT_COUNT, MAX_RATE_PER_SEC, SCENARIOS,
                                  Simulator)
from dtm.simulator.pool import DevicePool

T0 = 1793323937770
DEVICE_EVENTS = fastavro.parse_schema(load_avro_schema("device.events-value"))


def sim(cap=20, rate=DEFAULT_RATE, rng_seed=0, **kw):
    s = Simulator(DevicePool(cap), random.Random(rng_seed), rate_per_sec=rate, **kw)
    s.tick(T0)
    return s


def first(events):
    assert events, "expected at least one event"
    return events[0]


def cmd(event, action, *, reason=None, enrolment_task_id=None, correlation_id=None,
        challenge_method=None, device_id=None):
    env = event["envelope"] if event else {}
    return {
        "envelope": {
            "message_id": str(uuid.uuid4()),
            "correlation_id": correlation_id or env.get("correlation_id", "c-x"),
            "causation_id": None,
            "agent_id": "agent:test",
            "agent_version": "dev",
            "config_version": "n/a",
            "event_time": datetime.fromtimestamp(T0 / 1000, tz=timezone.utc),
            "device_id": device_id or env["device_id"],
            "customer_id": env.get("customer_id"),
            "device_seq": None,
            "required_context_seq": None,
            "mode": "LIVE",
            "replay_run_id": None,
        },
        "action": action,
        "reason": reason,
        "challenge_method": challenge_method,
        "enrolment_task_id": enrolment_task_id,
    }


def ctrl(command, scenario=None, *, rate=None, count=None):
    return {"envelope": {"message_id": str(uuid.uuid4())}, "command": command, "scenario": scenario,
            "rate_per_sec": rate, "count": count}


def trust(s, device_id):
    s.on_policy_command(cmd(None, "ALLOW", device_id=device_id), T0)


def run_closed_loop(s, now):
    events = s.tick(now)
    for e in events:
        if e["event_type"] == "ENROL_REQUESTED":
            s.on_policy_command(cmd(e, "ALLOW"), now)
    return events


class PacingTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_RATE, 0.2)
        self.assertAlmostEqual(sum(DEFAULT_MIX.values()), 1.0)
        self.assertTrue(set(DEFAULT_MIX) <= set(SCENARIOS))

    def test_rate_paces_enrolments(self):
        s = sim(cap=20, rate=0.2)
        evs = [e for t in range(0, 10_001, 100) for e in s.tick(T0 + t)]
        self.assertEqual(sum(e["event_type"] == "ENROL_REQUESTED" for e in evs), 2)

    def test_first_tick_starts_with_an_empty_bucket(self):
        s = Simulator(DevicePool(5), random.Random(0), rate_per_sec=1.0)
        self.assertEqual(s.tick(T0), [])

    def test_never_more_than_cap_distinct_devices(self):
        s = sim(cap=5, rate=50)
        ids = {e["envelope"]["device_id"] for t in range(0, 60_000, 50) for e in run_closed_loop(s, T0 + t)}
        self.assertLessEqual(len(ids), 5)
        self.assertTrue(ids)

    def test_events_validate_against_the_avro_schema(self):
        s = sim(cap=10, rate=20)
        events = [e for t in range(0, 20_000, 100) for e in run_closed_loop(s, T0 + t)]
        self.assertTrue(events)
        for e in events:
            fastavro.validation.validate(e, DEVICE_EVENTS, raise_errors=True)

    def test_zero_rate_pauses_emission_without_stopping(self):
        s = sim()
        s.on_control(ctrl("START", rate=0.0))
        self.assertTrue(s.running)
        self.assertEqual(s.tick(T0 + 60_000), [])

    def test_non_finite_negative_or_excessive_rate_rejected_at_construction(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -1.0, MAX_RATE_PER_SEC + 0.5):
            with self.subTest(rate=bad), self.assertRaises(ValueError):
                Simulator(DevicePool(5), random.Random(0), rate_per_sec=bad)
        Simulator(DevicePool(5), random.Random(0), rate_per_sec=MAX_RATE_PER_SEC)

    def test_non_finite_or_excessive_control_rate_is_logged_and_ignored(self):
        for bad in (float("nan"), float("inf"), MAX_RATE_PER_SEC * 10):
            with self.subTest(rate=bad):
                s = sim(cap=20, rate=1.0)
                s.tick(T0)
                with self.assertLogs("dtm.simulator.engine", "WARNING"):
                    s.on_control(ctrl(None, rate=bad))
                self.assertEqual(len(s.tick(T0 + 1000)), 1)

    def test_rate_control_changes_the_pace(self):
        s = sim(cap=20, rate=0.0)
        s.on_control(ctrl(None, rate=1.0))
        self.assertEqual(len(s.tick(T0 + 1000)), 1)

    def test_device_seq_increases_per_device(self):
        s = sim(cap=1, rate=1, pending_timeout_ms=1000)
        seqs = [e["envelope"]["device_seq"] for t in range(1000, 10_000, 1000) for e in s.tick(T0 + t)]
        self.assertGreater(len(seqs), 1)
        self.assertEqual(seqs, sorted(set(seqs)))

    def test_every_enrolment_mints_a_correlation_id(self):
        s = sim(cap=5, rate=5)
        evs = s.tick(T0 + 1000)
        self.assertEqual(len(evs), 5)
        self.assertEqual(len({e["envelope"]["correlation_id"] for e in evs}), 5)


class ClosedLoopTest(unittest.TestCase):
    def test_enrolled_device_is_pending(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        self.assertEqual(enrol["event_type"], "ENROL_REQUESTED")
        self.assertEqual(s.status("dev-0001"), "PENDING")

    def test_pending_device_not_reenrolled(self):
        s = sim(cap=1, rate=1)
        first(s.tick(T0 + 1000))
        self.assertEqual([e for t in range(2000, 60_000, 1000) for e in s.tick(T0 + t)], [])

    def test_pending_device_reenrolled_after_timeout(self):
        s = sim(cap=1, rate=1, pending_timeout_ms=120_000)
        first(s.tick(T0 + 1000))
        self.assertEqual(s.tick(T0 + 100_000), [])
        again = first(s.tick(T0 + 121_000))
        self.assertEqual(again["event_type"], "ENROL_REQUESTED")

    def test_allow_marks_trusted_and_unenrol_follows_when_pool_settled(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 1100)
        self.assertEqual(s.status("dev-0001"), "TRUSTED")
        self.assertEqual(first(s.tick(T0 + 2100))["event_type"], "UNENROL_REQUESTED")

    def test_command_actions_map_to_statuses(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        for action, reason, expected in (("READ_ONLY", None, "READ_ONLY"), ("BLOCK", "UNENROLLED", "UNENROLLED"),
                                         ("BLOCK", "ROOTED", "BLOCKED"), ("BLOCK", None, "BLOCKED"),
                                         ("ALLOW", None, "TRUSTED")):
            s.on_policy_command(cmd(enrol, action, reason=reason), T0 + 1100)
            self.assertEqual(s.status("dev-0001"), expected, (action, reason))

    def test_blocked_and_unenrolled_devices_are_enrolled_again(self):
        for reason in ("UNENROLLED", "ROOTED"):
            s = sim(cap=1, rate=1)
            enrol = first(s.tick(T0 + 1000))
            s.on_policy_command(cmd(enrol, "BLOCK", reason=reason), T0 + 1100)
            self.assertEqual(first(s.tick(T0 + 2100))["event_type"], "ENROL_REQUESTED")

    def test_unenrol_event_makes_device_pending_and_mints_correlation(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 1100)
        unenrol = first(s.tick(T0 + 2100))
        self.assertEqual(s.status("dev-0001"), "PENDING")
        self.assertNotEqual(unenrol["envelope"]["correlation_id"], enrol["envelope"]["correlation_id"])

    def test_command_for_unknown_device_is_ignored(self):
        s = sim(cap=1)
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_policy_command(cmd(None, "ALLOW", device_id="dev-9999"), T0)
        self.assertEqual(s.status("dev-0001"), "NEW")

    def test_duplicate_command_is_applied_once(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        allow = cmd(enrol, "ALLOW")
        s.on_policy_command(allow, T0 + 1100)
        s.on_policy_command(cmd(enrol, "BLOCK", reason="X"), T0 + 1200)
        self.assertEqual(s.status("dev-0001"), "BLOCKED")
        s.on_policy_command(allow, T0 + 1300)
        self.assertEqual(s.status("dev-0001"), "BLOCKED")

    def test_dedupe_memory_is_bounded(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        old = cmd(enrol, "ALLOW")
        s.on_policy_command(old, T0 + 1100)
        for _ in range(10_000):
            s.on_policy_command(cmd(enrol, "READ_ONLY"), T0 + 1200)
        s.on_policy_command(cmd(enrol, "BLOCK", reason="X"), T0 + 1300)
        s.on_policy_command(old, T0 + 1400)
        self.assertEqual(s.status("dev-0001"), "TRUSTED")


class ChallengeTest(unittest.TestCase):
    def test_challenge_answer_echoes_task_and_correlation(self):
        s = sim(rng_seed=1, pass_probability=1.0)
        enrol = first(s.tick(T0 + 5000))
        c = cmd(enrol, "CHALLENGE", enrolment_task_id="t-9", correlation_id=enrol["envelope"]["correlation_id"])
        s.on_policy_command(c, T0 + 5100)
        self.assertEqual(s.status(enrol["envelope"]["device_id"]), "CHALLENGED")
        self.assertEqual(s.tick(T0 + 6000), [])
        ans = first(s.tick(T0 + 7200))
        self.assertEqual((ans["event_type"], ans["enrolment_task_id"]), ("CHALLENGE_PASSED", "t-9"))
        self.assertEqual(ans["envelope"]["correlation_id"], enrol["envelope"]["correlation_id"])
        self.assertEqual(ans["envelope"]["causation_id"], c["envelope"]["message_id"])
        self.assertEqual(ans["envelope"]["device_id"], enrol["envelope"]["device_id"])
        self.assertEqual(ans["fingerprint"], enrol["fingerprint"])
        fastavro.validation.validate(ans, DEVICE_EVENTS, raise_errors=True)

    def test_challenge_fails_when_pass_probability_is_zero(self):
        s = sim(pass_probability=0.0)
        enrol = first(s.tick(T0 + 5000))
        s.on_policy_command(cmd(enrol, "CHALLENGE", enrolment_task_id="t-1"), T0 + 5100)
        self.assertEqual(first(s.tick(T0 + 7200))["event_type"], "CHALLENGE_FAILED")

    def test_challenge_without_task_id_not_answered(self):
        s = sim(pass_probability=1.0)
        enrol = first(s.tick(T0 + 5000))
        for task_id in (None, ""):
            with self.assertLogs("dtm.simulator.engine", "WARNING"):
                s.on_policy_command(cmd(enrol, "CHALLENGE", enrolment_task_id=task_id), T0 + 5100)
        answers = [e for t in range(5200, 20_000, 100) for e in s.tick(T0 + t) if e["event_type"].startswith("CHALLENGE_")]
        self.assertEqual(answers, [])
        self.assertEqual(s.status(enrol["envelope"]["device_id"]), "CHALLENGED")

    def test_challenge_answered_once(self):
        s = sim(pass_probability=1.0)
        enrol = first(s.tick(T0 + 5000))
        c = cmd(enrol, "CHALLENGE", enrolment_task_id="t-9")
        s.on_policy_command(c, T0)
        s.on_policy_command(c, T0 + 10)
        answers = [e for t in range(5000, 15_000, 100) for e in s.tick(T0 + t) if e["event_type"].startswith("CHALLENGE_")]
        self.assertEqual(len(answers), 1)

    def test_answer_is_dropped_when_the_device_has_moved_on(self):
        s = sim(pass_probability=1.0)
        enrol = first(s.tick(T0 + 5000))
        s.on_policy_command(cmd(enrol, "CHALLENGE", enrolment_task_id="t-9"), T0 + 5100)
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 5200)
        answers = [e for t in range(5200, 15_000, 100) for e in s.tick(T0 + t) if e["event_type"].startswith("CHALLENGE_")]
        self.assertEqual(answers, [])


class ScenarioTest(unittest.TestCase):
    def test_inject_scenarios(self):
        for scen, check in (("COMPROMISED_OS", lambda e: e["fingerprint"]["rooted"]),
                            ("OUTDATED_OS", lambda e: e["fingerprint"]["os_version"] in ("15.7", "11"))):
            s = sim()
            s.on_control(ctrl("INJECT", scen, count=1))
            self.assertTrue(check(first(s.tick(T0 + 5000))), scen)

    def test_challenge_scenario_uses_an_outdated_fingerprint(self):
        s = sim()
        s.on_control(ctrl("INJECT", "CHALLENGE"))
        ev = first(s.tick(T0 + 5000))
        self.assertEqual(ev["event_type"], "ENROL_REQUESTED")
        self.assertIn(ev["fingerprint"]["os_version"], ("15.7", "11"))

    def test_unenrol_scenario_unenrols_a_trusted_device(self):
        s = sim(cap=3)
        trust(s, "dev-0002")
        s.on_control(ctrl("INJECT", "UNENROL"))
        ev = first(s.tick(T0 + 5000))
        self.assertEqual((ev["event_type"], ev["envelope"]["device_id"]), ("UNENROL_REQUESTED", "dev-0002"))

    def test_unenrol_scenario_without_a_trusted_device_enrols(self):
        s = sim(cap=3)
        s.on_control(ctrl("INJECT", "UNENROL"))
        self.assertEqual(first(s.tick(T0 + 5000))["event_type"], "ENROL_REQUESTED")

    def test_new_device_scenario_picks_established_customer_with_trusted_device(self):
        s = sim(cap=3)
        trust(s, "dev-0001")
        s.on_control(ctrl("INJECT", "NEW_DEVICE_ESTABLISHED_ACCOUNT"))
        ev = first(s.tick(T0 + 5000))
        self.assertIn(ev["envelope"]["device_id"], ("dev-0002", "dev-0003"))
        self.assertGreater(ev["account_age_days"], 90)

    def test_impossible_travel_uses_far_country_after_recent_customer_event(self):
        s = sim(cap=3)
        enrol = first(s.tick(T0 + 5000))
        s.on_control(ctrl("INJECT", "IMPOSSIBLE_TRAVEL"))
        ev = first(s.tick(T0 + 10_000))
        self.assertEqual(ev["envelope"]["customer_id"], enrol["envelope"]["customer_id"])
        self.assertNotEqual(ev["geo"]["country"], enrol["geo"]["country"])
        self.assertEqual(ev["geo"]["country"], "AU")

    def test_impossible_travel_needs_a_recent_event(self):
        s = sim(cap=3)
        first(s.tick(T0 + 5000))
        s.on_control(ctrl("INJECT", "IMPOSSIBLE_TRAVEL"))
        ev = first(s.tick(T0 + 5000 + 11 * 60_000))
        self.assertNotEqual(ev["geo"]["country"], "AU")

    def test_unfeasible_scenario_falls_back_to_clean(self):
        s = sim(cap=1)
        s.on_control(ctrl("INJECT", "NEW_DEVICE_ESTABLISHED_ACCOUNT"))
        self.assertFalse(first(s.tick(T0 + 5000))["fingerprint"]["rooted"])

    def test_inject_count_queues_scenarios_ahead_of_the_mix(self):
        s = sim(cap=20, rate=10)
        s.on_control(ctrl("INJECT", "COMPROMISED_OS", count=3))
        evs = s.tick(T0 + 1000)
        self.assertEqual([e["fingerprint"]["rooted"] for e in evs[:3]], [True, True, True])

    def test_inject_without_scenario_is_ignored(self):
        s = sim()
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_control(ctrl("INJECT"))
        self.assertEqual(s.tick(T0 + 1000), [])

    def test_inject_of_unknown_scenario_is_ignored(self):
        s = sim()
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_control(ctrl("INJECT", "NOPE"))
        self.assertEqual(s.tick(T0 + 5000)[0]["event_type"], "ENROL_REQUESTED")

    def test_stop_and_start(self):
        s = sim()
        s.on_control(ctrl("STOP"))
        self.assertFalse(s.running)
        self.assertEqual(s.tick(T0 + 60_000), [])
        s.on_control(ctrl("START"))
        self.assertTrue(s.running)
        self.assertTrue(s.tick(T0 + 70_000))


class FixRoundTest(unittest.TestCase):
    def test_pool_of_read_only_devices_is_unenrolled(self):
        s = sim(cap=3, rate=5)
        for i in (1, 2, 3):
            s.on_policy_command(cmd(None, "READ_ONLY", device_id=f"dev-000{i}"), T0)
        evs = [e for t in range(1000, 60_000, 1000) for e in s.tick(T0 + t)]
        self.assertTrue(evs)
        self.assertEqual({e["event_type"] for e in evs[:3]}, {"UNENROL_REQUESTED"})

    def test_unresolved_challenged_device_is_reenrolled_after_the_timeout_only(self):
        s = sim(cap=1, rate=1, pending_timeout_ms=60_000)
        enrol = first(s.tick(T0 + 1000))
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_policy_command(cmd(enrol, "CHALLENGE"), T0 + 2000)
        self.assertEqual([e for t in range(3000, 61_000, 1000) for e in s.tick(T0 + t)], [])
        again = first(s.tick(T0 + 62_000))
        self.assertEqual(again["event_type"], "ENROL_REQUESTED")

    def test_resolved_challenged_device_is_not_reenrolled(self):
        s = sim(cap=1, rate=1, pending_timeout_ms=60_000)
        enrol = first(s.tick(T0 + 1000))
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_policy_command(cmd(enrol, "CHALLENGE"), T0 + 2000)
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 3000)
        evs = [e for t in range(4000, 60_000, 1000) for e in s.tick(T0 + t)]
        self.assertNotIn("ENROL_REQUESTED", [e["event_type"] for e in evs])

    def test_inject_count_is_clamped(self):
        s = sim(cap=20, rate=1)
        with self.assertLogs("dtm.simulator.engine", "WARNING"):
            s.on_control(ctrl("INJECT", "COMPROMISED_OS", count=MAX_INJECT_COUNT + 500))
        self.assertEqual(len(s._injected), MAX_INJECT_COUNT)

    def test_repeated_impossible_travel_does_not_reuse_the_last_country(self):
        s = sim(cap=3)
        first(s.tick(T0 + 5000))
        s.on_control(ctrl("INJECT", "IMPOSSIBLE_TRAVEL", count=2))
        one = first(s.tick(T0 + 10_000))
        two = first(s.tick(T0 + 15_000))
        self.assertEqual(one["geo"]["country"], "AU")
        self.assertNotEqual(two["geo"]["country"], "AU")
        self.assertEqual(two["envelope"]["customer_id"], one["envelope"]["customer_id"])

    def test_unenrol_reuses_the_enrolled_fingerprint(self):
        s = sim(cap=1, rate=1)
        s.on_control(ctrl("INJECT", "OUTDATED_OS"))
        enrol = first(s.tick(T0 + 1000))
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 1100)
        unenrol = first(s.tick(T0 + 2100))
        self.assertEqual(unenrol["event_type"], "UNENROL_REQUESTED")
        self.assertEqual(unenrol["fingerprint"], enrol["fingerprint"])
        self.assertIn(unenrol["fingerprint"]["os_version"], ("15.7", "11"))

    def test_idle_tokens_log_once_per_transition(self):
        s = sim(cap=1, rate=1)
        enrol = first(s.tick(T0 + 1000))
        with self.assertLogs("dtm.simulator.engine", "INFO") as logs:
            for t in range(2000, 10_000, 1000):
                s.tick(T0 + t)
        self.assertEqual(len([r for r in logs.records if "no device" in r.getMessage()]), 1)
        s.tick(T0 + 10_000)
        s.on_policy_command(cmd(enrol, "ALLOW"), T0 + 10_100)
        first(s.tick(T0 + 11_000))        # unenrol emitted: the idle state ends
        with self.assertLogs("dtm.simulator.engine", "INFO") as logs:
            for t in range(12_000, 20_000, 1000):
                s.tick(T0 + t)
        self.assertEqual(len([r for r in logs.records if "no device" in r.getMessage()]), 1)


class RunnerImportTest(unittest.TestCase):
    def test_runner_imports_and_requires_device_cap(self):
        from dtm.simulator import runner
        with self.assertRaises(ValueError):
            runner.run({})

    def test_publish_skips_events_that_fail_to_serialize_or_queue(self):
        from confluent_kafka import KafkaException
        from confluent_kafka.serialization import SerializationError
        from dtm.simulator.runner import _publish
        event = first(sim(cap=1, rate=1).tick(T0 + 1000))

        class Producer:
            def __init__(self, errors):
                self.errors, self.sent, self.polls = list(errors), [], 0

            def produce(self, topic, key, value, on_delivery):
                if self.errors:
                    raise self.errors.pop(0)
                self.sent.append((topic, key, value))

            def poll(self, timeout):
                self.polls += 1

        def serialize(_event, _ctx):
            return b"v"

        def broken(_event, _ctx):
            raise SerializationError("no schema")

        ok = Producer([])
        self.assertTrue(_publish(ok, serialize, event, None))
        self.assertEqual(ok.sent, [("device.events", event["envelope"]["device_id"].encode(), b"v")])
        retried = Producer([BufferError()])
        self.assertTrue(_publish(retried, serialize, event, None))
        self.assertEqual((len(retried.sent), retried.polls), (1, 1))
        with self.assertLogs("dtm.simulator.runner", "ERROR"):
            self.assertFalse(_publish(Producer([BufferError(), BufferError()]), serialize, event, None))
            self.assertFalse(_publish(Producer([KafkaException("boom")]), serialize, event, None))
            self.assertFalse(_publish(Producer([]), broken, event, None))
