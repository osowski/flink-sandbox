"""The in-memory board behind the UI: devices, the message feed, busiest pairs and the summary strip.

Each ``apply_*`` method folds one input into the board and returns the deltas a browser needs to stay
current, as ``(type, payload)`` pairs. One lock guards every structure, so the Kafka consumer, the
context poller and the SSE handlers can all call in from their own threads.
"""
import logging
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from dtm.common.envelope import to_epoch_ms

ENROLMENT_DECISION = "enrolment-decision"
RISK_ASSESSMENT = "risk-assessment"
STEP_UP_CHALLENGE = "step-up-challenge"
AUDIT_RECORD = "audit-record"
CHALLENGED = "CHALLENGED"
ROUTER = "dtm-a2a-task-router"
STEP_UP_HAND_OFF = ("enrolment", "step-up", "STEP_UP")
STALE_SOURCES = ("kafka", "context")
RATIONALE_CHARS = 80
REQUESTER_FACTOR = 4
MINUTE_MS = 60_000

AGENT_NAMES = {"risk": "Risk", "enrolment": "Enrolment", "step-up": "Step-up", "compliance": "Compliance",
               "revocation": "Revocation"}
TASK_PURPOSE = {"ASSESS_ENROLMENT": "assess risk", "DECIDE_ENROLMENT": "decide enrolment",
                "STEP_UP": "verify identity", "REVOKE": "revoke trust", "RESOLVE_CHALLENGE": "resolve challenge"}
ACTION_COLORS = {"ALLOW": "green", "READ_ONLY": "green", "CHALLENGE": "amber", "BLOCK": "red"}

log = logging.getLogger(__name__)

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


