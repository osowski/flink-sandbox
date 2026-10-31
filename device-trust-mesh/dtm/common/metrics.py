"""Prometheus metrics shared across the mesh."""
from prometheus_client import Counter, Histogram

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
