"""Prometheus metrics shared across the mesh."""
from prometheus_client import Counter, Gauge, Histogram

HOP_LATENCY_MS = Histogram(
    "dtm_hop_latency_ms",
    "Milliseconds from a record's envelope eventTime to its consumption by an agent.",
    ["agent", "topic"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

HANDLER_ERRORS = Counter(
    "dtm_handler_errors_total",
    "Messages whose handler raised; the message is committed and the agent reports the failure itself.",
    ["agent"],
)

LOOP_ERRORS = Counter(
    "dtm_loop_errors_total",
    "Exceptions that escaped a poll-loop iteration; the loop backs off and continues.",
    ["agent"],
)

INVALID_REQUESTS = Counter(
    "dtm_invalid_requests",
    "Inbound records an agent skipped as tombstones, non-requests or schema-invalid.",
    ["agent"],
)

PROCESSING_LATENCY_MS = Histogram(
    "dtm_agent_processing_latency_ms",
    "Milliseconds an agent's handler took on one inbound record, whether it succeeded or failed.",
    ["agent"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

LLM_LATENCY_MS = Histogram(
    "dtm_llm_latency_ms",
    "Milliseconds spent in one LLM completion call, by agent, model and outcome status.",
    ["agent", "model", "status"],
    buckets=(50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

TRACKED_DEVICES = Gauge(
    "dtm_tracked_devices",
    "Device rows currently held in the context table.",
)

DEVICE_CAP_EXCEEDED = Gauge(
    "dtm_device_cap_exceeded",
    "1 once the number of distinct device ids seen on device.context exceeds the configured cap.",
)

MATERIALIZATION_LAG_MS = Histogram(
    "dtm_materialization_lag_ms",
    "Milliseconds from a context record's Kafka timestamp to its application to the context table.",
    ["topic"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

MATERIALIZER_SKIPPED = Counter(
    "dtm_materializer_skipped_total",
    "Context records the materializer could not deserialize or apply and skipped.",
    ["topic"],
)

CONTEXT_TOOL_LATENCY_MS = Histogram(
    "dtm_context_tool_latency_ms",
    "Milliseconds a context-mcp tool call took, including any wait for a context sequence.",
    ["tool"],
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)

CONTEXT_STALE_TOTAL = Counter(
    "dtm_context_stale_total",
    "Device lookups that returned STALE because the requested context sequence was not materialized in time.",
)
