import unittest

from prometheus_client import REGISTRY

from dtm.common import metrics


class FakeRayMetric:
    def __init__(self, name, description, boundaries=None, tag_keys=()):
        self.name, self.boundaries, self.tag_keys = name, boundaries, tuple(tag_keys)
        self.calls = []

    def observe(self, value, tags=None):
        self.calls.append(("observe", value, tags))

    def inc(self, value=1.0, tags=None):
        self.calls.append(("inc", value, tags))

    def set(self, value, tags=None):
        self.calls.append(("set", value, tags))


class FakeRay:
    Histogram = Counter = Gauge = FakeRayMetric


class MirrorTest(unittest.TestCase):
    def test_histogram_records_in_both_with_string_tags(self):
        h = metrics.histogram("dtm_test_mirror_ms", "d", ["agent", "topic"], buckets=(1, 5), ray=FakeRay)
        h.labels("risk", 7).observe(3)
        self.assertEqual(h._mirror.boundaries, [1, 5])
        self.assertEqual(h._mirror.tag_keys, ("agent", "topic"))
        self.assertEqual(h._mirror.calls, [("observe", 3, {"agent": "risk", "topic": "7"})])
        self.assertEqual(REGISTRY.get_sample_value(
            "dtm_test_mirror_ms_count", {"agent": "risk", "topic": "7"}), 1)

    def test_counter_keeps_the_exposed_name(self):
        c = metrics.counter("dtm_test_mirror_total", "d", ["agent"], ray=FakeRay)
        c.labels("risk").inc()
        self.assertEqual(c._mirror.name, "dtm_test_mirror_total")
        self.assertEqual(c._mirror.calls, [("inc", 1, {"agent": "risk"})])
        self.assertEqual(REGISTRY.get_sample_value("dtm_test_mirror_total", {"agent": "risk"}), 1)

    def test_unlabelled_gauge_and_counter(self):
        g = metrics.gauge("dtm_test_mirror_gauge", "d", ray=FakeRay)
        g.set(4)
        c = metrics.counter("dtm_test_mirror_plain_total", "d", ray=FakeRay)
        c.inc()
        self.assertEqual(g._mirror.calls, [("set", 4, None)])
        self.assertEqual(c._mirror.calls, [("inc", 1, None)])
        self.assertEqual(g._value.get(), 4)

    def test_without_ray_records_only_prometheus(self):
        h = metrics.histogram("dtm_test_noray_ms", "d", ["agent"], buckets=(1,), ray=None)
        h.labels("risk").observe(0.5)
        self.assertIsNone(h._mirror)
        self.assertEqual(REGISTRY.get_sample_value("dtm_test_noray_ms_count", {"agent": "risk"}), 1)

    def test_mesh_metrics_are_mirrored_when_ray_is_installed(self):
        for metric in (metrics.HOP_LATENCY_MS, metrics.CONTEXT_STALE_TOTAL, metrics.TRACKED_DEVICES):
            with self.subTest(metric=metric._prom._name):
                self.assertIsNotNone(metric._mirror)


if __name__ == "__main__":
    unittest.main()
