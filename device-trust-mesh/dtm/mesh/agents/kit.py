"""Shared parts of the mesh agents: typed inbound records, and outputs with deterministic ids.

A handler parses its record with ``parse_request`` or ``parse_task_event``, then emits every output
through one ``Outputs``. Each output's envelope is derived from the inbound record, and the output
``messageId`` from the inbound ``messageId``, the config version and the output's index, so a
redelivered record reproduces the same output ids.
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from dtm.common import ids
from dtm.common.a2a import (REPLY_TOPIC, A2AValidationError, artifact_update, now_iso, send_message_request,
                            status_update, task_event, task_topic, validate)
from dtm.common.bus import POLICY_COMMANDS_TOPIC, AgentBus, Inbound
from dtm.common.config import AgentSettings
from dtm.common.envelope import Envelope
from dtm.common.metrics import INVALID_REQUESTS
from dtm.mesh.llm import LLMRequest, LLMResponse, llm_interaction_record

log = logging.getLogger(__name__)

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
COMPLETED = "TASK_STATE_COMPLETED"
REJECTED = "TASK_STATE_REJECTED"
FAILED = "TASK_STATE_FAILED"
AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

LLM_INTERACTIONS_TOPIC = "agent.llm-interactions"

try:
    AGENT_VERSION = version("device-trust-mesh")
except PackageNotFoundError:
    AGENT_VERSION = "0.0.0"


@dataclass(frozen=True)
class Cause:
    """The inbound record an agent's outputs answer."""
    message_id: str
    rpc_id: str
    context_id: str
    envelope: Envelope


@dataclass(frozen=True)
class Request(Cause):
    data: dict
    text: str | None
    task_id: str | None
    reference_task_ids: list[str]


@dataclass(frozen=True)
class TaskEvent(Cause):
    kind: str
    task_id: str
    state: str | None
    artifact_name: str | None
    artifact_data: dict | None


def _reject(bus: AgentBus, inbound: Inbound, why: str) -> None:
    INVALID_REQUESTS.labels(bus.agent).inc()
    log.warning("%s: skipping %s[%s]@%s: %s", bus.agent, inbound.topic, inbound.partition, inbound.offset, why)


def _valid(bus: AgentBus, inbound: Inbound) -> bool:
    """Check that the record is a non-tombstone that satisfies its logical topic's schema."""
    if inbound.value is None:
        _reject(bus, inbound, "tombstone")
        return False
    try:
        validate(bus.logical_topic(inbound.topic), inbound.value)
    except A2AValidationError as e:
        _reject(bus, inbound, str(e))
        return False
    return True


def parse_request(bus: AgentBus, inbound: Inbound) -> Request | None:
    """Return the typed request, or None, counted as invalid, for a tombstone, a non-SendMessage or an invalid record."""
    value = inbound.value
    if value is not None and value.get("method") != "SendMessage":
        _reject(bus, inbound, f"method is {value.get('method')!r}, not SendMessage")
        return None
    if not _valid(bus, inbound):
        return None
    message = value["params"]["message"]
    data_part, *rest = message["parts"]
    envelope = Envelope.from_a2a(message["metadata"]["urn:device-trust-mesh:envelope:v1"])
    return Request(message_id=message["messageId"], rpc_id=value["id"], context_id=message["contextId"],
                   envelope=envelope, data=data_part["data"], text=rest[0].get("text") if rest else None,
                   task_id=message.get("taskId") or None,
                   reference_task_ids=list(message.get("referenceTaskIds", [])))


def parse_task_event(bus: AgentBus, inbound: Inbound) -> TaskEvent | None:
    """Return the typed task event, or None, counted as invalid, for a tombstone or an invalid record."""
    if not _valid(bus, inbound):
        return None
    value = inbound.value
    kind, carrier = next(iter(value["result"].items()))
    envelope = Envelope.from_a2a(carrier["metadata"]["urn:device-trust-mesh:envelope:v1"])
    artifact = carrier.get("artifact")
    return TaskEvent(
        message_id=envelope.message_id, rpc_id=value["id"], context_id=carrier["contextId"], envelope=envelope,
        kind=kind, task_id=carrier["id"] if kind == "task" else carrier["taskId"],
        state=carrier["status"]["state"] if "status" in carrier else None,
        artifact_name=artifact["name"] if artifact else None,
        artifact_data=artifact["parts"][0].get("data") if artifact else None)


