import threading
import time
import unittest
from datetime import datetime, timezone

import confluent_kafka
from prometheus_client import REGISTRY

from dtm.mesh.context.materializer import (CONTEXT_TOPICS, Materializer, materializer_consumer_config)
from dtm.mesh.context.table import ContextTable
from fakes import FakeConsumer, msg

EVENT_TIME = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def ctx(seq, customer="cust-001"):
    return {"envelope": {"device_id": "dev-0001"}, "customer_id": customer, "context_seq": seq,
            "trust_status": "TRUSTED", "platform": "ios", "os_version": "18.1", "rooted": False,
            "last_country": "US", "last_lat": 37.77, "last_lon": -122.42,
            "last_event_type": "LOGIN", "last_event_time": EVENT_TIME}


def signal(signal_id, device_id="dev-0001"):
    return {"envelope": {"device_id": device_id, "event_time": EVENT_TIME}, "signal_id": signal_id,
            "signal_type": "IMPOSSIBLE_TRAVEL", "severity": "HIGH", "detail": "d",
            "source_event_type": "LOGIN"}


def device_msg(offset, value, device_id="dev-0001", partition=0, timestamp_ms=0):
    return msg("device.context", value, key={"device_id": device_id}, partition=partition,
               offset=offset, timestamp_ms=timestamp_ms)


def records(topic, partition, offsets):
    return [device_msg(o, ctx(o + 1), partition=partition) for o in offsets]


def passthrough(topic, raw):
    return raw


def materializer(partitions=None, high=None, deserialize_value=passthrough, clock=time.time):
    consumer = FakeConsumer()
    consumer.partition_counts = partitions or {"device.context": 1, "customer.context": 1,
                                               "device.risk-signals": 1}
    consumer.watermarks = {tp: (0, h) for tp, h in (high or {}).items()}
    table = ContextTable(20)
    return Materializer(table, consumer, passthrough, deserialize_value, clock=clock)


def metric(name, topic):
    return REGISTRY.get_sample_value(name, {"topic": topic}) or 0.0


