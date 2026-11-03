import math
import threading
import unittest
import unittest.mock

import confluent_kafka
import httpx
from confluent_kafka import KafkaError

from dtm.ui.model import Board, Coord
from dtm.ui.sources import (UI_TOPICS, WARM_TOPICS, ContextPoller, TopicReader, context_base_url,
                            lag_quantiles, parse_lag_buckets, ui_consumer_config)
from fakes import FakeConsumer, msg

NOW_S = 1000.0
NOW_MS = 1_000_000
SINCE_MS = NOW_MS - 300_000
BASE = "http://context.test/context-mcp"


class RecordingBoard:
    """Records each call; ``apply_*`` returns one delta, ``mark_*`` none, so routing is visible without the real board."""

    def __init__(self):
        self.calls = []
        self.marks = []

    def __getattr__(self, name):
        if not name.startswith(("apply_", "mark_")):
            raise AttributeError(name)

        def call(*args):
            if name.startswith("mark_"):
                self.marks.append((name, *args))
                return []
            self.calls.append((name, *args))
            return [(name, {})]
        return call


def decode(topic, raw):
    if raw == b"bad":
        raise ValueError("not avro")
    return None if raw is None else {"raw": raw}


def reader(board=None, partitions=None, deserialize=decode):
    consumer = FakeConsumer()
    consumer.partition_counts = partitions or {t: 1 for t in UI_TOPICS}
    consumer.watermarks = {(t, p): (0, 10) for t, n in consumer.partition_counts.items() for p in range(n)}
    published = []
    board = board if board is not None else RecordingBoard()
    return TopicReader(board, consumer, deserialize, published.append, clock=lambda: NOW_S), published


class ConfigTest(unittest.TestCase):
    def test_ui_consumer_config_is_read_committed_unique_group(self):
        a, b = ui_consumer_config("kafka:9071"), ui_consumer_config("kafka:9071")
        self.assertEqual(a["isolation.level"], "read_committed")
        self.assertEqual(a["bootstrap.servers"], "kafka:9071")
        self.assertEqual(a["auto.offset.reset"], "latest")
        self.assertFalse(a["enable.auto.commit"])
        self.assertTrue(a["group.id"].startswith("dtm.ui."))
        self.assertNotEqual(a["group.id"], b["group.id"])

    def test_context_base_url_defaults_and_overrides(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                context_base_url(),
                "http://device-trust-mesh-serve-svc.device-trust-mesh.svc.cluster.local:8000/context-mcp")
        with unittest.mock.patch.dict("os.environ", {"DTM_UI_CONTEXT_URL": "http://x/y"}):
            self.assertEqual(context_base_url(), "http://x/y")


class StartPositionsTest(unittest.TestCase):
    def test_start_positions_warm_topics_seek_by_time(self):
        r, _ = reader(partitions={**{t: 1 for t in UI_TOPICS}, "agent.pair_counts": 2})
        for p, offset in ((0, 40), (1, 55)):
            r._consumer.time_offsets[("agent.pair_counts", p, SINCE_MS)] = offset
        r._consumer.time_offsets[("agent.decision_latency", 0, SINCE_MS)] = 7
        positions = {(tp.topic, tp.partition): tp.offset for tp in r.start_positions()}
        self.assertEqual(positions[("agent.pair_counts", 0)], 40)
        self.assertEqual(positions[("agent.pair_counts", 1)], 55)
        self.assertEqual(positions[("agent.decision_latency", 0)], 7)
        for topic in set(UI_TOPICS) - set(WARM_TOPICS):
            self.assertEqual(positions[(topic, 0)], confluent_kafka.OFFSET_END)
        self.assertEqual(len(positions), 6)

    def test_start_positions_partition_without_recent_records_seeks_end(self):
        r, _ = reader()
        r._consumer.time_offsets[("agent.pair_counts", 0, SINCE_MS)] = -1
        r._consumer.watermarks[("agent.decision_latency", 0)] = (5, 5)
        r._consumer.time_offsets[("agent.decision_latency", 0, SINCE_MS)] = 5
        positions = {(tp.topic, tp.partition): tp.offset for tp in r.start_positions()}
        self.assertEqual(positions[("agent.pair_counts", 0)], confluent_kafka.OFFSET_END)
        self.assertEqual(positions[("agent.decision_latency", 0)], confluent_kafka.OFFSET_END)

    def test_start_positions_lookup_error_seeks_end(self):
        r, _ = reader()
        r._consumer.time_offsets[("agent.pair_counts", 0, SINCE_MS)] = 3
        r._consumer.time_errors[("agent.pair_counts", 0)] = "broker unavailable"
        positions = {(tp.topic, tp.partition): tp.offset for tp in r.start_positions()}
        self.assertEqual(positions[("agent.pair_counts", 0)], confluent_kafka.OFFSET_END)

    def test_assigns_once_without_subscribing(self):
        r, _ = reader()
        r.run_once()
        first = r._consumer.assigned
        self.assertEqual(len(first), len(UI_TOPICS))
        self.assertIsNone(r._consumer.subscribed)
        r.run_once()
        self.assertIs(r._consumer.assigned, first)


