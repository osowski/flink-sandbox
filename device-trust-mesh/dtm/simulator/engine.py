"""The simulator engine: enrolment pacing, risk scenarios and the closed loop on policy commands.

The engine is pure. It never reads a clock or touches Kafka: time arrives as ``now_ms`` and the only
randomness is the injected ``random.Random``. ``tick`` returns the device events that are due and
the caller produces them.
"""
import logging
import random
import uuid
from collections import deque
from dataclasses import dataclass

from dtm.simulator.events import device_event
from dtm.simulator.fingerprints import fingerprint
from dtm.simulator.pool import Device, DevicePool

log = logging.getLogger(__name__)

DEFAULT_RATE = 0.2
DEFAULT_MIX = {
    "CLEAN": 0.7,
    "COMPROMISED_OS": 0.075,
    "OUTDATED_OS": 0.075,
    "NEW_DEVICE_ESTABLISHED_ACCOUNT": 0.075,
    "IMPOSSIBLE_TRAVEL": 0.075,
}
SCENARIOS = ("CLEAN", "COMPROMISED_OS", "OUTDATED_OS", "NEW_DEVICE_ESTABLISHED_ACCOUNT",
             "IMPOSSIBLE_TRAVEL", "CHALLENGE", "UNENROL")

NEW, PENDING, TRUSTED, READ_ONLY = "NEW", "PENDING", "TRUSTED", "READ_ONLY"
CHALLENGED, BLOCKED, UNENROLLED = "CHALLENGED", "BLOCKED", "UNENROLLED"

_ENROLLABLE = (NEW, UNENROLLED, BLOCKED)
_ACTION_STATUS = {"ALLOW": TRUSTED, "READ_ONLY": READ_ONLY, "CHALLENGE": CHALLENGED}
_FAR_COUNTRY, _FAR_COUNTRY_FROM_FAR = "AU", "GB"
_ESTABLISHED_ACCOUNT_DAYS = 90
_RECENT_EVENT_MS = 10 * 60_000
_DEDUPE_CAPACITY = 10_000
_MIN_BUCKET = 2.0
_TOKEN_EPSILON = 1e-9


@dataclass
class _DeviceState:
    status: str = NEW
    pending_since_ms: int = 0
    fp: dict | None = None
    country: str | None = None


@dataclass
class _ChallengeAnswer:
    due_ms: int
    device_id: str
    passed: bool
    task_id: str
    correlation_id: str
    causation_id: str


@dataclass
class _Plan:
    device: Device
    event_type: str
    kind: str
    country: str


