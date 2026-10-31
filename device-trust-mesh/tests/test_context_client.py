import asyncio
import time
import unittest
from datetime import datetime, timezone

from dtm.common.context_client import ContextClient
from dtm.mesh.context.server import build_mcp
from dtm.mesh.context.table import ContextTable

EVENT_TIME = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def ctx(seq, status="TRUSTED"):
    return {"envelope": {"device_id": "dev-0001"}, "customer_id": "cust-001", "context_seq": seq,
            "trust_status": status, "platform": "ios", "os_version": "18.1", "rooted": False,
            "last_country": "US", "last_lat": 37.77, "last_lon": -122.42,
            "last_event_type": "LOGIN", "last_event_time": EVENT_TIME}


def seeded_table():
    table = ContextTable(20)
    table.apply("device.context", 0, 0, {"device_id": "dev-0001"}, ctx(5), 1)
    table.apply("device.context", 0, 1, {"device_id": "dev-0002"}, ctx(3, "SUSPECT"), 1)
    table.apply("customer.context", 0, 0, {"customer_id": "cust-001"},
                {"envelope": {"device_id": "dev-0001"}, "device_count": 2, "trusted_device_count": 1,
                 "known_countries": "US", "recent_anomaly_count": 0, "account_age_days": 400,
                 "last_country": "US", "last_event_time": EVENT_TIME}, 1)
    table.apply("device.risk-signals", 0, 0, None,
                {"envelope": {"device_id": "dev-0001", "event_time": EVENT_TIME}, "signal_id": "s1",
                 "signal_type": "IMPOSSIBLE_TRAVEL", "severity": "HIGH", "detail": "d",
                 "source_event_type": "LOGIN"}, 1)
    return table


class ContextClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_device_context_via_client(self):
        async with ContextClient(build_mcp(seeded_table())) as cc:
            r = await cc.get_device_context("dev-0001", min_seq=5)
            self.assertEqual((r["status"], r["device"]["trust_status"]), ("OK", "TRUSTED"))
            self.assertEqual(set(r["as_of"]), {"contextSeq", "materializedAt", "sourceOffsets"})

    async def test_other_tools(self):
        async with ContextClient(build_mcp(seeded_table())) as cc:
            self.assertEqual((await cc.get_customer_context("cust-001"))["status"], "OK")
            self.assertEqual([s["signal_id"] for s in await cc.list_risk_signals("dev-0001", 5)],
                             ["s1"])
            self.assertEqual([d["device_id"] for d in await cc.list_devices(trust_status="SUSPECT")],
                             ["dev-0002"])
            self.assertEqual(len(await cc.list_devices()), 2)

    async def test_waiting_lookup_does_not_block_others(self):
        server = build_mcp(seeded_table(), wait_timeout_ms=1000)
        async with ContextClient(server, stale_retries=0) as a, ContextClient(server) as b:
            t0 = time.monotonic()
            slow = asyncio.create_task(a.get_device_context("dev-0001", min_seq=99))
            await asyncio.sleep(0.05)
            await b.get_device_context("dev-0002")
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual((await slow)["status"], "STALE")

    async def test_client_retries_stale(self):
        table = seeded_table()
        server = build_mcp(table, wait_timeout_ms=30)
        asyncio.get_running_loop().call_later(
            0.05, lambda: table.apply("device.context", 0, 2, {"device_id": "dev-0001"}, ctx(7), 1))
        async with ContextClient(server, stale_retries=5) as cc:
            self.assertEqual((await cc.get_device_context("dev-0001", min_seq=7))["status"], "OK")

    async def test_stale_returned_after_retries_exhausted(self):
        server = build_mcp(seeded_table(), wait_timeout_ms=5)
        async with ContextClient(server, stale_retries=2) as cc:
            r = await cc.get_device_context("dev-0001", min_seq=99)
        self.assertEqual(r["status"], "STALE")

    async def test_not_found_is_not_retried(self):
        table = seeded_table()
        calls = []
        real = table.get_device
        table.get_device = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        async with ContextClient(build_mcp(table, wait_timeout_ms=5), stale_retries=3) as cc:
            r = await cc.get_device_context("nope")
        self.assertEqual((r["status"], len(calls)), ("NOT_FOUND", 1))

    async def test_tool_error_raises(self):
        async with ContextClient(build_mcp(seeded_table())) as cc:
            with self.assertLogs("mcp.server.mcpserver.server", "INFO"), self.assertRaises(RuntimeError) as e:
                await cc.get_device_context("dev-0001", min_seq="not-an-int")
        self.assertIn("get_device_context", str(e.exception))

    async def test_empty_result_raises_naming_the_tool(self):
        class Empty:
            is_error = False
            structured_content = None
            content = []

        class Stub:
            async def call_tool(self, tool, args):
                return Empty()

        cc = ContextClient(build_mcp(seeded_table()))
        cc._client = Stub()
        with self.assertRaisesRegex(RuntimeError, "get_customer_context"):
            await cc.get_customer_context("cust-001")

    async def test_use_outside_context_raises(self):
        with self.assertRaises(RuntimeError):
            await ContextClient(build_mcp(seeded_table())).list_devices()


if __name__ == "__main__":
    unittest.main()
