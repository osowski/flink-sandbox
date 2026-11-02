"""Feeds the in-memory board: a Kafka reader for the five UI topics and a poller for context-mcp.

The reader owns a private consumer with a unique group id that never commits, reads only committed
records and takes its partitions by assignment, so a restart never replays history except for the short
warm-up window on the two leaderboard topics. The poller reads the device table and the materialization
lag histogram over HTTP. Both run on daemon threads, report an unreachable source to the board as stale
and carry on, and pass every batch of deltas to ``publish``.
"""
import logging
import math
import os
import threading
import time
import uuid
from typing import Callable

import confluent_kafka
import httpx
from confluent_kafka import KafkaError, TopicPartition
from prometheus_client.parser import text_string_to_metric_families

from dtm.common.kafka import bootstrap_servers, consumer_config, schema_registry_url
from dtm.ui.model import Board, Coord, Delta

UI_TOPICS = ("agent.tasks_human_readable", "agent.decisions_human_readable",
             "agent.task-status_human_readable", "agent.pair_counts", "agent.decision_latency")
WARM_TOPICS = ("agent.pair_counts", "agent.decision_latency")
WARM_WINDOW_MS = 300_000

_ROUTES = {"agent.tasks_human_readable": "apply_task",
           "agent.decisions_human_readable": "apply_decision",
           "agent.task-status_human_readable": "apply_status",
           "agent.pair_counts": "apply_pair_counts",
           "agent.decision_latency": "apply_latency"}
_BATCH = 500
_METADATA_TIMEOUT_S = 10.0
_RETRY_BACKOFF_S = 1.0
_STOP_JOIN_S = 5.0
_PROBE_INTERVAL_S = 5.0
_PROBE_TIMEOUT_S = 2.0
_LAG_BUCKET = "dtm_materialization_lag_ms_bucket"
_DEFAULT_CONTEXT_URL = "http://device-trust-mesh-serve-svc.device-trust-mesh.svc.cluster.local:8000/context-mcp"
_HTTP_TIMEOUT_S = 2.0

log = logging.getLogger(__name__)

Publish = Callable[[list[Delta]], None]


def ui_consumer_config(bootstrap: str) -> dict:
    """Return settings for a process-private consumer that starts at the latest offset and never commits."""
    return consumer_config(bootstrap, group_id=f"dtm.ui.{uuid.uuid4()}", auto_offset_reset="latest",
                           enable_auto_commit=False)


class _Worker:
    """A daemon thread that runs ``_step`` until stopped; an exception in a step is logged and retried."""

    _name = "ui-source"
    _retry_backoff_s = _RETRY_BACKOFF_S

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_STOP_JOIN_S)

    def _step(self) -> None:
        raise NotImplementedError

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._step()
                except Exception:
                    log.exception("%s iteration failed, retrying in %ss", self._name, self._retry_backoff_s)
                    self._stop.wait(self._retry_backoff_s)
        finally:
            self._close()

    def _close(self) -> None:
        pass