class RunOnceTest(unittest.TestCase):
    def test_run_once_routes_each_topic_to_board(self):
        r, published = reader()
        expected = {"agent.tasks_human_readable": "apply_task",
                    "agent.decisions_human_readable": "apply_decision",
                    "agent.task-status_human_readable": "apply_status",
                    "agent.pair_counts": "apply_pair_counts",
                    "agent.decision_latency": "apply_latency"}
        r._consumer.queue([msg(t, f"v-{i}".encode(), partition=i, offset=10 + i, timestamp_ms=100 + i)
                           for i, t in enumerate(UI_TOPICS)])
        self.assertEqual(r.run_once(), len(UI_TOPICS))
        calls = r._board.calls
        self.assertEqual([c[0] for c in calls], [expected[t] for t in UI_TOPICS])
        for i, (name, value, coord) in enumerate(calls):
            self.assertEqual(value, {"raw": f"v-{i}".encode()})
            self.assertEqual(coord, Coord(UI_TOPICS[i], i, 10 + i, 100 + i))
        self.assertEqual(len(published), 1)
        self.assertEqual(len(published[0]), len(UI_TOPICS))

    def test_undecodable_record_is_skipped(self):
        r, published = reader()
        r._consumer.queue([msg("agent.pair_counts", b"bad", offset=1),
                           msg("agent.pair_counts", None, offset=2),
                           msg("agent.pair_counts", b"good", offset=3)])
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            self.assertEqual(r.run_once(), 1)
        applied = [c for c in r._board.calls if c[0] == "apply_pair_counts"]
        self.assertEqual([c[2].offset for c in applied], [3])
        self.assertEqual(len(published), 1)

    def test_a_board_failure_skips_the_record(self):
        board = Board(clock=lambda: NOW_S)
        r, published = reader(board=board, deserialize=lambda topic, raw: {"unexpected": 1})
        r._consumer.queue([msg("agent.pair_counts", b"x")])
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            self.assertEqual(r.run_once(), 0)

    def test_idle_batch_publishes_nothing(self):
        r, published = reader()
        self.assertEqual(r.run_once(), 0)
        self.assertEqual(published, [])

    def test_consume_error_marks_kafka_stale_then_fresh(self):
        board = Board(clock=lambda: NOW_S)
        r, published = reader(board=board, deserialize=lambda topic, raw: {"v": raw})
        r._consumer.queue([msg("agent.pair_counts", None, error="broker transport failure")])
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            self.assertEqual(r.run_once(), 0)
        self.assertEqual(published, [[("stale", {"source": "kafka", "since_ms": NOW_MS})]])
        self.assertEqual(board.snapshot()["stale"]["kafka"], NOW_MS)
        r.run_once()
        self.assertEqual(board.snapshot()["stale"]["kafka"], NOW_MS)
        r._consumer.queue([msg("agent.pair_counts", b"ok", offset=1)])
        r.run_once()
        self.assertIsNone(board.snapshot()["stale"]["kafka"])
        self.assertEqual(published[-1][-1], ("stale", {"source": "kafka", "since_ms": None}))

    def test_consume_exception_marks_stale_and_does_not_raise(self):
        r, published = reader()

        def boom(num_messages=1, timeout=-1):
            raise RuntimeError("consumer is closed")
        r._consumer.consume = boom
        self.assertEqual(r.run_once(), 0)
        self.assertEqual(r._board.marks, [("mark_stale", "kafka", NOW_MS)])

    def test_assign_failure_marks_stale_and_retries(self):
        r, published = reader()
        original = r._consumer.list_topics

        def failing(topic=None, timeout=None):
            raise RuntimeError("no metadata")
        r._consumer.list_topics = failing
        self.assertEqual(r.run_once(), 0)
        self.assertEqual(r._consumer.assigned, [])
        r._consumer.list_topics = original
        r.run_once()
        self.assertEqual(len(r._consumer.assigned), len(UI_TOPICS))


class BrokerLossTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW_S
        self.board = Board(clock=lambda: self.now)
        row = {"window_start": NOW_MS - 60_000, "window_end": NOW_MS, "from_agent": "a", "to_agent": "b", "message_count": 3}
        self.reader, self.published = reader(board=self.board, deserialize=lambda topic, raw: row)
        self.reader._clock = lambda: self.now
        self.reader.run_once()

    def stale(self):
        return self.board.snapshot()["stale"]["kafka"]

    def all_brokers_down(self):
        self.reader.on_error(KafkaError(KafkaError._ALL_BROKERS_DOWN, "all brokers down"))

    def test_all_brokers_down_marks_stale_on_next_run_once(self):
        self.all_brokers_down()
        self.reader.run_once()
        self.assertEqual(self.stale(), NOW_MS)
        self.assertEqual(self.published, [[("stale", {"source": "kafka", "since_ms": NOW_MS})]])

    def test_callback_fires_inside_consume(self):
        real = self.reader._consumer.consume

        def consume(num_messages=1, timeout=-1):
            self.all_brokers_down()
            return real(num_messages, timeout)
        self.reader._consumer.consume = consume
        self.reader.run_once()
        self.assertEqual(self.stale(), NOW_MS)

    def test_empty_batches_while_down_stay_stale(self):
        self.all_brokers_down()
        self.reader._consumer.list_topics = unittest.mock.Mock(side_effect=RuntimeError("timed out"))
        for _ in range(5):
            self.reader.run_once()
            self.now += 1
        self.assertEqual(self.stale(), NOW_MS)
        self.assertEqual(len(self.published), 1)

    def test_batch_with_messages_clears_stale(self):
        self.all_brokers_down()
        self.reader.run_once()
        self.reader._consumer.queue([msg("agent.pair_counts", b"x", offset=1)])
        self.assertEqual(self.reader.run_once(), 1)
        self.assertIsNone(self.stale())
        self.assertIn(("stale", {"source": "kafka", "since_ms": None}), self.published[-1])

    def test_successful_probe_clears_stale(self):
        self.all_brokers_down()
        self.reader.run_once()
        self.now += 1
        self.reader.run_once()
        self.assertEqual(self.stale(), NOW_MS)
        self.now += 10
        self.reader.run_once()
        self.assertIsNone(self.stale())

    def test_failed_probe_keeps_stale_and_retries(self):
        self.all_brokers_down()
        self.reader.run_once()
        original = self.reader._consumer.list_topics
        self.reader._consumer.list_topics = unittest.mock.Mock(side_effect=RuntimeError("timed out"))
        self.now += 10
        self.reader.run_once()
        self.assertEqual(self.stale(), NOW_MS)
        self.reader._consumer.list_topics = original
        self.now += 10
        self.reader.run_once()
        self.assertIsNone(self.stale())

    def test_probe_is_not_attempted_on_every_batch(self):
        self.all_brokers_down()
        self.reader.run_once()
        self.reader._consumer.list_topics = unittest.mock.Mock(side_effect=RuntimeError("timed out"))
        for _ in range(20):
            self.reader.run_once()
        self.reader._consumer.list_topics.assert_not_called()

    def test_lone_transport_error_does_not_mark_stale(self):
        self.reader.on_error(KafkaError(KafkaError._TRANSPORT, "connection reset"))
        self.reader.run_once()
        self.assertIsNone(self.stale())
        self.assertEqual(self.published, [])

    def test_healthy_empty_batches_publish_nothing(self):
        for _ in range(3):
            self.reader.run_once()
        self.assertEqual(self.published, [])

    def test_connect_wires_the_error_callback(self):
        captured = {}

        class Capturing:
            def __init__(self, config):
                captured.update(config)

        with unittest.mock.patch("confluent_kafka.Consumer", Capturing), \
                unittest.mock.patch("confluent_kafka.schema_registry.SchemaRegistryClient"), \
                unittest.mock.patch("confluent_kafka.schema_registry.avro.AvroDeserializer"):
            connected = TopicReader.connect(RecordingBoard(), lambda deltas: None)
        captured["error_cb"](KafkaError(KafkaError._ALL_BROKERS_DOWN, "down"))
        self.assertTrue(connected._down)
        self.assertEqual(captured["isolation.level"], "read_committed")


