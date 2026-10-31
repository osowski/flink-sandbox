"""In-memory stand-ins for the confluent_kafka clients, covering only the methods the mesh uses."""
from dataclasses import dataclass
from types import SimpleNamespace

from confluent_kafka import OFFSET_INVALID, TopicPartition


@dataclass
class Record:
    topic: str
    key: bytes | None
    value: bytes | None
    headers: dict[str, str]


class FakeProducer:
    def __init__(self):
        self.records: list[Record] = []
        self.fail_next_delivery = False
        self.flushed = 0
        self._pending = []

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.records.append(Record(topic, key, value, {k: v.decode() for k, v in headers or []}))
        error = None
        if self.fail_next_delivery:
            self.fail_next_delivery = False
            error = "delivery failed"
        if on_delivery is not None:
            self._pending.append((on_delivery, error, self.records[-1]))

    def poll(self, timeout=0):
        pending, self._pending = self._pending, []
        for callback, error, record in pending:
            callback(error, record)
        return len(pending)

    def flush(self, timeout=None):
        self.poll()
        self.flushed += 1
        return 0


@dataclass
class FakeMessage:
    _topic: str
    _value: bytes | None
    _key: bytes | None
    _headers: list | None
    _partition: int
    _offset: int
    _timestamp_ms: int
    _error: object = None

    def topic(self): return self._topic
    def value(self): return self._value
    def key(self): return self._key
    def headers(self): return self._headers
    def partition(self): return self._partition
    def offset(self): return self._offset
    def timestamp(self): return (1, self._timestamp_ms)
    def error(self): return self._error


def msg(topic, value, key=None, headers=None, partition=0, offset=0, timestamp_ms=0, error=None):
    """Build a fake consumed message; ``headers`` is a dict of str to str."""
    raw_headers = [(k, v.encode()) for k, v in headers.items()] if headers else None
    raw_key = key.encode() if isinstance(key, str) else key
    return FakeMessage(topic, value, raw_key, raw_headers, partition, offset, timestamp_ms, error)


class CommitLog(list):
    """The ``(topic, partition, offset)`` commits made so far; calling it mimics ``Consumer.committed``."""

    def __init__(self, offsets, errors):
        super().__init__()
        self._offsets = offsets
        self._errors = errors

    def __call__(self, partitions, timeout=None):
        return [_tp(p.topic, p.partition, self._offsets.get((p.topic, p.partition), OFFSET_INVALID),
                    self._errors.get((p.topic, p.partition)))
                for p in partitions]


def _tp(topic, partition, offset, error=None):
    return SimpleNamespace(topic=topic, partition=partition, offset=offset, error=error)


class FakeConsumer:
    def __init__(self):
        self.subscribed: list[str] | None = None
        self.assigned: list[TopicPartition] = []
        self.paused: list[TopicPartition] = []
        self.seeks: list[tuple[str, int, int]] = []
        self.committed_offsets: dict[tuple[str, int], int] = {}
        self.committed_errors: dict[tuple[str, int], str] = {}
        self.committed = CommitLog(self.committed_offsets, self.committed_errors)
        self.commit_error = None
        self.commit_raises: Exception | None = None
        self.seek_raises: Exception | None = None
        self.closed = False
        self.time_offsets: dict[tuple[str, int, int], int] = {}
        self.time_errors: dict[tuple[str, int], str] = {}
        self.lookup_timeouts: list[float | None] = []
        self.watermarks: dict[tuple[str, int], tuple[int, int]] = {}
        self.partition_counts: dict[str, int] = {}
        self.positions: dict[tuple[str, int], int] = {}
        self._queue: list[FakeMessage] = []
        self._on_assign = None

    def subscribe(self, topics, on_assign=None):
        self.subscribed = list(topics)
        self._on_assign = on_assign

    def queue(self, msgs):
        self._queue.extend(msgs)

    def trigger_assign(self, partitions):
        """Simulate a rebalance for ``(topic, partition)`` pairs and return the partitions as assigned."""
        parts = [TopicPartition(topic, partition) for topic, partition in partitions]
        if self._on_assign is not None:
            self._on_assign(self, parts)
        else:
            self.assign(parts)
        return parts

    def assign(self, partitions):
        self.assigned = list(partitions)

    def assignment(self):
        return list(self.assigned)

    def consume(self, num_messages=1, timeout=-1):
        batch, self._queue = self._queue[:num_messages], self._queue[num_messages:]
        return batch

    def offsets_for_times(self, partitions, timeout=None):
        self.lookup_timeouts.append(timeout)
        return [_tp(p.topic, p.partition, self.time_offsets.get((p.topic, p.partition, p.offset), -1),
                    self.time_errors.get((p.topic, p.partition)))
                for p in partitions]

    def commit(self, offsets=None, asynchronous=True):
        if self.commit_raises is not None:
            raise self.commit_raises
        results = []
        for tp in offsets or []:
            results.append(_tp(tp.topic, tp.partition, tp.offset, self.commit_error))
            if self.commit_error is None:
                self.committed.append((tp.topic, tp.partition, tp.offset))
        return results

    def pause(self, partitions):
        self.paused = list(partitions)

    def resume(self, partitions):
        self.paused = []

    def seek(self, partition):
        if self.seek_raises is not None:
            raise self.seek_raises
        self.seeks.append((partition.topic, partition.partition, partition.offset))

    def get_watermark_offsets(self, partition, timeout=None, cached=False):
        return self.watermarks.get((partition.topic, partition.partition), (0, 0))

    def position(self, partitions):
        return [_tp(p.topic, p.partition, self.positions.get((p.topic, p.partition), OFFSET_INVALID))
                for p in partitions]

    def list_topics(self, topic=None, timeout=None):
        names = [topic] if topic else list(self.partition_counts)
        return SimpleNamespace(topics={
            n: SimpleNamespace(partitions={i: None for i in range(self.partition_counts.get(n, 0))})
            for n in names})

    def close(self):
        self.closed = True
