"""The in-memory board behind the UI: devices, the message feed, busiest pairs and the summary strip.

Each ``apply_*`` method folds one input into the board and returns the deltas a browser needs to stay
current, as ``(type, payload)`` pairs. One lock guards every structure, so the Kafka consumer, the
context poller and the SSE handlers can all call in from their own threads.
"""
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable

from dtm.common.envelope import to_epoch_ms

ENROLMENT_DECISION = "enrolment-decision"
STALE_SOURCES = ("kafka", "context")
RATIONALE_CHARS = 80
REQUESTER_FACTOR = 4

Delta = tuple[str, dict]

_STATUS_DISPLAY = {
    "BLOCKED": ("BLOCKED", "red"),
    "UNENROLLED": ("UNENROLLED", "red"),
    "TRUSTED": ("TRUSTED", "green"),
    "CHALLENGED": ("STEP-UP", "amber"),
    "READ_ONLY": ("READ-ONLY", "amber"),
    "PENDING": ("PENDING", "grey"),
    "UNKNOWN": ("UNKNOWN", "grey"),
}
_CLEARED_FROM = ("CHALLENGED", "PENDING")
_COLORS = ("red", "green", "amber", "grey")


@dataclass(frozen=True)
class Coord:
    """Where a Kafka record sits: its topic, partition, offset and record timestamp."""
    topic: str
    partition: int
    offset: int
    timestamp_ms: int


def display_status(trust_status: str | None, previous: str | None) -> tuple[str, str]:
    """Return the ``(label, color)`` for a trust status; TRUSTED after CHALLENGED or PENDING is CLEARED."""
    if trust_status == "TRUSTED" and previous in _CLEARED_FROM:
        return "CLEARED", "green"
    if trust_status is None:
        return _STATUS_DISPLAY["UNKNOWN"]
    return _STATUS_DISPLAY.get(trust_status, (trust_status, "grey"))


