"""Loads the context topics into the in-memory table.

Each replica reads every partition of every context topic from the beginning with its own
unique group id and never commits, so every replica holds the full context and a restart
replays the topics. The materializer is ready once it has consumed everything that was
already in the topics when it started.
"""
import logging
import threading
import time
import uuid
from typing import Callable

import confluent_kafka
from confluent_kafka import TopicPartition

from dtm.common.kafka import bootstrap_servers, consumer_config, schema_registry_url
from dtm.common.metrics import MATERIALIZATION_LAG_MS, MATERIALIZER_SKIPPED
from dtm.mesh.context.table import ContextTable

CONTEXT_TOPICS = ("device.context", "customer.context", "device.risk-signals")

_KEYED_TOPICS = ("device.context", "customer.context")
_BATCH = 500
_METADATA_TIMEOUT_S = 10.0
_RETRY_BACKOFF_S = 1.0

log = logging.getLogger(__name__)


def materializer_consumer_config(bootstrap: str) -> dict:
    """Return settings for a replica-private consumer that replays the context topics and never commits."""
    return consumer_config(bootstrap, group_id=f"dtm.context-mcp.{uuid.uuid4()}",
                           auto_offset_reset="earliest", enable_auto_commit=False)


class Materializer:
    def __init__(self, table: ContextTable, consumer,
                 deserialize_key: Callable[[str, bytes | None], dict | None],
                 deserialize_value: Callable[[str, bytes | None], dict | None],
                 clock: Callable[[], float] = time.time):
        self._table = table
        self._consumer = consumer
        self._deserialize_key = deserialize_key
        self._deserialize_value = deserialize_value
        self._clock = clock
        self._targets: dict[tuple[str, int], int] | None = None
        self._consumed: dict[tuple[str, int], int] = {}
        self._stop = threading.Event()
        self._retry_backoff_s = _RETRY_BACKOFF_S
        self.ready = threading.Event()

    @classmethod
    def connect(cls, table: ContextTable) -> "Materializer":
        from confluent_kafka import Consumer
        from confluent_kafka.schema_registry import SchemaRegistryClient
        from confluent_kafka.schema_registry.avro import AvroDeserializer
        from confluent_kafka.serialization import MessageField, SerializationContext

        avro = AvroDeserializer(SchemaRegistryClient({"url": schema_registry_url()}))

        def deserialize_key(topic: str, raw: bytes | None) -> dict | None:
            if topic not in _KEYED_TOPICS:
                return None
            return avro(raw, SerializationContext(topic, MessageField.KEY))

        def deserialize_value(topic: str, raw: bytes | None) -> dict | None:
            return avro(raw, SerializationContext(topic, MessageField.VALUE))

        consumer = Consumer(materializer_consumer_config(bootstrap_servers()))
        return cls(table, consumer, deserialize_key, deserialize_value)

    def catch_up_targets(self) -> dict[tuple[str, int], int]:
        """Return the last offset to consume on each partition that holds records; empty partitions are omitted."""
        return self._scan()[1]

    def run_once(self, timeout: float = 0.1) -> int:
        """Consume and apply one batch; return the number of records applied to the table."""
        if self._targets is None:
            self._begin()
        applied = 0
        for m in self._consumer.consume(num_messages=_BATCH, timeout=timeout):
            if m.error():
                log.warning("consume error on %s: %s", m.topic(), m.error())
                continue
            if self._apply(m):
                applied += 1
            self._consumed[(m.topic(), m.partition())] = m.offset()
        self._check_ready()
        return applied

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="context-materializer", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _begin(self) -> None:
        partitions, targets = self._scan()
        self._consumer.assign([TopicPartition(topic, partition, confluent_kafka.OFFSET_BEGINNING)
                               for topic, partition in partitions])
        self._targets = targets

    def _scan(self) -> tuple[list[tuple[str, int]], dict[tuple[str, int], int]]:
        partitions, targets = [], {}
        for topic in CONTEXT_TOPICS:
            metadata = self._consumer.list_topics(topic, timeout=_METADATA_TIMEOUT_S).topics[topic]
            if getattr(metadata, "error", None):
                raise RuntimeError(f"cannot read the partitions of {topic}: {metadata.error}")
            for partition in sorted(metadata.partitions):
                low, high = self._consumer.get_watermark_offsets(
                    TopicPartition(topic, partition), timeout=_METADATA_TIMEOUT_S)
                partitions.append((topic, partition))
                if low < high:
                    targets[(topic, partition)] = high - 1
        return partitions, targets

    def _apply(self, m) -> bool:
        topic, partition, offset = m.topic(), m.partition(), m.offset()
        timestamp_type, timestamp_ms = m.timestamp()
        try:
            key = self._deserialize_key(topic, m.key()) if topic in _KEYED_TOPICS else None
            value = self._deserialize_value(topic, m.value())
            self._table.apply(topic, partition, offset, key, value,
                              timestamp_ms if timestamp_type != confluent_kafka.TIMESTAMP_NOT_AVAILABLE else 0)
        except Exception as e:
            MATERIALIZER_SKIPPED.labels(topic).inc()
            log.warning("skipping record topic=%s partition=%s offset=%s: %s", topic, partition, offset, e)
            return False
        if timestamp_type != confluent_kafka.TIMESTAMP_NOT_AVAILABLE:
            MATERIALIZATION_LAG_MS.labels(topic).observe(self._clock() * 1000 - timestamp_ms)
        return True

    def _check_ready(self) -> None:
        if self.ready.is_set():
            return
        pending = [tp for tp, target in self._targets.items() if self._consumed.get(tp, -1) < target]
        if pending:
            self._pass_trailing_control_records(pending)
            pending = [tp for tp in pending if self._consumed.get(tp, -1) < self._targets[tp]]
        if not pending:
            self.ready.set()

    def _pass_trailing_control_records(self, pending: list[tuple[str, int]]) -> None:
        """Count a partition as consumed once the consumer position is past its high watermark.

        A transactional writer ends each partition with a commit marker that is never delivered
        as a record, so the last offset to consume may not be reachable by records alone.
        """
        positions = self._consumer.position([TopicPartition(topic, partition) for topic, partition in pending])
        for position in positions:
            tp = (position.topic, position.partition)
            if position.offset > self._targets[tp]:
                self._consumed[tp] = self._targets[tp]

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    log.exception("materializer iteration failed, retrying in %ss", self._retry_backoff_s)
                    self._stop.wait(self._retry_backoff_s)
        finally:
            self._consumer.close()
