"""AgentBus: the agents' only Kafka access, speaking the A2A v1.0 Kafka binding.

A2A records are validated against the vendored schemas and written as the caller's own JSON, so
integers stay integers. Avro records go through an injected serializer. Every topic name an agent
uses is logical; the configured prefix is applied only when talking to Kafka.
"""
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from confluent_kafka import OFFSET_BEGINNING, OFFSET_END, TopicPartition

from dtm.common.a2a import CARDS_TOPIC, REPLY_TOPIC, dumps, task_topic, validate
from dtm.common.envelope import ENVELOPE_URI
from dtm.common.kafka import bootstrap_servers, consumer_config, producer_config, schema_registry_url
from dtm.common.metrics import HOP_LATENCY_MS

log = logging.getLogger(__name__)

POLICY_COMMANDS_TOPIC = "device.policy-commands"

_MODES = ("live", "replay")
_START_POSITIONS = ("committed", "earliest", "offset", "timestamp")
_POLICY_COMMAND_MODES = ("enabled", "redirect", "disabled")
_FLUSH_TIMEOUT_S = 10.0
_LOOKUP_TIMEOUT_S = 10.0
_POLL_BATCH = 10
_REPLAY_ONLY_POSITIONS = ("earliest", "offset", "timestamp")


class ConsumeNotAllowed(ValueError):
    """Raised when an agent subscribes to a topic outside its allow-list."""


class DeliveryFailed(RuntimeError):
    """Raised by ``commit`` when a produced record was not acknowledged by the broker.

    Nothing was committed. The caller must re-process the same inbound record, or stop, before
    committing anything later; committing a later offset would skip the failed record's input.
    """


