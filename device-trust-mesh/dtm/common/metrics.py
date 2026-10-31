"""Prometheus metrics shared across the mesh."""
from prometheus_client import Histogram

HOP_LATENCY_MS = Histogram(
    "dtm_hop_latency_ms",
    "Milliseconds from a record's envelope eventTime to its consumption by an agent.",
    ["agent", "topic"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)
