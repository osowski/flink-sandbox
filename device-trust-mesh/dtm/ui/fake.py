"""A synthetic mesh for ``python -m dtm.ui --fake``, so the page can be checked without a cluster.

About every ``EVENT_INTERVAL_MS`` one device sends an enrolment event, the pace drifting slowly over a
``RATE_PERIOD_MS`` cycle so the pair sparklines have a shape, and the conversation it starts plays out
as the same records the real sources hand the board: ``/v1/devices`` rows every 500 ms, view records with
an ``envelope`` for tasks, decisions and statuses, ``agent.decision_latency`` rows for enrolment decisions
and an ``agent.pair_counts`` window every 5 s. The first leg runs router → risk → enrolment, and enrolment
sends to compliance and, for a challenge, to step-up; the router resumes a challenged enrolment by sending
to enrolment once the device answers. Nothing here touches Kafka or HTTP.
"""
import heapq
import itertools
import logging
import math
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from dtm.ui.model import Board, Coord, Delta, percentile

ROUTER = "dtm-a2a-task-router"
TASKS = "agent.tasks_human_readable"
DECISIONS = "agent.decisions_human_readable"
STATUSES = "agent.task-status_human_readable"
PAIR_COUNTS = "agent.pair_counts"
DECISION_LATENCY = "agent.decision_latency"

EVENT_INTERVAL_MS = 1500
RATE_PERIOD_MS = 120_000
POLL_INTERVAL_MS = 500
WINDOW_SLIDE_MS = 5000
WINDOW_SIZE_MS = 60_000
LAG_INTERVAL_MS = 5000
CHALLENGE_ANSWER_MS = 6000
PARTITIONS = 3
SEED = 7
_STOP_JOIN_S = 5.0

log = logging.getLogger(__name__)

# Played in turn, so every display status shows up within the first few events.
SCENARIOS = ("CLEAN", "OUTDATED_OS", "COMPROMISED_OS", "CLEAN", "NEW_DEVICE_ESTABLISHED_ACCOUNT",
             "CLEAN", "IMPOSSIBLE_TRAVEL", "CLEAN")
_BLOCKING = {"COMPROMISED_OS": "Compromised OS fingerprint", "IMPOSSIBLE_TRAVEL": "Impossible travel"}