class TopicReader(_Worker):
    _name = "ui-kafka-reader"

    def __init__(self, board, consumer, deserialize: Callable[[str, bytes | None], dict | None],
                 publish: Publish, clock: Callable[[], float] = time.time):
        super().__init__()
        self._board = board
        self._consumer = consumer
        self._deserialize = deserialize
        self._publish = publish
        self._clock = clock
        self._assigned = False
        self._down = False
        self._probe_at: float | None = None

    @classmethod
    def connect(cls, board: Board, publish: Publish) -> "TopicReader":
        from confluent_kafka import Consumer
        from confluent_kafka.schema_registry import SchemaRegistryClient
        from confluent_kafka.schema_registry.avro import AvroDeserializer
        from confluent_kafka.serialization import MessageField, SerializationContext

        avro = AvroDeserializer(SchemaRegistryClient({"url": schema_registry_url()}))

        def deserialize(topic: str, raw: bytes | None) -> dict | None:
            return avro(raw, SerializationContext(topic, MessageField.VALUE))

        reader: TopicReader

        def error_cb(err) -> None:
            reader.on_error(err)

        consumer = Consumer({**ui_consumer_config(bootstrap_servers()), "error_cb": error_cb})
        reader = cls(board, consumer, deserialize, publish)
        return reader

    def on_error(self, err) -> None:
        """Handle a client-level error; only the loss of every broker marks Kafka down.

        librdkafka delivers these through the error callback, from inside ``consume``, and never as a
        message, so an unreachable broker otherwise looks like an idle topic. A single transport error
        is routine and reconnects on its own, so it is ignored.
        """
        if err.code() == KafkaError._ALL_BROKERS_DOWN:
            log.warning("kafka: %s", err)
            self._down = True

    def start_positions(self) -> list[TopicPartition]:
        """Return where each partition starts: the warm-up window on ``WARM_TOPICS``, the end elsewhere.

        A warm topic partition with no record at or after the window start, or with no records at all,
        starts at the end like every other topic.
        """
        since_ms = self._now_ms() - WARM_WINDOW_MS
        positions = []
        for topic in UI_TOPICS:
            metadata = self._consumer.list_topics(topic, timeout=_METADATA_TIMEOUT_S).topics[topic]
            if getattr(metadata, "error", None):
                raise RuntimeError(f"cannot read the partitions of {topic}: {metadata.error}")
            partitions = sorted(metadata.partitions)
            warm = self._warm_offsets(topic, partitions, since_ms) if topic in WARM_TOPICS else {}
            positions.extend(TopicPartition(topic, p, warm.get(p, confluent_kafka.OFFSET_END))
                             for p in partitions)
        return positions

    def run_once(self, timeout: float = 0.1) -> int:
        """Consume and apply one batch, publish its deltas once, and return the number of records applied."""
        deltas: list[Delta] = []
        applied = 0
        try:
            if not self._assigned:
                self._consumer.assign(self.start_positions())
                self._assigned = True
            batch = self._consumer.consume(num_messages=_BATCH, timeout=timeout)
        except Exception as e:
            log.warning("kafka read failed: %s", e)
            self._mark_down()
            self._finish(self._board.mark_stale("kafka", self._now_ms()))
            return 0
        failed = False
        for m in batch:
            if m.error():
                log.warning("consume error on %s: %s", m.topic(), m.error())
                failed = True
                continue
            applied_deltas = self._apply(m)
            if applied_deltas is not None:
                deltas.extend(applied_deltas)
                applied += 1
        if failed:
            self._mark_down()
        deltas.extend(self._kafka_health(received=bool(batch) and not failed))
        self._finish(deltas)
        return applied

    def _mark_down(self) -> None:
        self._down = True
        self._probe_at = None

    def _kafka_health(self, received: bool) -> list[Delta]:
        """Return the stale or fresh deltas for this batch.

        While Kafka is down it stays stale until there is positive proof it is back: a batch that
        carried records, or a metadata request that succeeds. An empty batch proves nothing.
        """
        if not self._down:
            return []
        now = self._clock()
        if self._probe_at is None:
            self._probe_at = now + _PROBE_INTERVAL_S
        if received or (now >= self._probe_at and self._probe()):
            self._down = False
            self._probe_at = None
            return self._board.mark_fresh("kafka")
        if now >= self._probe_at:
            self._probe_at = now + _PROBE_INTERVAL_S
        return self._board.mark_stale("kafka", self._now_ms())

    def _probe(self) -> bool:
        try:
            self._consumer.list_topics(UI_TOPICS[0], timeout=_PROBE_TIMEOUT_S)
            return True
        except Exception as e:
            log.warning("kafka metadata probe failed: %s", e)
            return False

    def _step(self) -> None:
        self.run_once()

    def _close(self) -> None:
        self._consumer.close()

    def _warm_offsets(self, topic: str, partitions: list[int], since_ms: int) -> dict[int, int]:
        """Return the first offset at or after ``since_ms`` for each partition that has one."""
        asked = [TopicPartition(topic, p, since_ms) for p in partitions]
        found = {}
        for answer in self._consumer.offsets_for_times(asked, timeout=_METADATA_TIMEOUT_S):
            if getattr(answer, "error", None):
                log.warning("cannot seek %s[%s] by time: %s", topic, answer.partition, answer.error)
                continue
            low, high = self._consumer.get_watermark_offsets(
                TopicPartition(topic, answer.partition), timeout=_METADATA_TIMEOUT_S)
            if answer.offset >= 0 and low < high:
                found[answer.partition] = answer.offset
        return found

    def _apply(self, m) -> list[Delta] | None:
        topic, partition, offset = m.topic(), m.partition(), m.offset()
        timestamp_type, timestamp_ms = m.timestamp()
        if timestamp_type == confluent_kafka.TIMESTAMP_NOT_AVAILABLE:
            timestamp_ms = 0
        try:
            value = self._deserialize(topic, m.value())
            if value is None:
                log.warning("skipping empty record topic=%s partition=%s offset=%s", topic, partition, offset)
                return None
            apply = getattr(self._board, _ROUTES[topic])
            return apply(value, Coord(topic, partition, offset, timestamp_ms))
        except Exception as e:
            log.warning("skipping record topic=%s partition=%s offset=%s: %s", topic, partition, offset, e)
            return None

    def _finish(self, deltas: list[Delta]) -> None:
        if deltas:
            self._publish(deltas)

    def _now_ms(self) -> int:
        return round(self._clock() * 1000)