def json_safe(value):
    """Return ``value`` as strict-JSON data: datetimes become epoch ms and tuples become lists."""
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


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
        self._latency: OrderedDict[str, dict] = OrderedDict()
        self._feed: deque[dict] = deque(maxlen=feed_limit)
        self._requesters: OrderedDict[str, str | None] = OrderedDict()
        self._rows: OrderedDict[str, dict] = OrderedDict()
        self._held: OrderedDict[str, dict] = OrderedDict()
        self._activity: dict[str, dict] = {}
        self._hand_offs: dict[str, tuple[str, dict]] = {}
        self._pair_windows: dict[int, dict[tuple[str, str], tuple[int, int]]] = {}
        self._decision_window: list[tuple[int, float]] = []
        self._freshness_window: list[tuple[int, float]] = []
        self._context_lag: tuple[float | None, float | None] = (None, None)
        self._context_lag_over: tuple[bool, bool] = (False, False)
        self._stale: dict[str, int | None] = dict.fromkeys(STALE_SOURCES)

    @property
    def feed_limit(self) -> int:
        """How many recent messages, and so message rows, the board holds."""
        return self._feed_limit

    def apply_devices(self, rows: list[dict], polled_at_ms: int) -> list[Delta]:
        """Fold in a ``/v1/devices`` poll.

        Freshness is measured only when a device already on the board advances its context sequence, so
        the first sighting of a device, including the whole first poll, measures none.
        """
        with self._lock:
            deltas: list[Delta] = []
            for row in rows:
                changed = self._apply_device_row(row, polled_at_ms)
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
            deltas = [self._message("task", value, value.get("from_agent"), value.get("to_agent"),
                                    message_id, coord)]
            bubble = self._task_bubble(value, message_id)
            if bubble is not None:
                self._hand_offs[bubble["agent"]] = (message_id, json_safe(value))
            return deltas + self._activity_delta(bubble)

    def apply_decision(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.decisions_human_readable`` row, targeted at the agent that asked."""
        return self._apply_reply("decision", value, coord)

    def apply_status(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.task-status_human_readable`` row, targeted at the agent that asked."""
        return self._apply_reply("status", value, coord)

    def apply_pair_counts(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.pair_counts`` row; a re-delivered row replaces its count.

        Rows can span different window lengths, so each count is kept with its own window length and
        read back as a per-minute rate. A row whose window has no positive length is skipped.
        """
        with self._lock:
            window_end = to_epoch_ms(value["window_end"])
            length_ms = window_end - to_epoch_ms(value["window_start"])
            if length_ms <= 0:
                log.warning("skipping pair count with window length %s ms: %s -> %s", length_ms,
                            value["from_agent"], value["to_agent"])
                return []
            pairs = (value["from_agent"], value["to_agent"])
            self._pair_windows.setdefault(window_end, {})[pairs] = (value["message_count"], length_ms)
            for stale in sorted(self._pair_windows)[:-self._history_windows]:
                del self._pair_windows[stale]
            return [("pairs", self._pairs())]

    def apply_latency(self, value: dict, coord: Coord) -> list[Delta]:
        """Fold in an ``agent.decision_latency`` row; only enrolment decisions reach the board.

        A row older than the device's latest decision is a replay and is ignored; a re-delivered latest
        row is applied again but observed once. The summary window is keyed on ``decision_time``. A row
        for a device the board has not listed yet waits and is applied when the device first appears.
        """
        with self._lock:
            if value["artifact_name"] != ENROLMENT_DECISION:
                return []
            device_id = value["device_id"]
            decided_at = to_epoch_ms(value["decision_time"])
            high_water = self._decided_at.get(device_id)
            if high_water is not None and decided_at < high_water:
                return []
            self._decided_at[device_id] = decided_at
            if value["latency_ms"] is not None and (high_water is None or decided_at > high_water):
                self._observe(self._decision_window, decided_at, value["latency_ms"])
            self._latency[device_id] = {"latency_ms": value["latency_ms"], "reason": _reason(value)}
            self._latency.move_to_end(device_id)
            while len(self._latency) > self._feed_limit:
                self._latency.popitem(last=False)
            row = self._devices.get(device_id)
            if row is None:
                return [("summary", self._summary())]
            row.update(self._latency_fields(row))
            return [("device", dict(row)), ("summary", self._summary())]

    def apply_context_lag(self, p50_ms: float | None, p95_ms: float | None,
                          p50_over: bool = False, p95_over: bool = False) -> list[Delta]:
        """Record context-mcp's own materialization lag percentiles.

        Values are finite or None, since they go out as JSON; an ``over`` flag says the true lag is above
        that value.
        """
        with self._lock:
            self._context_lag = (p50_ms, p95_ms)
            self._context_lag_over = (p50_over, p95_over)
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
        """Return everything a new client needs: devices (latest change first), feed (newest first), pairs, summary, stale flags and each agent's latest bubble."""
        with self._lock:
            devices = sorted(self._devices.values(), key=lambda r: (-r["changed_at_ms"], r["device_id"]))
            return {"devices": [dict(r) for r in devices],
                    "feed": [dict(m) for m in reversed(self._feed)],
                    "pairs": self._pairs(), "summary": self._summary(), "stale": dict(self._stale),
                    "activity": {agent: dict(b) for agent, b in self._activity.items()}}

    def message(self, message_id: str) -> dict | None:
        """Return ``{"message": feed entry, "row": row as applied}`` for a held message id, else None."""
        with self._lock:
            held = self._rows.get(message_id)
            return None if held is None else {"message": dict(held["message"]), "row": json_safe(held["row"])}

    def _apply_device_row(self, row: dict, polled_at_ms: int) -> dict | None:
        device_id = row["device_id"]
        seq = row["context_seq"]
        known = self._devices.get(device_id)
        core = {"customer_id": row["customer_id"], "trust_status": row["trust_status"],
                "context_seq": seq, "last_event_time_ms": to_epoch_ms(row["last_event_time"]),
                "last_event_type": row["last_event_type"]}
        if known is not None and all(known[k] == v for k, v in core.items()):
            return None
        status_changed = known is None or known["trust_status"] != core["trust_status"]
        if known is not None and status_changed:
            self._previous_status[device_id] = known["trust_status"]
        freshness = known["freshness_ms"] if known is not None else None
        advanced = (known is not None and seq is not None
                    and (known["context_seq"] is None or seq > known["context_seq"]))
        if advanced and core["last_event_time_ms"] is not None:
            freshness = max(0, polled_at_ms - core["last_event_time_ms"])
            self._observe(self._freshness_window, self._now_ms(), freshness)
        label, color = display_status(core["trust_status"], self._previous_status.get(device_id))
        new = {"device_id": device_id, **core, "label": label, "color": color, "reason": None,
               "latency_ms": None, "freshness_ms": freshness,
               "changed_at_ms": polled_at_ms if status_changed else known["changed_at_ms"]}
        new.update(self._latency_fields(new))
        self._devices[device_id] = new
        return new

    def _latency_fields(self, row: dict) -> dict:
        """Return ``latency_ms`` and ``reason`` for a device row: the latest decision's, else the last event type."""
        latest = self._latency.get(row["device_id"], {})
        return {"latency_ms": latest.get("latency_ms"), "reason": latest.get("reason") or row["last_event_type"]}

    def _apply_reply(self, kind: str, value: dict, coord: Coord) -> list[Delta]:
        with self._lock:
            envelope = value["envelope"]
            target = self._requesters.get(envelope.get("causation_id"))
            deltas = [self._message(kind, value, value.get("agent"), target, envelope["message_id"], coord)]
            if kind == "decision":
                deltas += self._activity_delta(self._decision_bubble(value, target))
            return deltas

    def _message(self, kind: str, value: dict, source: str | None, target: str | None,
                 message_id: str, coord: Coord) -> Delta:
        envelope = value["envelope"]
        message = {"kind": kind, "time_ms": to_epoch_ms(envelope["event_time"]), "source": source,
                   "target": target, "device_id": envelope["device_id"], "task_id": value.get("task_id"),
                   "message_id": message_id, "topic": coord.topic, "partition": coord.partition,
                   "offset": coord.offset}
        self._feed.append(message)
        self._rows[message_id] = {"message": message, "row": json_safe(value)}
        self._rows.move_to_end(message_id)
        while len(self._rows) > self._feed_limit:
            self._rows.popitem(last=False)
        return "message", dict(message)

    def _task_bubble(self, value: dict, message_id: str) -> dict | None:
        """Return the speech bubble a hand-off sets for its sender, or None for the router.

        Enrolment hands off to step-up only on a challenge, so that hand-off is always shown as one.
        """
        sender = value.get("from_agent")
        if not sender or sender == ROUTER:
            return None
        task_type = value.get("task_type")
        to_agent = value.get("to_agent")
        purpose = TASK_PURPOSE.get(task_type, (task_type or "task").lower().replace("_", " "))
        held = self._held.get(value["envelope"].get("correlation_id"), {})
        action, color = purpose, "neutral"
        if sender == "risk" and held.get("risk_score") is not None:
            action = f"{purpose} · risk {held['risk_score']}"
        elif (sender, to_agent, task_type) == STEP_UP_HAND_OFF or held.get("challenged"):
            action, color = f"{CHALLENGED} → {purpose}", "amber"
        return self._bubble(sender, AGENT_NAMES.get(to_agent, to_agent or "—"), action, color, value, message_id)

    def _decision_bubble(self, value: dict, target: str | None) -> dict | None:
        """Return the speech bubble an artifact sets for its agent; an enrichment is held for the hand-off instead.

        A risk score that arrives after its hand-off returns that hand-off's bubble, enriched. An audit record
        names the outcome, else the method, of the artifact it audits, its causation.
        """
        agent = value.get("agent")
        artifact_name = value.get("artifact_name")
        outcome, policy_action = value.get("outcome"), value.get("policy_action")
        message_id = value["envelope"]["message_id"]
        if artifact_name == RISK_ASSESSMENT:
            self._hold(value, risk_score=value.get("risk_score"))
            return self._enriched_hand_off("risk", value["envelope"].get("correlation_id"))
        if not agent:
            return None
        if artifact_name == STEP_UP_CHALLENGE:
            method = value.get("method")
            action = f"challenge → {method}" if method else "challenge"
            return self._bubble(agent, "device", action, "amber", value, message_id)
        if artifact_name == AUDIT_RECORD:
            subject = self._rows.get(value["envelope"].get("causation_id"), {}).get("row", {})
            audited = outcome or subject.get("outcome") or subject.get("method")
            return self._bubble(agent, "audit log", f"audited {audited}" if audited else "audited",
                                "neutral", value, message_id)
        if outcome == CHALLENGED:
            self._hold(value, challenged=True)
            return None
        to = "device" if policy_action else AGENT_NAMES.get(target, target) if target else "—"
        action = " → ".join(p for p in (outcome, policy_action) if p) or artifact_name or "decision"
        return self._bubble(agent, to, action, ACTION_COLORS.get(policy_action, "neutral"), value, message_id)

    def _enriched_hand_off(self, agent: str, correlation_id: str | None) -> dict | None:
        """Return ``agent``'s hand-off bubble on ``correlation_id`` rebuilt with what is held, if that changes it."""
        message_id, hand_off = self._hand_offs.get(agent, (None, None))
        if hand_off is None or correlation_id is None or hand_off["envelope"].get("correlation_id") != correlation_id:
            return None
        bubble = self._task_bubble(hand_off, message_id)
        return None if bubble == self._activity.get(agent) else bubble

    def _hold(self, value: dict, **fields) -> None:
        """Keep an enrichment for the hand-off on the same correlation, which may already have passed."""
        correlation_id = value["envelope"].get("correlation_id")
        if correlation_id is None:
            return
        self._held.setdefault(correlation_id, {}).update(fields)
        self._held.move_to_end(correlation_id)
        while len(self._held) > self._feed_limit * REQUESTER_FACTOR:
            self._held.popitem(last=False)

    def _bubble(self, agent: str, to: str, action: str, color: str, value: dict, message_id: str) -> dict:
        envelope = value["envelope"]
        device_id = envelope["device_id"]
        customer_id = envelope.get("customer_id") or self._devices.get(device_id, {}).get("customer_id")
        return {"agent": agent, "to": to, "customer_id": customer_id, "device_id": device_id,
                "action": action, "color": color, "message_id": message_id,
                "time_ms": to_epoch_ms(envelope["event_time"])}

    def _activity_delta(self, bubble: dict | None) -> list[Delta]:
        """Record ``bubble`` as its agent's latest and return its delta, or nothing when there is none.

        The agent's hand-off is kept for a later enrichment only while its bubble is that hand-off's.
        """
        if bubble is None:
            return []
        agent = bubble["agent"]
        if self._hand_offs.get(agent, (None,))[0] != bubble["message_id"]:
            self._hand_offs.pop(agent, None)
        self._activity[agent] = bubble
        return [("activity", dict(bubble))]

    def _observe(self, window: list, at_ms: int, value: float) -> None:
        window.append((at_ms, value))
        self._prune(window)

    def _prune(self, window: list) -> None:
        cutoff = self._now_ms() - self._window_ms
        window[:] = [(at, v) for at, v in window if at >= cutoff]

    def _now_ms(self) -> int:
        return round(self._clock() * 1000)

    def _pairs(self) -> dict:
        if not self._pair_windows:
            return {"window_end_ms": None, "window_ms": None, "ranking": []}
        windows = sorted(self._pair_windows)
        newest = self._pair_windows[windows[-1]]

        def per_minute(window: int, pair: tuple[str, str]) -> float:
            count, length_ms = self._pair_windows[window].get(pair, (0, MINUTE_MS))
            return count * MINUTE_MS / length_ms

        ranking = [{"from_agent": f, "to_agent": t, "message_count": count,
                    "per_minute": per_minute(windows[-1], (f, t)),
                    "history": [per_minute(w, (f, t)) for w in windows]}
                   for (f, t), (count, _) in newest.items()]
        ranking.sort(key=lambda r: (-r["per_minute"], r["from_agent"], r["to_agent"]))
        return {"window_end_ms": windows[-1], "window_ms": max(length for _, length in newest.values()),
                "ranking": ranking}

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
                "context_lag_p50_ms": self._context_lag[0], "context_lag_p95_ms": self._context_lag[1],
                "context_lag_p50_over": self._context_lag_over[0],
                "context_lag_p95_over": self._context_lag_over[1]}

    @staticmethod
    def _check_source(source: str) -> None:
        if source not in STALE_SOURCES:
            raise ValueError(f"unknown stale source {source!r}")
