import threading
import unittest
from datetime import datetime, timezone

from dtm.common.metrics import DEVICE_CAP_EXCEEDED, TRACKED_DEVICES
from dtm.mesh.context.table import SIGNALS_PER_DEVICE, ContextTable

EVENT_TIME = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
EVENT_MS = 1793448000000


def ctx(seq, status="TRUSTED", customer="cust-001"):
    return {"envelope": {"device_id": "dev-0001"}, "customer_id": customer, "context_seq": seq,
            "trust_status": status, "platform": "ios", "os_version": "18.1", "rooted": False,
            "last_country": "US", "last_lat": 37.77, "last_lon": -122.42,
            "last_event_type": "LOGIN", "last_event_time": EVENT_TIME}


def customer_ctx(**over):
    return {"envelope": {"device_id": "dev-0001"}, "device_count": 2, "trusted_device_count": 1,
            "known_countries": "US,CA", "recent_anomaly_count": 0, "account_age_days": 400,
            "last_country": "US", "last_event_time": EVENT_TIME, **over}


def signal(signal_id, device_id, severity="HIGH"):
    return {"envelope": {"device_id": device_id, "event_time": EVENT_TIME}, "signal_id": signal_id,
            "signal_type": "IMPOSSIBLE_TRAVEL", "severity": severity, "detail": "d",
            "source_event_type": "LOGIN"}


def put(t, device_id, value, offset=0, partition=0):
    t.apply("device.context", partition, offset, {"device_id": device_id}, value, 1)