class ReaderThreadTest(unittest.TestCase):
    def test_thread_survives_errors_and_stop_closes_consumer(self):
        r, _ = reader()
        r._retry_backoff_s = 0.01
        calls = threading.Event()
        count = []

        def flaky(timeout=0.1):
            count.append(1)
            if len(count) == 1:
                raise RuntimeError("boom")
            calls.set()
            return 0
        r.run_once = flaky
        with self.assertLogs("dtm.ui.sources", "ERROR"):
            thread = r.start()
            self.assertTrue(calls.wait(2))
        r.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(thread.daemon)
        self.assertTrue(r._consumer.closed)


BUCKETS_TEXT = """\
# HELP dtm_materialization_lag_ms Lag.
# TYPE dtm_materialization_lag_ms histogram
dtm_materialization_lag_ms_bucket{le="50.0",topic="device.context"} 2.0
dtm_materialization_lag_ms_bucket{le="100.0",topic="device.context"} 3.0
dtm_materialization_lag_ms_bucket{le="+Inf",topic="device.context"} 4.0
dtm_materialization_lag_ms_count{topic="device.context"} 4.0
dtm_materialization_lag_ms_sum{topic="device.context"} 300.0
dtm_materialization_lag_ms_bucket{le="50.0",topic="customer.context"} 1.0
dtm_materialization_lag_ms_bucket{le="100.0",topic="customer.context"} 1.0
dtm_materialization_lag_ms_bucket{le="+Inf",topic="customer.context"} 2.0
dtm_other_bucket{le="50.0"} 99.0
"""


class LagTest(unittest.TestCase):
    def test_parse_lag_buckets_sums_topics(self):
        self.assertEqual(parse_lag_buckets(BUCKETS_TEXT), {50.0: 3.0, 100.0: 4.0, math.inf: 6.0})

    def test_parse_lag_buckets_without_the_histogram_is_empty(self):
        self.assertEqual(parse_lag_buckets("# nothing\n"), {})

    def test_lag_quantiles_from_delta(self):
        previous = {50.0: 0, 100.0: 0, math.inf: 0}
        current = {50.0: 6, 100.0: 10, math.inf: 10}
        self.assertEqual(lag_quantiles(previous, current), (50.0, 100.0))

    def test_lag_quantiles_uses_change_not_totals(self):
        previous = {50.0: 100, 100.0: 100, math.inf: 100}
        current = {50.0: 100, 100.0: 110, math.inf: 110}
        self.assertEqual(lag_quantiles(previous, current), (100.0, 100.0))

    def test_lag_quantiles_tail_in_inf(self):
        previous = {50.0: 0, math.inf: 0}
        current = {50.0: 9, math.inf: 10}
        self.assertEqual(lag_quantiles(previous, current), (50.0, math.inf))

    def test_lag_quantiles_no_change_is_none(self):
        same = {50.0: 3, math.inf: 3}
        self.assertEqual(lag_quantiles(same, dict(same)), (None, None))
        self.assertEqual(lag_quantiles(None, same), (None, None))


DEVICE = {"device_id": "dev-0001", "customer_id": "c-1", "trust_status": "TRUSTED", "context_seq": 1,
          "last_event_time": 999_000, "last_event_type": "LOGIN"}


def poller(handler, board=None, metrics_every=10):
    board = board if board is not None else RecordingBoard()
    published = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ContextPoller(board, http, published.append, BASE, metrics_every=metrics_every,
                         clock=lambda: NOW_S), published