def percentile(values: list[float], q: float) -> float | None:
    """Return the nearest-rank percentile ``q`` (0-100) of ``values``, or None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _reason(value: dict) -> str | None:
    head = " · ".join(p for p in (value.get("outcome"), value.get("policy_action")) if p)
    rationale = (value.get("rationale") or "")[:RATIONALE_CHARS]
    if head and rationale:
        return f"{head} — {rationale}"
    return head or rationale or None


class Board:
    def __init__(self, clock: Callable[[], float] = time.time, feed_limit: int = 500,
                 window_ms: int = 300_000, history_windows: int = 12):
        self._clock = clock
        self._feed_limit = feed_limit
        self._window_ms = window_ms
        self._history_windows = history_windows
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        self._previous_status: dict[str, str | None] = {}
        self._decided_at: dict[str, int] = {}
        self._pending_latency: OrderedDict[str, dict] = OrderedDict()
        self._polled = False
        self._feed: deque[dict] = deque(maxlen=feed_limit)
        self._requesters: OrderedDict[str, str | None] = OrderedDict()
        self._pair_windows: dict[int, dict[tuple[str, str], int]] = {}
        self._decision_window: deque[tuple[int, float]] = deque()
        self._freshness_window: deque[tuple[int, float]] = deque()
        self._context_lag: tuple[float | None, float | None] = (None, None)
        self._stale: dict[str, int | None] = dict.fromkeys(STALE_SOURCES)

    def apply_devices(self, rows: list[dict], polled_at_ms: int) -> list[Delta]:
        """Fold in a ``/v1/devices`` poll; the first poll is a baseline and measures no freshness."""
        with self._lock:
            baseline = not self._polled
            self._polled = True
            deltas: list[Delta] = []
            for row in rows:
                changed = self._apply_device_row(row, polled_at_ms, baseline)
                if changed is not None:
                    deltas.append(("device", dict(changed)))
            deltas.append(("summary", self._summary()))
            return deltas

    def apply_task(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.tasks_human_readable`` row and remember who asked, for later replies."""
        with self._lock:
            envelope = value["envelope"]
            message_id = value.get("message_id") or envelope["message_id"]
            self._requesters[message_id] = value.get("from_agent")
            while len(self._requesters) > self._feed_limit * REQUESTER_FACTOR:
                self._requesters.popitem(last=False)
            return [self._message("task", value, value.get("from_agent"), value.get("to_agent"),
                                  message_id, coord)]

    def apply_decision(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.decisions_human_readable`` row, targeted at the agent that asked."""
        return self._apply_reply("decision", value, coord)

    def apply_status(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.task-status_human_readable`` row, targeted at the agent that asked."""
        return self._apply_reply("status", value, coord)

    def apply_pair_counts(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.pair_counts`` row; a re-delivered row replaces its count."""
        with self._lock:
            window_end = to_epoch_ms(value["window_end"])
            pairs = (value["from_agent"], value["to_agent"])
            self._pair_windows.setdefault(window_end, {})[pairs] = value["message_count"]
            for stale in sorted(self._pair_windows)[:-self._history_windows]:
                del self._pair_windows[stale]
            return [("pairs", self._pairs())]

    def apply_latency(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.decision_latency`` row; only enrolment decisions reach the board.

        A row older than the device's latest decision is a replay and is ignored. A row for a device
        the board has not listed yet waits and is applied when the device first appears.
        """
        with self._lock:
            if value["artifact_name"] != ENROLMENT_DECISION:
                return []
            device_id = value["device_id"]
            decided_at = to_epoch_ms(value["decision_time"])
            if decided_at < self._decided_at.get(device_id, decided_at):
                return []
            self._decided_at[device_id] = decided_at
            applied = {"latency_ms": value["latency_ms"], "reason": _reason(value)}
            if value["latency_ms"] is not None:
                self._observe(self._decision_window, value["latency_ms"])
            row = self._devices.get(device_id)
            if row is None:
                self._pending_latency[device_id] = applied
                self._pending_latency.move_to_end(device_id)
                while len(self._pending_latency) > self._feed_limit:
                    self._pending_latency.popitem(last=False)
                return [("summary", self._summary())]
            row.update(applied)
            return [("device", dict(row)), ("summary", self._summary())]

    def apply_context_lag(self, p50_ms: float | None, p95_ms: float | None) -> list[Delta]:
        """Record context-mcp's own materialization lag percentiles."""
        with self._lock:
            self._context_lag = (p50_ms, p95_ms)
            return [("summary", self._summary())]

    def mark_stale(self, source: str, since_ms: int) -> list[Delta]:
        """Flag ``source`` ("kafka" or "context") as stale since ``since_ms``; a repeat is a no-op."""
        with self._lock:
            self._check_source(source)
            if self._stale[source] is not None:
                return []
            self._stale[source] = since_ms
            return [("stale", {"source": source, "since_ms": since_ms})]

    def mark_fresh(self, source: str) -> list[Delta]:
        """Clear the stale flag on ``source``; a repeat is a no-op."""
        with self._lock:
            self._check_source(source)
            if self._stale[source] is None:
                return []
            self._stale[source] = None
            return [("stale", {"source": source, "since_ms": None})]

    def snapshot(self) -> dict:
        """Return everything a new client needs: devices (latest change first), feed (newest first), pairs, summary and stale flags."""
        with self._lock:
            devices = sorted(self._devices.values(), key=lambda r: (-r["changed_at_ms"], r["device_id"]))
            return {"devices": [dict(r) for r in devices],
                    "feed": [dict(m) for m in reversed(self._feed)],
                    "pairs": self._pairs(), "summary": self._summary(), "stale": dict(self._stale)}

    def _apply_device_row(self, row: dict, polled_at_ms: int, baseline: bool) -> dict | None:
        device_id = row["device_id"]
        seq = row["context_seq"]
        known = self._devices.get(device_id)
        core = {"customer_id": row["customer_id"], "trust_status": row["trust_status"],
                "context_seq": seq, "last_event_time_ms": to_epoch_ms(row["last_event_time"])}
        if known is not None and all(known[k] == v for k, v in core.items()):
            return None
        status_changed = known is None or known["trust_status"] != core["trust_status"]
        if known is not None and status_changed:
            self._previous_status[device_id] = known["trust_status"]
        advanced = (known is None or known["context_seq"] is None
                    or (seq is not None and seq > known["context_seq"]))
        freshness = known["freshness_ms"] if known is not None else None
        if advanced and not baseline and core["last_event_time_ms"] is not None:
            freshness = max(0, polled_at_ms - core["last_event_time_ms"])
            self._observe(self._freshness_window, freshness)
        label, color = display_status(core["trust_status"], self._previous_status.get(device_id))
        latency = known if known is not None else self._pending_latency.pop(device_id, {})
        self._devices[device_id] = {
            "device_id": device_id, **core, "label": label, "color": color,
            "reason": latency.get("reason"), "latency_ms": latency.get("latency_ms"),
            "freshness_ms": freshness,
            "changed_at_ms": polled_at_ms if status_changed else known["changed_at_ms"]}
        return self._devices[device_id]

    def _apply_reply(self, kind: str, value: dict, coord: Coord) -> list[Delta]:
        with self._lock:
            envelope = value["envelope"]
            target = self._requesters.get(envelope.get("causation_id"))
            return [self._message(kind, value, value.get("agent"), target, envelope["message_id"], coord)]

    def _message(self, kind: str, value: dict, source: str | None, target: str | None,
                 message_id: str, coord: Coord) -> Delta:
        envelope = value["envelope"]
        message = {"kind": kind, "time_ms": to_epoch_ms(envelope["event_time"]), "source": source,
                   "target": target, "device_id": envelope["device_id"], "task_id": value.get("task_id"),
                   "message_id": message_id, "topic": coord.topic, "partition": coord.partition,
                   "offset": coord.offset}
        self._feed.append(message)
        return "message", dict(message)

    def _observe(self, window: deque, value: float) -> None:
        window.append((self._now_ms(), value))
        self._prune(window)

    def _prune(self, window: deque) -> None:
        cutoff = self._now_ms() - self._window_ms
        while window and window[0][0] < cutoff:
            window.popleft()

    def _now_ms(self) -> int:
        return round(self._clock() * 1000)

    def _pairs(self) -> dict:
        if not self._pair_windows:
            return {"window_end_ms": None, "ranking": []}
        windows = sorted(self._pair_windows)
        newest = self._pair_windows[windows[-1]]
        ranking = [{"from_agent": f, "to_agent": t, "message_count": count,
                    "history": [self._pair_windows[w].get((f, t), 0) for w in windows]}
                   for (f, t), count in newest.items()]
        ranking.sort(key=lambda r: (-r["message_count"], r["from_agent"], r["to_agent"]))
        return {"window_end_ms": windows[-1], "ranking": ranking}

    def _summary(self) -> dict:
        self._prune(self._decision_window)
        self._prune(self._freshness_window)
        decisions = [v for _, v in self._decision_window]
        freshness = [v for _, v in self._freshness_window]
        by_color = dict.fromkeys(_COLORS, 0)
        for row in self._devices.values():
            by_color[row["color"]] += 1
        return {"device_count": len(self._devices), "by_color": by_color,
                "decision_p50_ms": percentile(decisions, 50), "decision_p95_ms": percentile(decisions, 95),
                "freshness_p50_ms": percentile(freshness, 50), "freshness_p95_ms": percentile(freshness, 95),
                "context_lag_p50_ms": self._context_lag[0], "context_lag_p95_ms": self._context_lag[1]}

    @staticmethod
    def _check_source(source: str) -> None:
        if source not in STALE_SOURCES:
            raise ValueError(f"unknown stale source {source!r}")