class ContextTableTest(unittest.TestCase):
    def test_apply_and_get(self):
        t = ContextTable(20)
        t.apply("device.context", 0, 4, {"device_id": "dev-0001"}, ctx(5), 1)
        r = t.get_device("dev-0001")
        self.assertEqual((r["status"], r["device"]["context_seq"]), ("OK", 5))
        self.assertIs(type(r["device"]["last_event_time"]), int)
        self.assertEqual(r["device"]["last_event_time"], EVENT_MS)
        self.assertEqual(r["as_of"]["sourceOffsets"],
                         [{"topic": "device.context", "partition": 0, "offset": 4}])

    def test_device_row_has_exactly_the_documented_keys(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(5))
        self.assertEqual(sorted(t.get_device("dev-0001")["device"]), sorted([
            "device_id", "customer_id", "context_seq", "trust_status", "platform", "os_version",
            "rooted", "last_country", "last_lat", "last_lon", "last_event_type",
            "last_event_time"]))

    def test_null_last_event_time_stays_none(self):
        t = ContextTable(20)
        put(t, "dev-0001", {**ctx(5), "last_event_time": None})
        self.assertIsNone(t.get_device("dev-0001")["device"]["last_event_time"])

    def test_as_of_is_camel_case_with_materialized_at_from_clock(self):
        t = ContextTable(20, clock=lambda: 1793448000.5)
        self.assertEqual(t.as_of("dev-0001"),
                         {"contextSeq": None, "materializedAt": 0, "sourceOffsets": []})
        put(t, "dev-0001", ctx(5), offset=3)
        self.assertEqual(t.as_of("dev-0001"), {
            "contextSeq": 5, "materializedAt": 1793448000500,
            "sourceOffsets": [{"topic": "device.context", "partition": 0, "offset": 3}]})
        self.assertIsNone(t.as_of(None)["contextSeq"])

    def test_null_context_seq_reads_as_none(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(None))
        r = t.get_device("dev-0001")
        self.assertEqual((r["status"], r["as_of"]["contextSeq"]), ("OK", None))

    def test_source_offsets_sorted_per_topic_partition(self):
        t = ContextTable(20)
        t.apply("device.risk-signals", 1, 7, None, signal("s-1", "dev-0001"), 1)
        put(t, "dev-0001", ctx(5), offset=2, partition=1)
        put(t, "dev-0002", ctx(1), offset=9, partition=0)
        put(t, "dev-0002", ctx(2), offset=10, partition=0)
        self.assertEqual(t.offsets(), {("device.context", 0): 10, ("device.context", 1): 2,
                                       ("device.risk-signals", 1): 7})
        self.assertEqual([(o["topic"], o["partition"], o["offset"])
                          for o in t.as_of(None)["sourceOffsets"]],
                         [("device.context", 0, 10), ("device.context", 1, 2),
                          ("device.risk-signals", 1, 7)])

    def test_tombstone_deletes_and_unindexes(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(5))
        t.apply("device.context", 0, 5, {"device_id": "dev-0001"}, None, 2)
        self.assertEqual(t.get_device("dev-0001")["status"], "NOT_FOUND")
        self.assertEqual(t.list_devices("cust-001"), [])

    def test_customer_change_moves_secondary_index(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(5, customer="cust-001"))
        put(t, "dev-0001", ctx(6, customer="cust-002"), offset=1)
        self.assertEqual(t.list_devices("cust-001"), [])
        self.assertEqual([d["device_id"] for d in t.list_devices("cust-002")], ["dev-0001"])

    def test_min_seq_satisfied_immediately(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(5))
        self.assertEqual(t.get_device("dev-0001", min_seq=5)["status"], "OK")

    def test_min_seq_waits_for_materializer(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(4))
        threading.Timer(0.05, lambda: t.apply(
            "device.context", 0, 6, {"device_id": "dev-0001"}, ctx(6), 3)).start()
        r = t.get_device("dev-0001", min_seq=6, wait_timeout_ms=2000)
        self.assertEqual((r["status"], r["device"]["context_seq"]), ("OK", 6))

    def test_min_seq_waits_for_a_device_that_appears(self):
        t = ContextTable(20)
        threading.Timer(0.05, lambda: put(t, "dev-0001", ctx(6))).start()
        r = t.get_device("dev-0001", min_seq=6, wait_timeout_ms=2000)
        self.assertEqual((r["status"], r["device"]["context_seq"]), ("OK", 6))

    def test_min_seq_times_out_stale(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(4))
        r = t.get_device("dev-0001", min_seq=9, wait_timeout_ms=50)
        self.assertEqual(r["status"], "STALE")
        self.assertEqual(r["as_of"]["contextSeq"], 4)
        self.assertEqual(r["device"]["context_seq"], 4)

    def test_min_seq_for_unknown_device_waits_then_stale(self):
        r = ContextTable(20).get_device("dev-0009", min_seq=1, wait_timeout_ms=50)
        self.assertEqual((r["status"], r["device"]), ("STALE", None))

    def test_null_seq_does_not_satisfy_min_seq(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(None))
        self.assertEqual(t.get_device("dev-0001", min_seq=1, wait_timeout_ms=30)["status"], "STALE")

    def test_non_positive_timeout_does_not_wait(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(4))
        done = []
        thread = threading.Thread(
            target=lambda: done.append(t.get_device("dev-0001", min_seq=9, wait_timeout_ms=0)))
        thread.start()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done[0]["status"], "STALE")

    def test_waiting_lookup_does_not_block_other_lookups(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(4))
        waiter = threading.Thread(
            target=lambda: t.get_device("dev-0001", min_seq=9, wait_timeout_ms=1500))
        waiter.start()
        try:
            self.assertEqual(t.get_device("dev-0001")["status"], "OK")
            put(t, "dev-0002", ctx(1), offset=1)
            self.assertEqual(t.get_device("dev-0002")["status"], "OK")
        finally:
            waiter.join()

    def test_risk_signals_bounded_newest_first(self):
        t = ContextTable(20)
        for i in range(60):
            t.apply("device.risk-signals", 0, i, None, signal(f"s-{i}", "dev-0001"), i)
        got = t.list_risk_signals("dev-0001", limit=100)
        self.assertEqual(SIGNALS_PER_DEVICE, 50)
        self.assertEqual(len(got), 50)
        self.assertEqual(got[0]["signal_id"], "s-59")
        self.assertEqual(got[-1]["signal_id"], "s-10")
        self.assertEqual([s["signal_id"] for s in t.list_risk_signals("dev-0001", limit=3)],
                         ["s-59", "s-58", "s-57"])

    def test_risk_signal_row_shape(self):
        t = ContextTable(20)
        t.apply("device.risk-signals", 0, 1, None, signal("s-1", "dev-0001"), 1)
        self.assertEqual(t.list_risk_signals("dev-0001"), [{
            "signal_id": "s-1", "signal_type": "IMPOSSIBLE_TRAVEL", "severity": "HIGH",
            "detail": "d", "source_event_type": "LOGIN", "event_time": EVENT_MS}])
        self.assertEqual(t.list_risk_signals("dev-0404"), [])

    def test_duplicate_signal_ignored(self):
        t = ContextTable(20)
        t.apply("device.risk-signals", 0, 1, None, signal("s-1", "dev-0001"), 1)
        t.apply("device.risk-signals", 3, 9, None, signal("s-1", "dev-0001"), 2)
        self.assertEqual(len(t.list_risk_signals("dev-0001")), 1)
        self.assertEqual(t.offsets()[("device.risk-signals", 3)], 9)

    def test_signals_are_kept_per_device(self):
        t = ContextTable(20)
        t.apply("device.risk-signals", 0, 1, None, signal("s-1", "dev-0001"), 1)
        t.apply("device.risk-signals", 0, 2, None, signal("s-1", "dev-0002"), 2)
        self.assertEqual(len(t.list_risk_signals("dev-0001")), 1)
        self.assertEqual(len(t.list_risk_signals("dev-0002")), 1)

    def test_customer_context_with_device_list(self):
        t = ContextTable(20)
        put(t, "dev-0002", ctx(2, status="BLOCKED"), offset=1)
        put(t, "dev-0001", ctx(5))
        t.apply("customer.context", 0, 0, {"customer_id": "cust-001"}, customer_ctx(), 1)
        self.assertEqual(t.get_customer("cust-001"), {"status": "OK", "customer": {
            "customer_id": "cust-001", "device_count": 2, "trusted_device_count": 1,
            "known_countries": "US,CA", "recent_anomaly_count": 0, "account_age_days": 400,
            "last_country": "US", "last_event_time": EVENT_MS,
            "devices": [{"device_id": "dev-0001", "trust_status": "TRUSTED"},
                        {"device_id": "dev-0002", "trust_status": "BLOCKED"}]}})

    def test_unknown_customer_not_found(self):
        self.assertEqual(ContextTable(20).get_customer("cust-9"),
                         {"status": "NOT_FOUND", "customer": None})

    def test_customer_tombstone_deletes(self):
        t = ContextTable(20)
        t.apply("customer.context", 0, 0, {"customer_id": "cust-001"}, customer_ctx(), 1)
        t.apply("customer.context", 0, 1, {"customer_id": "cust-001"}, None, 2)
        self.assertEqual(t.get_customer("cust-001")["status"], "NOT_FOUND")

    def test_list_devices_filters(self):
        t = ContextTable(20)
        put(t, "dev-0003", ctx(1, status="BLOCKED", customer="cust-002"), offset=0)
        put(t, "dev-0002", ctx(1, status="TRUSTED", customer="cust-001"), offset=1)
        put(t, "dev-0001", ctx(1, status="BLOCKED", customer="cust-001"), offset=2)
        ids = lambda rows: [d["device_id"] for d in rows]
        self.assertEqual(ids(t.list_devices()), ["dev-0001", "dev-0002", "dev-0003"])
        self.assertEqual(ids(t.list_devices("cust-001")), ["dev-0001", "dev-0002"])
        self.assertEqual(ids(t.list_devices(trust_status="BLOCKED")), ["dev-0001", "dev-0003"])
        self.assertEqual(ids(t.list_devices("cust-001", "BLOCKED")), ["dev-0001"])
        self.assertEqual(t.list_devices("cust-404"), [])

    def test_unknown_topic_rejected(self):
        with self.assertRaises(ValueError):
            ContextTable(20).apply("nope", 0, 0, None, {}, 1)

    def test_cap_exceeded_flag(self):
        t = ContextTable(1)
        put(t, "dev-0001", ctx(1))
        self.assertEqual(DEVICE_CAP_EXCEEDED._value.get(), 0)
        with self.assertLogs("dtm.mesh.context.table", "ERROR") as logs:
            put(t, "dev-0002", ctx(1), offset=1)
            put(t, "dev-0003", ctx(1), offset=2)
        self.assertEqual(DEVICE_CAP_EXCEEDED._value.get(), 1)
        self.assertEqual(len(logs.records), 1)

    def test_cap_counts_distinct_ids_ever_seen_and_new_table_resets(self):
        t = ContextTable(1)
        put(t, "dev-0001", ctx(1))
        t.apply("device.context", 0, 1, {"device_id": "dev-0001"}, None, 2)
        with self.assertLogs("dtm.mesh.context.table", "ERROR"):
            put(t, "dev-0002", ctx(1), offset=2)
        self.assertEqual(DEVICE_CAP_EXCEEDED._value.get(), 1)
        ContextTable(5)
        self.assertEqual(DEVICE_CAP_EXCEEDED._value.get(), 0)

    def test_tracked_devices_gauge_follows_rows(self):
        t = ContextTable(20)
        put(t, "dev-0001", ctx(1))
        put(t, "dev-0002", ctx(1), offset=1)
        self.assertEqual(TRACKED_DEVICES._value.get(), 2)
        t.apply("device.context", 0, 2, {"device_id": "dev-0001"}, None, 2)
        self.assertEqual(TRACKED_DEVICES._value.get(), 1)


if __name__ == "__main__":
    unittest.main()