class Outputs:
    """Emits the records an agent produces for one inbound record, each with a derived envelope and id."""

    def __init__(self, bus: AgentBus, agent: str, settings: AgentSettings, cause: Cause,
                 clock: Callable[[], float] = time.time):
        self._bus = bus
        self._agent_id = f"agent:{agent}"
        self._settings = settings
        self._cause = cause
        self._clock = clock
        self._index = 0
        self._key = cause.envelope.device_id

    def new_task_id(self) -> str:
        return ids.task_id(self._agent_id, self._cause.message_id)

    def next_envelope(self) -> Envelope:
        inbound = self._cause.envelope
        envelope = Envelope(
            message_id=ids.message_id(self._agent_id, self._cause.message_id, self._settings.config_version,
                                      self._index),
            correlation_id=inbound.correlation_id, causation_id=self._cause.message_id,
            agent_id=self._agent_id, agent_version=AGENT_VERSION, config_version=self._settings.config_version,
            event_time=round(self._clock() * 1000), device_id=inbound.device_id, customer_id=inbound.customer_id,
            device_seq=inbound.device_seq, required_context_seq=inbound.required_context_seq,
            mode=inbound.mode, replay_run_id=inbound.replay_run_id)
        self._index += 1
        return envelope

    def submitted(self, task_id: str) -> None:
        envelope = self.next_envelope()
        self._reply(task_event(self._cause.rpc_id, task_id, self._cause.context_id, SUBMITTED, envelope,
                               now_iso(envelope.event_time)))

    def status(self, task_id: str, state: str, text: str | None = None) -> None:
        envelope = self.next_envelope()
        self._reply(status_update(self._cause.rpc_id, task_id, self._cause.context_id, state, envelope,
                                  now_iso(envelope.event_time), text))

    def artifact(self, task_id: str, name: str, data: dict, text: str) -> None:
        envelope = self.next_envelope()
        self._reply(artifact_update(self._cause.rpc_id, task_id, self._cause.context_id, envelope.message_id,
                                    name, data, text, envelope))

    def send(self, to_agent: str, data: dict, text: str, *, task_id: str | None = None,
             reference_task_ids: list[str] | None = None) -> None:
        request = send_message_request(self.next_envelope(), data, text, task_id=task_id,
                                       reference_task_ids=reference_task_ids)
        self._bus.send(to_agent, request, self._key)

    def policy_command(self, action: str, reason: str, *, challenge_method: str | None = None,
                       enrolment_task_id: str | None = None) -> None:
        self.avro(POLICY_COMMANDS_TOPIC, {"action": action, "reason": reason, "challenge_method": challenge_method,
                                          "enrolment_task_id": enrolment_task_id})

    def llm_interaction(self, req: LLMRequest, resp: LLMResponse, guardrail_result: str | None = None) -> str:
        record = llm_interaction_record(self.next_envelope(), req, resp, guardrail_result)
        self._bus.produce_avro(LLM_INTERACTIONS_TOPIC, record, self._key)
        return record["envelope"]["message_id"]

    def avro(self, topic: str, fields: dict) -> Envelope:
        envelope = self.next_envelope()
        self._bus.produce_avro(topic, {"envelope": envelope.to_avro(), **fields}, self._key)
        return envelope

    def _reply(self, record: dict) -> None:
        self._bus.reply(None, record, self._key)


def run_task(outputs: Outputs, task_id: str, body: Callable[[], None]) -> None:
    """Run ``body``; an exception ends the task FAILED with ``ExceptionType: message`` and is not re-raised."""
    try:
        body()
    except Exception as e:
        log.exception("task %s failed", task_id)
        outputs.status(task_id, FAILED, f"{type(e).__name__}: {e}")