def parse_lag_buckets(metrics_text: str) -> dict[float, float]:
    """Return the cumulative ``dtm_materialization_lag_ms`` bucket counts by ``le``, summed over topics."""
    buckets: dict[float, float] = {}
    for family in text_string_to_metric_families(metrics_text):
        for sample in family.samples:
            if sample.name == _LAG_BUCKET:
                le = float(sample.labels["le"])
                buckets[le] = buckets.get(le, 0.0) + sample.value
    return buckets


def lag_quantiles(previous: dict[float, float] | None,
                  current: dict[float, float]) -> tuple[float | None, float | None]:
    """Return the p50 and p95 of the observations between two scrapes, or ``(None, None)`` if there are none.

    Each is the smallest bucket bound whose cumulative count grew by at least its share of the total growth.
    """
    if previous is None:
        return None, None
    growth = {le: count - previous.get(le, 0.0) for le, count in sorted(current.items())}
    total = growth.get(math.inf, max(growth.values(), default=0.0))
    if total <= 0:
        return None, None

    def quantile(q: float) -> float:
        return next((le for le, grown in growth.items() if grown >= q * total), math.inf)

    return quantile(0.5), quantile(0.95)


def context_base_url() -> str:
    return os.environ.get("DTM_UI_CONTEXT_URL", _DEFAULT_CONTEXT_URL)


class ContextPoller(_Worker):
    _name = "ui-context-poller"

    def __init__(self, board, http: httpx.Client, publish: Publish, base_url: str,
                 interval_s: float = 0.5, metrics_every: int = 10,
                 clock: Callable[[], float] = time.time):
        super().__init__()
        self._board = board
        self._http = http
        self._publish = publish
        self._base_url = base_url.rstrip("/")
        self._interval_s = interval_s
        self._metrics_every = metrics_every
        self._clock = clock
        self._polls = 0
        self._previous_buckets: dict[float, float] | None = None

    def poll_once(self) -> None:
        """Poll the device table, scrape the lag histogram every ``metrics_every`` polls, and publish."""
        now_ms = round(self._clock() * 1000)
        try:
            response = self._http.get(f"{self._base_url}/v1/devices", timeout=_HTTP_TIMEOUT_S)
            if response.status_code != 200:
                raise httpx.HTTPStatusError(f"status {response.status_code}", request=response.request,
                                            response=response)
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError(f"expected a list of devices, got {type(rows).__name__}")
            deltas = self._board.apply_devices(rows, now_ms)
        except Exception as e:
            log.warning("context-mcp device poll failed: %s", e)
            self._publish(self._board.mark_stale("context", now_ms))
            return
        deltas = self._board.mark_fresh("context") + deltas
        self._polls += 1
        if self._polls % self._metrics_every == 0:
            deltas += self._scrape_lag()
        self._publish(deltas)

    def _step(self) -> None:
        self.poll_once()
        self._stop.wait(self._interval_s)

    def _scrape_lag(self) -> list[Delta]:
        try:
            response = self._http.get(f"{self._base_url}/metrics", timeout=_HTTP_TIMEOUT_S)
            if response.status_code != 200:
                raise httpx.HTTPStatusError(f"status {response.status_code}", request=response.request,
                                            response=response)
            current = parse_lag_buckets(response.text)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("context-mcp metrics scrape failed: %s", e)
            self._previous_buckets = None
            return []
        p50, p95 = lag_quantiles(self._previous_buckets, current)
        self._previous_buckets = current
        return self._board.apply_context_lag(p50, p95)