class MaterializerTest(unittest.TestCase):
    def test_assigns_every_partition_from_beginning_without_subscribe(self):
        m = materializer(partitions={"device.context": 6, "customer.context": 6,
                                     "device.risk-signals": 6})
        m.run_once()
        self.assertEqual(len(m._consumer.assigned), 18)
        self.assertEqual({(tp.topic, tp.partition) for tp in m._consumer.assigned},
                         {(t, p) for t in CONTEXT_TOPICS for p in range(6)})
        self.assertTrue(all(tp.offset == confluent_kafka.OFFSET_BEGINNING
                            for tp in m._consumer.assigned))
        self.assertIsNone(m._consumer.subscribed)

    def test_assigns_once(self):
        m = materializer()
        m.run_once()
        first = m._consumer.assigned
        m.run_once()
        self.assertIs(m._consumer.assigned, first)

    def test_catch_up_targets_are_high_watermark_minus_one_for_non_empty_partitions(self):
        m = materializer(partitions={"device.context": 2, "customer.context": 1,
                                     "device.risk-signals": 1},
                         high={("device.context", 0): 3, ("customer.context", 0): 1})
        m._consumer.watermarks[("device.context", 1)] = (7, 7)
        self.assertEqual(m.catch_up_targets(),
                         {("device.context", 0): 2, ("customer.context", 0): 0})

    def test_ready_only_after_reaching_startup_high_watermarks(self):
        m = materializer(high={("device.context", 0): 3})
        m._consumer.queue(records("device.context", 0, offsets=[0, 1]))
        m.run_once()
        self.assertFalse(m.ready.is_set())
        m._consumer.queue(records("device.context", 0, offsets=[2]))
        m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_every_partition_with_data_must_catch_up(self):
        m = materializer(partitions={"device.context": 2, "customer.context": 1,
                                     "device.risk-signals": 1},
                         high={("device.context", 0): 1, ("device.context", 1): 1})
        m._consumer.queue(records("device.context", 0, offsets=[0]))
        m.run_once()
        self.assertFalse(m.ready.is_set())
        m._consumer.queue(records("device.context", 1, offsets=[0]))
        m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_empty_partitions_are_ready_immediately(self):
        m = materializer(high={})
        m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_partition_whose_start_offset_equals_its_high_watermark_is_caught_up(self):
        m = materializer()
        m._consumer.watermarks[("device.context", 0)] = (5, 5)
        m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_consumer_position_past_trailing_control_records_completes_catch_up(self):
        m = materializer(high={("device.context", 0): 3})
        m._consumer.queue(records("device.context", 0, offsets=[0, 1]))
        m.run_once()
        self.assertFalse(m.ready.is_set())
        m._consumer.positions[("device.context", 0)] = 3
        m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_records_and_tombstones_reach_table(self):
        m = materializer(high={("device.context", 0): 2, ("customer.context", 0): 1,
                               ("device.risk-signals", 0): 1})
        m._consumer.queue([
            device_msg(0, ctx(1)),
            msg("customer.context", {"envelope": {"device_id": "x"}, "device_count": 1,
                                     "trusted_device_count": 1, "known_countries": "US",
                                     "recent_anomaly_count": 0, "account_age_days": 3,
                                     "last_country": "US", "last_event_time": EVENT_TIME},
                key={"customer_id": "cust-001"}, offset=0),
            msg("device.risk-signals", signal("s-1"), key=b"dev-0001", offset=0)])
        self.assertEqual(m.run_once(), 3)
        table = m._table
        self.assertEqual(table.get_device("dev-0001")["status"], "OK")
        self.assertEqual(table.get_customer("cust-001")["status"], "OK")
        self.assertEqual([s["signal_id"] for s in table.list_risk_signals("dev-0001")], ["s-1"])
        m._consumer.queue([device_msg(1, None)])
        m.run_once()
        self.assertEqual(table.get_device("dev-0001")["status"], "NOT_FOUND")
        self.assertEqual(table.offsets()[("device.context", 0)], 1)

    def test_risk_signal_key_is_ignored(self):
        keys = []
        m = materializer(high={("device.risk-signals", 0): 1})
        m._deserialize_key = lambda topic, raw: keys.append(topic) or raw
        m._consumer.queue([msg("device.risk-signals", signal("s-1"), key=b"dev-0001", offset=0)])
        m.run_once()
        self.assertEqual(keys, [])
        self.assertEqual(len(m._table.list_risk_signals("dev-0001")), 1)

    def test_values_are_deserialized_with_their_topic(self):
        seen = []

        def deserialize(topic, raw):
            seen.append((topic, raw))
            return ctx(raw)

        m = materializer(high={("device.context", 0): 1}, deserialize_value=deserialize)
        m._consumer.queue([device_msg(0, 7)])
        m.run_once()
        self.assertEqual(seen, [("device.context", 7)])
        self.assertEqual(m._table.get_device("dev-0001")["device"]["context_seq"], 7)

    def test_malformed_record_is_skipped_counted_and_does_not_block_readiness(self):
        before = metric("dtm_materializer_skipped_total", "device.context")
        m = materializer(high={("device.context", 0): 2})
        m._consumer.queue([device_msg(0, {"customer_id": "only-this"}), device_msg(1, ctx(9))])
        with self.assertLogs("dtm.mesh.context.materializer", "WARNING") as logs:
            applied = m.run_once()
        self.assertEqual(applied, 1)
        self.assertTrue(m.ready.is_set())
        self.assertEqual(m._table.get_device("dev-0001")["device"]["context_seq"], 9)
        self.assertEqual(metric("dtm_materializer_skipped_total", "device.context") - before, 1)
        self.assertIn("device.context", logs.output[0])
        self.assertIn("partition=0", logs.output[0])
        self.assertIn("offset=0", logs.output[0])

    def test_skipped_last_record_still_counts_as_consumed(self):
        m = materializer(high={("device.context", 0): 2})
        m._consumer.queue([device_msg(0, ctx(1)), device_msg(1, {"bad": True})])
        with self.assertLogs("dtm.mesh.context.materializer", "WARNING"):
            m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_deserializer_failure_is_skipped_and_counted(self):
        def deserialize(topic, raw):
            if raw == "poison":
                raise ValueError("bad avro")
            return raw

        before = metric("dtm_materializer_skipped_total", "device.context")
        m = materializer(high={("device.context", 0): 2}, deserialize_value=deserialize)
        m._consumer.queue([device_msg(0, "poison"), device_msg(1, ctx(2))])
        with self.assertLogs("dtm.mesh.context.materializer", "WARNING"):
            m.run_once()
        self.assertTrue(m.ready.is_set())
        self.assertEqual(m._table.get_device("dev-0001")["device"]["context_seq"], 2)
        self.assertEqual(metric("dtm_materializer_skipped_total", "device.context") - before, 1)

    def test_consume_errors_are_logged_and_skipped(self):
        m = materializer(high={("device.context", 0): 1})
        m._consumer.queue([msg("device.context", None, error="broker down"),
                           device_msg(0, ctx(1))])
        with self.assertLogs("dtm.mesh.context.materializer", "WARNING"):
            m.run_once()
        self.assertTrue(m.ready.is_set())

    def test_materializer_consumer_reads_committed(self):
        cfg = materializer_consumer_config("b:9")
        self.assertEqual(cfg["isolation.level"], "read_committed")
        self.assertIs(cfg["enable.auto.commit"], False)
        self.assertEqual(cfg["auto.offset.reset"], "earliest")
        self.assertEqual(cfg["bootstrap.servers"], "b:9")
        self.assertTrue(cfg["group.id"].startswith("dtm.context-mcp."))
        self.assertNotEqual(cfg["group.id"], materializer_consumer_config("b:9")["group.id"])

    def test_lag_metric_observed(self):
        before_count = metric("dtm_materialization_lag_ms_count", "device.context")
        before_sum = metric("dtm_materialization_lag_ms_sum", "device.context")
        m = materializer(clock=lambda: 10.0)
        m.run_once()
        self.assertTrue(m.ready.is_set())
        m._consumer.queue([device_msg(0, ctx(1), timestamp_ms=9_250)])
        m.run_once()
        self.assertEqual(metric("dtm_materialization_lag_ms_count", "device.context") - before_count, 1)
        self.assertEqual(metric("dtm_materialization_lag_ms_sum", "device.context") - before_sum, 750)

    def test_lag_not_observed_during_startup_replay(self):
        before = metric("dtm_materialization_lag_ms_count", "device.context")
        m = materializer(high={("device.context", 0): 3}, clock=lambda: 10.0)
        m._consumer.queue([device_msg(o, ctx(o + 1), timestamp_ms=1_000) for o in (0, 1)])
        m.run_once()
        m._consumer.queue([device_msg(2, ctx(3), timestamp_ms=1_000)])
        m.run_once()
        self.assertTrue(m.ready.is_set())
        self.assertEqual(metric("dtm_materialization_lag_ms_count", "device.context"), before)
        m._consumer.queue([device_msg(3, ctx(4), timestamp_ms=9_900)])
        m.run_once()
        self.assertEqual(metric("dtm_materialization_lag_ms_count", "device.context") - before, 1)

    def test_lag_not_observed_for_skipped_record(self):
        before = metric("dtm_materialization_lag_ms_count", "device.context")
        m = materializer(high={("device.context", 0): 1})
        m._consumer.queue([device_msg(0, {"bad": 1})])
        with self.assertLogs("dtm.mesh.context.materializer", "WARNING"):
            m.run_once()
        self.assertEqual(metric("dtm_materialization_lag_ms_count", "device.context"), before)

    def test_thread_materializes_until_stopped_and_closes_the_consumer(self):
        m = materializer(high={("device.context", 0): 1})
        m._consumer.queue([device_msg(0, ctx(1))])
        thread = m.start()
        self.assertIsInstance(thread, threading.Thread)
        self.assertTrue(thread.daemon)
        self.assertTrue(m.ready.wait(2))
        m.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(m._consumer.closed)

    def test_thread_survives_an_iteration_error(self):
        m = materializer(high={("device.context", 0): 1})
        calls = []
        real = m._consumer.consume

        def flaky(num_messages=1, timeout=-1):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return real(num_messages, timeout)

        m._consumer.consume = flaky
        m._consumer.queue([device_msg(0, ctx(1))])
        m._retry_backoff_s = 0.01
        with self.assertLogs("dtm.mesh.context.materializer", "ERROR"):
            thread = m.start()
            self.assertTrue(m.ready.wait(2))
        m.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_failed_startup_is_retried_without_assigning(self):
        m = materializer()
        real = m._consumer.list_topics
        attempts = []

        def flaky(topic=None, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("metadata unavailable")
            return real(topic, timeout)

        m._consumer.list_topics = flaky
        with self.assertRaises(RuntimeError):
            m.run_once()
        self.assertEqual(m._consumer.assigned, [])
        m.run_once()
        self.assertEqual(len(m._consumer.assigned), 3)
        self.assertTrue(m.ready.is_set())


if __name__ == "__main__":
    unittest.main()
