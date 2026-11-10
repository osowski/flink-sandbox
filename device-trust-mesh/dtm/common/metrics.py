"""Prometheus metrics shared across the mesh.

Each metric is a prometheus_client metric, served at the app's /metrics route, and
is mirrored into ray.util.metrics so each Ray node's metrics agent exports every
Serve replica's series as ``ray_<name>``. Without Ray installed (the simulator and
UI images) or outside a Ray worker, the mirror records nothing.
"""
from collections.abc import Sequence

from prometheus_client import Counter, Gauge, Histogram

try:
    from ray.util import metrics as _ray
except ImportError:
    _ray = None


class _Recorder:
    """Records to a prometheus_client metric (or child) and its Ray mirror; other attributes come from the former."""

    def __init__(self, prom, mirror, tags: dict[str, str] | None):
        self._prom, self._mirror, self._tags = prom, mirror, tags

    def observe(self, value: float) -> None:
        self._prom.observe(value)
        if self._mirror is not None:
            self._mirror.observe(value, tags=self._tags)

    def inc(self, amount: float = 1) -> None:
        self._prom.inc(amount)
        if self._mirror is not None:
            self._mirror.inc(amount, tags=self._tags)

    def set(self, value: float) -> None:
        self._prom.set(value)
        if self._mirror is not None:
            self._mirror.set(value, tags=self._tags)

    def __getattr__(self, name):
        return getattr(self._prom, name)


class _Metric(_Recorder):
    def __init__(self, prom, mirror, labelnames: Sequence[str]):
        super().__init__(prom, mirror, None)
        self._labelnames = tuple(labelnames)

    def labels(self, *values) -> _Recorder:
        tags = dict(zip(self._labelnames, (str(v) for v in values)))
        return _Recorder(self._prom.labels(*values), self._mirror, tags)


def histogram(name: str, doc: str, labelnames: Sequence[str] = (), *,
              buckets: Sequence[float], ray=_ray) -> _Metric:
    mirror = ray.Histogram(name, doc, boundaries=list(buckets), tag_keys=tuple(labelnames)) if ray else None
    return _Metric(Histogram(name, doc, labelnames, buckets=buckets), mirror, labelnames)


def counter(name: str, doc: str, labelnames: Sequence[str] = (), *, ray=_ray) -> _Metric:
    mirror = ray.Counter(name, doc, tag_keys=tuple(labelnames)) if ray else None
    return _Metric(Counter(name, doc, labelnames), mirror, labelnames)


def gauge(name: str, doc: str, labelnames: Sequence[str] = (), *, ray=_ray) -> _Metric:
    mirror = ray.Gauge(name, doc, tag_keys=tuple(labelnames)) if ray else None
    return _Metric(Gauge(name, doc, labelnames), mirror, labelnames)

HOP_LATENCY_MS = histogram(
    "dtm_hop_latency_ms",
    "Milliseconds from a record's envelope eventTime to its consumption by an agent.",
    ["agent", "topic"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

HANDLER_ERRORS = counter(
    "dtm_handler_errors_total",
    "Messages whose handler raised; the message is committed and the agent reports the failure itself.",
    ["agent"],
)

LOOP_ERRORS = counter(
    "dtm_loop_errors_total",
    "Exceptions that escaped a poll-loop iteration; the loop backs off and continues.",
    ["agent"],
)

INVALID_REQUESTS = counter(
    "dtm_invalid_requests",
    "Inbound records an agent skipped as tombstones, non-requests or schema-invalid.",
    ["agent"],
)

PROCESSING_LATENCY_MS = histogram(
    "dtm_agent_processing_latency_ms",
    "Milliseconds an agent's handler took on one inbound record, whether it succeeded or failed.",
    ["agent"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

LLM_LATENCY_MS = histogram(
    "dtm_llm_latency_ms",
    "Milliseconds spent in one LLM completion call, by agent, model and outcome status.",
    ["agent", "model", "status"],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

TRACKED_DEVICES = gauge(
    "dtm_tracked_devices",
    "Device rows currently held in the context table.",
)

DEVICE_CAP_EXCEEDED = gauge(
    "dtm_device_cap_exceeded",
    "1 once the number of distinct device ids seen on device.context exceeds the configured cap.",
)

MATERIALIZATION_LAG_MS = histogram(
    "dtm_materialization_lag_ms",
    "Milliseconds from a context record's Kafka timestamp to its application to the context table.",
    ["topic"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

MATERIALIZER_SKIPPED = counter(
    "dtm_materializer_skipped_total",
    "Context records the materializer could not deserialize or apply and skipped.",
    ["topic"],
)

CONTEXT_TOOL_LATENCY_MS = histogram(
    "dtm_context_tool_latency_ms",
    "Milliseconds a context-mcp tool call took, including any wait for a context sequence.",
    ["tool"],
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)

CONTEXT_STALE_TOTAL = counter(
    "dtm_context_stale_total",
    "Device lookups that returned STALE because the requested context sequence was not materialized in time.",
)