Action = Callable[[int], list[Delta]]


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class FakeFeed:
    def __init__(self, board: Board, publish: Callable[[list[Delta]], None],
                 clock: Callable[[], float] = time.time, devices: int = 20, rate_hz: float = 4.0):
        """``rate_hz`` is how often the background thread advances the synthetic mesh."""
        self._board = board
        self._publish = publish
        self._clock = clock
        self._device_count = devices
        self._interval_s = 1 / rate_hz
        self._rng = random.Random(SEED)
        self._queue: list[tuple[int, int, Action]] = []
        self._order = itertools.count()
        self._offsets: dict[tuple[str, int], int] = {}
        self._context: dict[str, dict] = {}
        self._partition: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self._busy: set[str] = set()
        self._sent: deque[tuple[int, str, str]] = deque()
        self._lags: deque[int] = deque(maxlen=200)
        self._scenarios = itertools.cycle(SCENARIOS)
        self._now_ms: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run, name="ui-fake-feed", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(_STOP_JOIN_S)

    def step(self, now_s: float) -> None:
        """Play every synthetic record due by ``now_s`` into the board and publish the deltas in one batch."""
        now_ms = round(now_s * 1000)
        if self._now_ms is None:
            self._begin(now_ms)
        self._now_ms = max(self._now_ms, now_ms)
        deltas: list[Delta] = []
        while self._queue and self._queue[0][0] <= self._now_ms:
            due_ms, _, action = heapq.heappop(self._queue)
            deltas += action(due_ms)
        if deltas:
            self._publish(deltas)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(self._clock())
            except Exception:
                log.exception("fake feed step failed")
            self._stop.wait(self._interval_s)

    def _at(self, due_ms: int, action: Action) -> None:
        heapq.heappush(self._queue, (due_ms, next(self._order), action))

    def _begin(self, now_ms: int) -> None:
        self._now_ms = now_ms
        for i in range(self._device_count):
            device_id = f"dev-{i + 1:04d}"
            pending = i % 5 == 4
            self._partition[device_id] = i % PARTITIONS
            self._seq[device_id] = 1
            self._context[device_id] = {
                "device_id": device_id, "customer_id": f"cust-{i // 2 + 1:03d}",
                "trust_status": "PENDING" if pending else "TRUSTED",
                "last_event_type": "ENROL_REQUESTED", "context_seq": 1,
                "last_event_time": now_ms - self._rng.randint(10_000, 120_000)}
        self._at(now_ms, self._poll)
        self._at(now_ms + 500, self._device_event)
        self._at(now_ms - now_ms % WINDOW_SLIDE_MS + WINDOW_SLIDE_MS, self._pair_window)
        self._at(now_ms + LAG_INTERVAL_MS, self._context_lag)

    def _poll(self, due_ms: int) -> list[Delta]:
        self._at(due_ms + POLL_INTERVAL_MS, self._poll)
        return self._board.apply_devices([dict(r) for r in self._context.values()], due_ms)

    def _context_update(self, device_id: str, **fields) -> Action:
        def apply(due_ms: int) -> list[Delta]:
            self._context[device_id].update(fields)
            return []
        return apply

    def _materialize(self, at_ms: int, device_id: str, **fields) -> None:
        lag = self._rng.randint(150, 600)
        self._lags.append(lag)
        self._at(at_ms + lag, self._context_update(device_id, **fields))

    def _device_event(self, due_ms: int) -> list[Delta]:
        drift = 1 + 0.4 * math.sin(2 * math.pi * due_ms / RATE_PERIOD_MS)
        self._at(due_ms + round(EVENT_INTERVAL_MS * drift) + self._rng.randint(-300, 300), self._device_event)
        idle = sorted(set(self._context) - self._busy)
        if not idle:
            return []
        device_id = self._rng.choice(idle)
        self._busy.add(device_id)
        scenario = next(self._scenarios)
        seq = self._next_seq(device_id)
        fields = {"context_seq": seq, "last_event_type": "ENROL_REQUESTED", "last_event_time": due_ms}
        if scenario == "NEW_DEVICE_ESTABLISHED_ACCOUNT":
            fields["trust_status"] = "PENDING"
        self._materialize(due_ms, device_id, **fields)
        self._conversation(device_id, scenario, seq, due_ms)
        return []

    def _conversation(self, device_id: str, scenario: str, seq: int, t: int) -> None:
        """Schedule the records one enrolment event produces, starting from its event time ``t``."""
        corr = str(uuid.uuid4())
        to_risk = self._task(t, device_id, seq, corr, ROUTER, "risk", "ASSESS_RISK")
        score = 85 if scenario in _BLOCKING else 55 if scenario == "OUTDATED_OS" else 12
        assessed_at = t + self._jitter(500)
        self._reply(assessed_at, device_id, seq, corr, to_risk, "risk", "risk-assessment",
                    outcome=None, policy_action=None, risk_score=score, rationale=f"{scenario} signals")
        to_enrolment = self._task(assessed_at + 100, device_id, seq, corr, "risk", "enrolment", "DECIDE_ENROLMENT")
        decide_at = t + self._jitter(1800)
        to_compliance = self._task(decide_at - 500, device_id, seq, corr, "enrolment", "compliance", "AUDIT")
        self._reply(decide_at - 200, device_id, seq, corr, to_compliance, "compliance", "audit-record",
                    outcome="RECORDED", policy_action=None, risk_score=None, rationale="audit written")
        if scenario == "NEW_DEVICE_ESTABLISHED_ACCOUNT":
            decide_at += 6000
        if scenario == "OUTDATED_OS":
            self._decide(decide_at, device_id, seq, corr, to_enrolment, t, "CHALLENGED", "CHALLENGE",
                         "Outdated OS; step-up required", "CHALLENGED", "TASK_STATE_AUTH_REQUIRED")
            to_step_up = self._task(decide_at + 200, device_id, seq, corr, "enrolment", "step-up", "STEP_UP")
            self._reply(decide_at + self._jitter(600), device_id, seq, corr, to_step_up, "step-up",
                        "step-up-challenge", outcome=None, policy_action=None, risk_score=None,
                        rationale="push challenge sent")
            self._at(decide_at + CHALLENGE_ANSWER_MS, lambda due_ms: self._answer(device_id, corr, due_ms))
            return
        if scenario in _BLOCKING:
            outcome, action, rationale, status = "DENIED", "BLOCK", _BLOCKING[scenario], "BLOCKED"
        else:
            outcome, action, rationale, status = "GRANTED", "ALLOW", "Clean device", "TRUSTED"
        self._decide(decide_at, device_id, seq, corr, to_enrolment, t, outcome, action, rationale, status,
                     "TASK_STATE_COMPLETED")
        self._at(decide_at + 1000, self._release(device_id))

    def _answer(self, device_id: str, corr: str, t: int) -> list[Delta]:
        """The device passes its step-up challenge; the router resumes the enrolment."""
        seq = self._next_seq(device_id)
        self._materialize(t, device_id, context_seq=seq, last_event_type="CHALLENGE_PASSED", last_event_time=t)
        resume = self._task(t, device_id, seq, corr, ROUTER, "enrolment", "RESOLVE_CHALLENGE")
        decide_at = t + self._jitter(700)
        self._decide(decide_at, device_id, seq, corr, resume, t, "GRANTED", "ALLOW",
                     "Step-up challenge passed", "TRUSTED", "TASK_STATE_COMPLETED")
        self._at(decide_at + 1000, self._release(device_id))
        return []

    def _release(self, device_id: str) -> Action:
        def release(due_ms: int) -> list[Delta]:
            self._busy.discard(device_id)
            return []
        return release

    def _next_seq(self, device_id: str) -> int:
        """Return the device's next context sequence; context-mcp shows it once the event materializes."""
        self._seq[device_id] += 1
        return self._seq[device_id]

    def _jitter(self, ms: int) -> int:
        return ms + self._rng.randint(-100, 100)

    def _envelope(self, message_id: str, causation_id: str | None, agent_id: str, at_ms: int,
                  device_id: str, seq: int, corr: str) -> dict:
        return {"message_id": message_id, "correlation_id": corr, "causation_id": causation_id,
                "agent_id": agent_id, "agent_version": "1", "config_version": "fake",
                "event_time": _dt(at_ms), "device_id": device_id,
                "customer_id": self._context[device_id]["customer_id"], "device_seq": seq,
                "required_context_seq": seq, "mode": "LIVE", "replay_run_id": None}

    def _coord(self, topic: str, device_id: str, at_ms: int) -> Coord:
        key = (topic, self._partition[device_id])
        offset = self._offsets.get(key, 0)
        self._offsets[key] = offset + 1
        return Coord(topic, key[1], offset, at_ms)

    def _task(self, at_ms: int, device_id: str, seq: int, corr: str, from_agent: str, to_agent: str,
              task_type: str) -> str:
        message_id = str(uuid.uuid4())
        agent_id = f"flink:{ROUTER}" if from_agent == ROUTER else f"agent:{from_agent}"
        value = {"envelope": self._envelope(message_id, None, agent_id, at_ms, device_id, seq, corr),
                 "from_agent": from_agent, "to_agent": to_agent, "task_type": task_type, "task_id": None,
                 "message_id": message_id, "reference_task_ids": None, "summary": f"{task_type} {device_id}"}

        def send(due_ms: int) -> list[Delta]:
            self._sent.append((due_ms, from_agent, to_agent))
            return self._board.apply_task(value, self._coord(TASKS, device_id, due_ms))
        self._at(at_ms, send)
        return message_id

    def _reply(self, at_ms: int, device_id: str, seq: int, corr: str, causation_id: str, agent: str,
               artifact_name: str, *, outcome: str | None, policy_action: str | None,
               risk_score: int | None, rationale: str, state: str = "TASK_STATE_COMPLETED") -> None:
        task_id = str(uuid.uuid4())
        decision = {"envelope": self._envelope(str(uuid.uuid4()), causation_id, f"agent:{agent}", at_ms,
                                               device_id, seq, corr),
                    "agent": agent, "task_id": task_id, "artifact_name": artifact_name, "outcome": outcome,
                    "policy_action": policy_action, "risk_score": risk_score, "rationale": rationale,
                    "signal_ids": None, "method": None}
        status = {"envelope": self._envelope(str(uuid.uuid4()), causation_id, f"agent:{agent}", at_ms + 5,
                                             device_id, seq, corr),
                  "agent": agent, "task_id": task_id, "state": state, "status_time": None, "status_text": None}
        self._at(at_ms, lambda due_ms: self._board.apply_decision(decision,
                                                                  self._coord(DECISIONS, device_id, due_ms)))
        self._at(at_ms + 5, lambda due_ms: self._board.apply_status(status,
                                                                    self._coord(STATUSES, device_id, due_ms)))

    def _decide(self, at_ms: int, device_id: str, seq: int, corr: str, causation_id: str, event_ms: int,
                outcome: str, policy_action: str, rationale: str, trust_status: str, state: str) -> None:
        """Schedule an enrolment decision, its latency row and the trust status it lands in context."""
        self._reply(at_ms, device_id, seq, corr, causation_id, "enrolment", "enrolment-decision",
                    outcome=outcome, policy_action=policy_action, risk_score=None, rationale=rationale,
                    state=state)
        row = {"device_id": device_id, "customer_id": self._context[device_id]["customer_id"],
               "correlation_id": corr, "device_seq": seq, "agent": "enrolment", "task_id": None,
               "artifact_name": "enrolment-decision", "outcome": outcome, "policy_action": policy_action,
               "rationale": rationale, "event_time": _dt(event_ms), "decision_time": _dt(at_ms),
               "latency_ms": at_ms - event_ms}
        self._at(at_ms + 300, lambda due_ms: self._board.apply_latency(
            row, self._coord(DECISION_LATENCY, device_id, due_ms)))
        self._materialize(at_ms, device_id, trust_status=trust_status)

    def _pair_window(self, due_ms: int) -> list[Delta]:
        self._at(due_ms + WINDOW_SLIDE_MS, self._pair_window)
        start = due_ms - WINDOW_SIZE_MS
        while self._sent and self._sent[0][0] < start:
            self._sent.popleft()
        counts: dict[tuple[str, str], int] = {}
        for at_ms, from_agent, to_agent in self._sent:
            if at_ms < due_ms:
                counts[(from_agent, to_agent)] = counts.get((from_agent, to_agent), 0) + 1
        # Each row returns the whole ranking, so only the last one's delta is needed.
        deltas: list[Delta] = []
        for (from_agent, to_agent), count in sorted(counts.items()):
            value = {"window_start": _dt(start), "window_end": _dt(due_ms), "from_agent": from_agent,
                     "to_agent": to_agent, "message_count": count}
            deltas = self._board.apply_pair_counts(value, Coord(PAIR_COUNTS, 0, 0, due_ms))
        return deltas

    def _context_lag(self, due_ms: int) -> list[Delta]:
        self._at(due_ms + LAG_INTERVAL_MS, self._context_lag)
        lags = list(self._lags)
        return self._board.apply_context_lag(percentile(lags, 50), percentile(lags, 95))