class PollerTest(unittest.TestCase):
    def test_poll_once_applies_devices(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=[DEVICE])
        p, published = poller(handler)
        p.poll_once()
        self.assertEqual(seen, [f"{BASE}/v1/devices"])
        self.assertEqual(p._board.calls, [("apply_devices", [DEVICE], NOW_MS)])
        self.assertEqual(len(published), 1)

    def test_poll_error_reports_stale_and_continues(self):
        responses = iter([httpx.Response(503), httpx.Response(200, json=[DEVICE])])
        board = Board(clock=lambda: NOW_S)
        p, published = poller(lambda request: next(responses), board=board)
        p.poll_once()
        self.assertEqual(board.snapshot()["stale"]["context"], NOW_MS)
        self.assertEqual(published, [[("stale", {"source": "context", "since_ms": NOW_MS})]])
        p.poll_once()
        snapshot = board.snapshot()
        self.assertIsNone(snapshot["stale"]["context"])
        self.assertEqual([d["device_id"] for d in snapshot["devices"]], ["dev-0001"])
        self.assertEqual(len(published), 2)
        self.assertIn(("stale", {"source": "context", "since_ms": None}), published[1])

    def test_non_list_body_marks_stale_and_publishes(self):
        for body in ({"devices": []}, "text", None):
            p, published = poller(lambda request, body=body: httpx.Response(200, json=body))
            with self.assertLogs("dtm.ui.sources", "WARNING"):
                p.poll_once()
            self.assertEqual(p._board.marks, [("mark_stale", "context", NOW_MS)])
            self.assertEqual(p._board.calls, [])
            self.assertEqual(len(published), 1)

    def test_apply_failure_marks_stale_publishes_and_does_not_mark_fresh(self):
        board = Board(clock=lambda: NOW_S)
        p, published = poller(lambda request: httpx.Response(200, json=[{"device_id": "d"}]), board=board)
        board.mark_stale("context", 1)
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            p.poll_once()
        self.assertEqual(board.snapshot()["stale"]["context"], 1)
        self.assertEqual(len(published), 1)

    def test_fresh_is_marked_only_after_devices_apply(self):
        board = Board(clock=lambda: NOW_S)
        board.mark_stale("context", 1)
        p, published = poller(lambda request: httpx.Response(200, json=[DEVICE]), board=board)
        p.poll_once()
        self.assertIsNone(board.snapshot()["stale"]["context"])
        self.assertEqual(len(published), 1)

    def test_transport_error_and_bad_body_report_stale(self):
        def refuse(request):
            raise httpx.ConnectError("refused")
        p, published = poller(refuse)
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            p.poll_once()
        self.assertEqual(p._board.marks, [("mark_stale", "context", NOW_MS)])
        p, _ = poller(lambda request: httpx.Response(200, text="not json"))
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            p.poll_once()
        self.assertEqual(p._board.marks, [("mark_stale", "context", NOW_MS)])

    def test_metrics_scraped_every_n_polls(self):
        scrapes = []
        metrics = iter([
            'dtm_materialization_lag_ms_bucket{le="50.0",topic="t"} 0\n'
            'dtm_materialization_lag_ms_bucket{le="+Inf",topic="t"} 0\n',
            'dtm_materialization_lag_ms_bucket{le="50.0",topic="t"} 5\n'
            'dtm_materialization_lag_ms_bucket{le="+Inf",topic="t"} 5\n'])

        def handler(request):
            if request.url.path.endswith("/metrics"):
                scrapes.append(len(p._board.calls))
                return httpx.Response(200, text=next(metrics))
            return httpx.Response(200, json=[])
        p, _ = poller(handler, metrics_every=3)
        for _ in range(6):
            p.poll_once()
        self.assertEqual(len(scrapes), 2)
        lags = [c for c in p._board.calls if c[0] == "apply_context_lag"]
        self.assertEqual(lags, [("apply_context_lag", None, None), ("apply_context_lag", 50.0, 50.0)])
        self.assertEqual([c[0] for c in p._board.calls].count("apply_devices"), 6)

    def test_metrics_failure_does_not_mark_context_stale(self):
        def handler(request):
            if request.url.path.endswith("/metrics"):
                return httpx.Response(500)
            return httpx.Response(200, json=[])
        p, _ = poller(handler, metrics_every=1)
        with self.assertLogs("dtm.ui.sources", "WARNING"):
            p.poll_once()
        self.assertEqual(p._board.marks, [("mark_fresh", "context")])

    def test_thread_survives_errors_and_stops(self):
        p, _ = poller(lambda request: httpx.Response(200, json=[]))
        p._interval_s = 0.01
        ran = threading.Event()
        count = []

        def flaky():
            count.append(1)
            if len(count) == 1:
                raise RuntimeError("boom")
            ran.set()
        p.poll_once = flaky
        with self.assertLogs("dtm.ui.sources", "ERROR"):
            thread = p.start()
            self.assertTrue(ran.wait(2))
        p.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(thread.daemon)


if __name__ == "__main__":
    unittest.main()