class CommitFailed(RuntimeError):
    """Raised when the broker rejected an offset commit."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _partition_offsets(value) -> dict[int, int]:
    """Normalize a ``{partition: offset}`` mapping, accepting int or str keys, into int keys."""
    if not isinstance(value, dict) or not value:
        raise ValueError("startPosition offset requires startValue as a non-empty {partition: offset} mapping")
    try:
        offsets = {int(k): v for k, v in value.items()}
    except (TypeError, ValueError):
        raise ValueError(f"startValue partitions must be integers; got {sorted(map(str, value))}") from None
    if any(p < 0 for p in offsets) or not all(_is_int(o) and o >= 0 for o in offsets.values()):
        raise ValueError("startValue must map non-negative partitions to non-negative offsets")
    return offsets


def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")


@dataclass(frozen=True)
class BusConfig:
    mode: str = "live"
    start_position: str = "committed"
    start_value: int | dict[int, int] | None = None
    topic_prefix: str = ""
    policy_commands: str = "enabled"
    allowed_consume: tuple[str, ...] = ("agent.tasks.<self>", "agent.task-events", "agent.cards")
    replay_run_id: str | None = None

    def __post_init__(self):
        _one_of("mode", self.mode, _MODES)
        _one_of("startPosition", self.start_position, _START_POSITIONS)
        _one_of("policyCommands", self.policy_commands, _POLICY_COMMAND_MODES)
        if self.policy_commands == "redirect" and not self.topic_prefix:
            raise ValueError("policyCommands redirect requires a non-empty topicPrefix")
        if self.start_position in _REPLAY_ONLY_POSITIONS and not (self.mode == "replay" and self.replay_run_id):
            raise ValueError(f"startPosition {self.start_position} requires mode replay and a replayRunId")
        if self.start_position == "offset":
            object.__setattr__(self, "start_value", _partition_offsets(self.start_value))
        elif self.start_position == "timestamp":
            if not _is_int(self.start_value) or self.start_value < 0:
                raise ValueError("startPosition timestamp requires startValue as epoch milliseconds")

    @classmethod
    def from_dict(cls, d: dict) -> "BusConfig":
        """Build a config from the camelCase keys used in ``agents.yaml``; absent keys keep their defaults."""
        names = {"mode": "mode", "startPosition": "start_position", "startValue": "start_value",
                 "topicPrefix": "topic_prefix", "policyCommands": "policy_commands",
                 "allowedConsume": "allowed_consume", "replayRunId": "replay_run_id"}
        values = {field: d[key] for key, field in names.items() if key in d}
        if "allowed_consume" in values:
            values["allowed_consume"] = tuple(values["allowed_consume"])
        return cls(**values)


@dataclass(frozen=True)
class Inbound:
    """A consumed record. ``topic`` is the physical topic; ``value`` is parsed JSON, or None for a tombstone."""
    topic: str
    partition: int
    offset: int
    key: str | None
    headers: dict[str, str]
    value: dict | None
    timestamp_ms: int


def _group_id(agent: str, config: BusConfig) -> str:
    group = f"dtm.{agent}.{config.mode}"
    return f"{group}.{config.replay_run_id}" if config.replay_run_id else group


def _a2a_envelope(record: dict) -> dict | None:
    """Return the envelope dict of an A2A request or task event, or None when the record carries none."""
    params = record.get("params")
    if isinstance(params, dict):
        carrier = params.get("message")
    else:
        result = record.get("result")
        carrier = next(iter(result.values()), None) if isinstance(result, dict) else None
    metadata = carrier.get("metadata") if isinstance(carrier, dict) else None
    envelope = metadata.get(ENVELOPE_URI) if isinstance(metadata, dict) else None
    return envelope if isinstance(envelope, dict) else None


def _a2a_context_id(record: dict) -> str | None:
    if "params" in record:
        return record["params"]["message"].get("contextId")
    return next(iter(record["result"].values())).get("contextId")


class AgentBus:
    def __init__(self, agent: str, config: BusConfig, *, producer, consumer,
                 avro_serialize: Callable[[str, dict], bytes], clock: Callable[[], float] = time.time):
        self.agent = agent
        self.config = config
        self._producer = producer
        self._consumer = consumer
        self._avro_serialize = avro_serialize
        self._clock = clock
        self._delivery_errors: list = []
        self._produced: set[str] = set()
        self._paused = False
        self._allowed = {name.replace("<self>", agent) for name in config.allowed_consume}

    @classmethod
    def connect(cls, agent: str, config: BusConfig) -> "AgentBus":
        from confluent_kafka import Consumer, Producer
        from confluent_kafka.schema_registry import SchemaRegistryClient
        from confluent_kafka.schema_registry.avro import AvroSerializer
        from confluent_kafka.serialization import MessageField, SerializationContext

        registry = SchemaRegistryClient({"url": schema_registry_url()})
        serializers: dict[str, AvroSerializer] = {}

        def avro_serialize(topic: str, value: dict) -> bytes:
            if topic not in serializers:
                serializers[topic] = AvroSerializer(
                    registry, schema_str=None,
                    conf={"auto.register.schemas": False, "use.latest.version": True})
            return serializers[topic](value, SerializationContext(topic, MessageField.VALUE))

        bootstrap = bootstrap_servers()
        return cls(agent, config,
                   producer=Producer(producer_config(bootstrap)),
                   consumer=Consumer(consumer_config(bootstrap, _group_id(agent, config))),
                   avro_serialize=avro_serialize)

    @property
    def group_id(self) -> str:
        return _group_id(self.agent, self.config)

    def topic(self, name: str) -> str:
        """Return the physical topic for a logical name."""
        return self.config.topic_prefix + name

    def logical_topic(self, physical: str) -> str:
        """Return the logical name of a physical topic by removing the configured prefix."""
        prefix = self.config.topic_prefix
        return physical[len(prefix):] if prefix and physical.startswith(prefix) else physical

    def produced_topics(self) -> set[str]:
        """Return the physical topics written so far."""
        return set(self._produced)

    def subscribe(self, topics: list[str]) -> None:
        refused = [t for t in topics if t not in self._allowed]
        if refused:
            raise ConsumeNotAllowed(f"{self.agent} may not consume {', '.join(refused)}; "
                                    f"allowed: {', '.join(sorted(self._allowed))}")
        self._consumer.subscribe([self.topic(t) for t in topics], on_assign=self._on_assign)

    def _on_assign(self, consumer, partitions: list[TopicPartition]) -> None:
        if self.config.start_position != "committed":
            self._apply_start_position(consumer, partitions)
        consumer.assign(partitions)
        if self._paused:
            consumer.pause(partitions)

    def _apply_start_position(self, consumer, partitions: list[TopicPartition]) -> None:
        """Set start offsets on the partitions that have no committed offset; the others resume where they left off."""
        committed = consumer.committed(partitions, timeout=_LOOKUP_TIMEOUT_S)
        for tp in committed:
            if tp.error:
                raise RuntimeError(f"cannot read committed offset of {tp.topic}[{tp.partition}]: {tp.error}")
        has_commit = {(tp.topic, tp.partition) for tp in committed if tp.offset >= 0}
        fresh = [tp for tp in partitions if (tp.topic, tp.partition) not in has_commit]
        position, value = self.config.start_position, self.config.start_value
        if position == "earliest":
            for tp in fresh:
                tp.offset = OFFSET_BEGINNING
        elif position == "offset":
            for tp in fresh:
                tp.offset = value.get(tp.partition, OFFSET_BEGINNING)
        elif position == "timestamp" and fresh:
            probes = [TopicPartition(tp.topic, tp.partition, value) for tp in fresh]
            resolved = {(r.topic, r.partition): r for r in
                        consumer.offsets_for_times(probes, timeout=_LOOKUP_TIMEOUT_S)}
            for tp in fresh:
                r = resolved[(tp.topic, tp.partition)]
                if r.error:
                    raise RuntimeError(f"cannot resolve timestamp {value} for {tp.topic}[{tp.partition}]: {r.error}")
                tp.offset = r.offset if r.offset >= 0 else OFFSET_END

    def poll(self, timeout: float, max_records: int = _POLL_BATCH) -> list[Inbound]:
        """Return up to ``max_records`` decoded records; errored or undecodable records are logged and skipped."""
        inbound = []
        for m in self._consumer.consume(num_messages=max_records, timeout=timeout):
            if m.error():
                log.warning("consume error on %s: %s", m.topic(), m.error())
                continue
            try:
                record = self._decode(m)
            except (ValueError, TypeError, AttributeError) as e:
                log.warning("skipping undecodable record at %s[%s]@%s: %s", m.topic(), m.partition(), m.offset(), e)
                continue
            self._observe_hop(record)
            inbound.append(record)
        return inbound

    @staticmethod
    def _decode(m) -> Inbound:
        value = None if m.value() is None else json.loads(m.value())
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"value is {type(value).__name__}, not a JSON object")
        key = m.key()
        headers = {k: v.decode("utf-8") for k, v in m.headers() or [] if v is not None}
        return Inbound(m.topic(), m.partition(), m.offset(),
                       key.decode("utf-8") if key is not None else None,
                       headers, value, m.timestamp()[1])

    def _observe_hop(self, record: Inbound) -> None:
        envelope = _a2a_envelope(record.value) if record.value is not None else None
        event_time = envelope.get("eventTime") if envelope is not None else None
        if _is_int(event_time):
            HOP_LATENCY_MS.labels(self.agent, self.logical_topic(record.topic)).observe(
                self._clock() * 1000 - event_time)

    def send(self, to_agent: str, request: dict, key: str) -> None:
        logical = task_topic(to_agent)
        validate(logical, request)
        self._produce(self.topic(logical), dumps(request), key, self._a2a_headers(request))

    def reply(self, inbound: Inbound | None, record: dict, key: str) -> None:
        validate(REPLY_TOPIC, record)
        self._produce(self._reply_topic(inbound), dumps(record), key, self._a2a_headers(record))

    def _reply_topic(self, inbound: Inbound | None) -> str:
        """Return the topic named by the inbound's reply header, or the shared reply topic when absent or outside the prefix."""
        requested = inbound.headers.get("a2a-reply-topic") if inbound is not None else None
        if requested and not requested.startswith(self.config.topic_prefix):
            log.warning("ignoring reply topic %s outside prefix %s", requested, self.config.topic_prefix)
            return self.topic(REPLY_TOPIC)
        return requested or self.topic(REPLY_TOPIC)

    def publish_card(self, card: dict) -> None:
        validate(CARDS_TOPIC, card)
        self._produce(self.topic(CARDS_TOPIC), dumps(card), card["name"], None)

    def tombstone_card(self, name: str) -> None:
        self._produce(self.topic(CARDS_TOPIC), None, name, None)

    def produce_avro(self, topic: str, value: dict, key: str) -> None:
        """Serialize ``value`` for a logical Avro topic and produce it; policy commands follow the configured mode."""
        if topic == POLICY_COMMANDS_TOPIC:
            mode = self.config.policy_commands
            if mode == "disabled":
                return
            physical = topic if mode == "enabled" else self.topic(topic)
        else:
            physical = self.topic(topic)
        self._produce(physical, self._avro_serialize(topic, value), key, None)

    def commit(self, inbound: Inbound) -> None:
        """Commit the offset after inbound, but only once every record produced so far is delivered.

        Raises ``DeliveryFailed`` without committing if a produced record failed; the caller must then
        re-process the same inbound (or stop) before committing anything later. Raises ``CommitFailed``
        if the broker rejects the offset commit.
        """
        remaining = self._producer.flush(_FLUSH_TIMEOUT_S)
        errors, self._delivery_errors = self._delivery_errors, []
        if errors:
            raise DeliveryFailed(f"{len(errors)} record(s) not delivered: {errors[0]}")
        if remaining:
            raise DeliveryFailed(f"{remaining} record(s) still undelivered after {_FLUSH_TIMEOUT_S}s")
        results = self._consumer.commit(
            offsets=[TopicPartition(inbound.topic, inbound.partition, inbound.offset + 1)], asynchronous=False)
        failed = [tp for tp in results or [] if tp.error]
        if failed:
            raise CommitFailed(f"commit rejected for {failed[0].topic}[{failed[0].partition}]: {failed[0].error}")

    def pause(self) -> None:
        """Pause consumption, including partitions assigned by later rebalances, until ``resume``."""
        self._paused = True
        self._consumer.pause(self._consumer.assignment())

    def resume(self) -> None:
        self._paused = False
        self._consumer.resume(self._consumer.assignment())

    def close(self) -> None:
        self._producer.flush(_FLUSH_TIMEOUT_S)
        self._consumer.close()

    def _a2a_headers(self, record: dict) -> dict[str, str]:
        envelope = _a2a_envelope(record) or {}
        headers = {
            "a2a-correlation-id": _a2a_context_id(record),
            "a2a-message-id": record["id"],
            "a2a-reply-topic": self.topic(REPLY_TOPIC),
            "dtm-agent-id": envelope.get("agentId"),
            "dtm-mode": envelope.get("mode"),
        }
        return {k: v for k, v in headers.items() if v is not None}

    def _produce(self, topic: str, value: bytes | None, key: str, headers: dict[str, str] | None) -> None:
        self._producer.produce(
            topic, value=value, key=key.encode("utf-8"),
            headers=[(k, v.encode("utf-8")) for k, v in (headers or {}).items()] or None,
            on_delivery=self._on_delivery)
        self._produced.add(topic)
        self._producer.poll(0)

    def _on_delivery(self, error, message) -> None:
        if error is not None:
            self._delivery_errors.append(error)