class Simulator:
    def __init__(self, pool: DevicePool, rng: random.Random, *, rate_per_sec: float = DEFAULT_RATE,
                 pass_probability: float = 0.8, challenge_delay_ms: int = 2000,
                 pending_timeout_ms: int = 120_000, version: str = "dev"):
        if rate_per_sec < 0:
            raise ValueError(f"rate_per_sec must not be negative, got {rate_per_sec}")
        self.pool = pool
        self.running = True
        self._rng = rng
        self._rate = rate_per_sec
        self._pass_probability = pass_probability
        self._challenge_delay_ms = challenge_delay_ms
        self._pending_timeout_ms = pending_timeout_ms
        self._version = version
        self._devices = {d.device_id: d for d in pool.devices}
        self._state = {d.device_id: _DeviceState() for d in pool.devices}
        self._customers = pool.customers()
        self._last_event: dict[str, tuple[int, str]] = {}
        self._injected: deque[str] = deque()
        self._answers: list[_ChallengeAnswer] = []
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._tokens = 0.0
        self._last_tick_ms: int | None = None

    def status(self, device_id: str) -> str:
        return self._state[device_id].status

    def tick(self, now_ms: int) -> list[dict]:
        """Return the device events due at ``now_ms``: challenge answers first, then paced enrolments."""
        self._refill(now_ms)
        events = self._due_answers(now_ms)
        while self._tokens >= 1 - _TOKEN_EPSILON:
            self._tokens -= 1
            scenario = self._injected.popleft() if self._injected else self._draw_from_mix()
            event = self._emit(scenario, now_ms)
            if event is not None:
                events.append(event)
        return events

    def on_policy_command(self, cmd: dict, now_ms: int) -> None:
        env = cmd["envelope"]
        message_id = env["message_id"]
        device_id = env["device_id"]
        state = self._state.get(device_id)
        if state is None:
            log.warning("policy command %s for unknown device %s ignored", message_id, device_id)
            return
        if message_id in self._seen:
            return
        action = cmd["action"]
        if action == "BLOCK":
            status = UNENROLLED if cmd.get("reason") == "UNENROLLED" else BLOCKED
        elif action in _ACTION_STATUS:
            status = _ACTION_STATUS[action]
        else:
            log.warning("policy command %s has unknown action %r; ignored", message_id, action)
            return
        self._remember(message_id)
        state.status = status
        if action == "CHALLENGE":
            self._schedule_answer(cmd, now_ms)

    def on_control(self, ctrl: dict) -> None:
        rate = ctrl.get("rate_per_sec")
        if rate is not None:
            if rate < 0:
                log.warning("negative rate %s ignored", rate)
            else:
                self._rate = rate
        command = ctrl.get("command")
        if command == "START":
            self.running = True
        elif command == "STOP":
            self.running = False
        elif command == "INJECT":
            scenario = ctrl.get("scenario")
            if scenario is None:
                log.warning("INJECT without a scenario ignored")
                return
            if scenario not in SCENARIOS:
                log.warning("INJECT of unknown scenario %r ignored", scenario)
                return
            count = ctrl.get("count")
            self._injected.extend([scenario] * max(1 if count is None else count, 0))

    def _refill(self, now_ms: int) -> None:
        if self._last_tick_ms is not None and self.running:
            elapsed_s = max(now_ms - self._last_tick_ms, 0) / 1000
            self._tokens = min(self._tokens + self._rate * elapsed_s, max(_MIN_BUCKET, self._rate))
        self._last_tick_ms = now_ms if self._last_tick_ms is None else max(self._last_tick_ms, now_ms)

    def _draw_from_mix(self) -> str:
        return self._rng.choices(list(DEFAULT_MIX), weights=list(DEFAULT_MIX.values()))[0]

    def _remember(self, message_id: str) -> None:
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        if len(self._seen_order) > _DEDUPE_CAPACITY:
            self._seen.discard(self._seen_order.popleft())

    def _schedule_answer(self, cmd: dict, now_ms: int) -> None:
        env = cmd["envelope"]
        task_id = cmd.get("enrolment_task_id")
        if not task_id:
            log.warning("challenge command %s for %s has no enrolment_task_id; not answered",
                        env["message_id"], env["device_id"])
            return
        self._answers.append(_ChallengeAnswer(
            due_ms=now_ms + self._challenge_delay_ms,
            device_id=env["device_id"],
            passed=self._rng.random() < self._pass_probability,
            task_id=task_id,
            correlation_id=env["correlation_id"],
            causation_id=env["message_id"],
        ))

    def _due_answers(self, now_ms: int) -> list[dict]:
        due = [a for a in self._answers if a.due_ms <= now_ms]
        self._answers = [a for a in self._answers if a.due_ms > now_ms]
        events = []
        for answer in due:
            state = self._state[answer.device_id]
            if state.status != CHALLENGED:
                continue
            device = self._devices[answer.device_id]
            events.append(self._event(
                device, "CHALLENGE_PASSED" if answer.passed else "CHALLENGE_FAILED", now_ms,
                correlation_id=answer.correlation_id, causation_id=answer.causation_id,
                fp=state.fp or fingerprint(device.platform, "CLEAN"),
                country=state.country or device.home_country, enrolment_task_id=answer.task_id))
        return events

    def _emit(self, scenario: str, now_ms: int) -> dict | None:
        plan = self._plan(scenario, now_ms)
        if plan is None:
            if scenario != "CLEAN":
                log.info("scenario %s has no feasible device; falling back to CLEAN", scenario)
            plan = self._plan("CLEAN", now_ms)
        if plan is None:
            return None
        state = self._state[plan.device.device_id]
        state.status = PENDING
        state.pending_since_ms = now_ms
        state.fp = fingerprint(plan.device.platform, plan.kind)
        state.country = plan.country
        return self._event(plan.device, plan.event_type, now_ms, correlation_id=str(uuid.uuid4()),
                           fp=state.fp, country=plan.country)

    def _plan(self, scenario: str, now_ms: int) -> _Plan | None:
        if scenario == "UNENROL":
            return self._unenrol_plan()
        candidates = self._enrollable(now_ms)
        if scenario == "CLEAN":
            if candidates:
                return self._enrol_plan(self._rng.choice(candidates), "CLEAN")
            return self._unenrol_plan()
        if scenario == "COMPROMISED_OS":
            kind = "ROOTED"
        elif scenario in ("OUTDATED_OS", "CHALLENGE"):
            kind = "OUTDATED"
        elif scenario == "NEW_DEVICE_ESTABLISHED_ACCOUNT":
            candidates = [d for d in candidates if self._has_trusted_sibling(d)
                          and d.account_age_days > _ESTABLISHED_ACCOUNT_DAYS]
            kind = "CLEAN"
        elif scenario == "IMPOSSIBLE_TRAVEL":
            recent = [d for d in candidates if self._has_recent_event(d.customer_id, now_ms)]
            if not recent:
                return None
            device = self._rng.choice(recent)
            far = _FAR_COUNTRY_FROM_FAR if device.home_country == _FAR_COUNTRY else _FAR_COUNTRY
            return _Plan(device, "ENROL_REQUESTED", "CLEAN", far)
        else:
            raise ValueError(f"unknown scenario {scenario!r}")
        return self._enrol_plan(self._rng.choice(candidates), kind) if candidates else None

    def _enrol_plan(self, device: Device, kind: str) -> _Plan:
        return _Plan(device, "ENROL_REQUESTED", kind, device.home_country)

    def _unenrol_plan(self) -> _Plan | None:
        trusted = [d for d in self.pool.devices if self._state[d.device_id].status == TRUSTED]
        if not trusted:
            return None
        device = self._rng.choice(trusted)
        return _Plan(device, "UNENROL_REQUESTED", "CLEAN", device.home_country)

    def _enrollable(self, now_ms: int) -> list[Device]:
        def eligible(state: _DeviceState) -> bool:
            if state.status in _ENROLLABLE:
                return True
            return state.status == PENDING and now_ms - state.pending_since_ms >= self._pending_timeout_ms

        return [d for d in self.pool.devices if eligible(self._state[d.device_id])]

    def _has_trusted_sibling(self, device: Device) -> bool:
        return any(d.device_id != device.device_id and self._state[d.device_id].status == TRUSTED
                   for d in self._customers[device.customer_id])

    def _has_recent_event(self, customer_id: str, now_ms: int) -> bool:
        last = self._last_event.get(customer_id)
        return last is not None and now_ms - last[0] <= _RECENT_EVENT_MS

    def _event(self, device: Device, event_type: str, now_ms: int, *, correlation_id: str, fp: dict,
               country: str, causation_id: str | None = None, enrolment_task_id: str | None = None) -> dict:
        self._last_event[device.customer_id] = (now_ms, country)
        return device_event(device, event_type, seq=self.pool.next_seq(device.device_id, now_ms),
                            now_ms=now_ms, correlation_id=correlation_id, fp=fp, country=country,
                            causation_id=causation_id, enrolment_task_id=enrolment_task_id,
                            version=self._version)
