import asyncio
import threading
import unittest
from unittest import mock

from agent_fakes import context_table
from dtm.common.context_client import ContextClient
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.context.server import build_mcp
from mcp.client.client import Client

DEVICES = [{"device_id": "dev-0001"}, {"device_id": "dev-0002", "trust_status": "SUSPECT"}]
CUSTOMERS = [{"customer_id": "cust-001"}]
SIGNALS = [{"device_id": "dev-0001", "signal_id": "s1"}, {"device_id": "dev-0001", "signal_id": "s2"}]


class SyncContextTest(unittest.TestCase):
    def setUp(self):
        table = context_table(devices=DEVICES, customers=CUSTOMERS, signals=SIGNALS)
        self.ctx = SyncContext(build_mcp(table, wait_timeout_ms=50), timeout_s=5.0)
        self.addCleanup(self.ctx.close)

    def test_device_lookup_returns_as_of(self):
        result = self.ctx.get_device_context("dev-0001", min_seq=7)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["as_of"]["contextSeq"], 7)
        self.assertEqual(result["device"]["trust_status"], "TRUSTED")

    def test_unreached_sequence_is_stale(self):
        self.assertEqual(self.ctx.get_device_context("dev-0001", min_seq=99)["status"], "STALE")

    def test_customer_and_signal_lookups(self):
        customer = self.ctx.get_customer_context("cust-001")
        self.assertEqual(customer["status"], "OK")
        self.assertEqual([d["device_id"] for d in customer["customer"]["devices"]], ["dev-0001", "dev-0002"])
        signals = self.ctx.list_risk_signals("dev-0001", limit=1)
        self.assertEqual([s["signal_id"] for s in signals], ["s2"])
        self.assertEqual(self.ctx.list_risk_signals("dev-0002"), [])

    def test_calls_from_many_threads_share_the_session(self):
        with mock.patch("dtm.mesh.agents.context.ContextClient", wraps=ContextClient) as client:
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.ctx.get_device_context("dev-0001")))
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual([r["status"] for r in results], ["OK"] * 4)
        self.assertEqual(client.call_count, 1)

    def test_reconnects_after_failure(self):
        original = Client.call_tool
        failures = []

        async def flaky(client, *args, **kwargs):
            if not failures:
                failures.append(1)
                raise RuntimeError("transport down")
            return await original(client, *args, **kwargs)

        with mock.patch("dtm.mesh.agents.context.ContextClient", wraps=ContextClient) as opened, \
                mock.patch.object(Client, "call_tool", flaky):
            with self.assertRaisesRegex(RuntimeError, "transport down"):
                self.ctx.get_device_context("dev-0001")
            self.assertEqual(self.ctx.get_device_context("dev-0001")["status"], "OK")
        self.assertEqual(opened.call_count, 2)

    def test_call_that_exceeds_the_timeout_raises_and_the_next_one_succeeds(self):
        gate = threading.Event()
        original = Client.call_tool

        async def slow(client, *args, **kwargs):
            if not gate.is_set():
                gate.set()
                await asyncio.sleep(30)
            return await original(client, *args, **kwargs)

        quick = SyncContext(build_mcp(context_table(devices=DEVICES), wait_timeout_ms=50), timeout_s=0.3)
        self.addCleanup(quick.close)
        with mock.patch.object(Client, "call_tool", slow):
            with self.assertRaises(TimeoutError):
                quick.get_device_context("dev-0001")
            self.assertEqual(quick.get_device_context("dev-0001")["status"], "OK")

    def test_close_is_idempotent(self):
        self.ctx.get_device_context("dev-0001")
        self.ctx.close()
        self.ctx.close()

    def test_close_stops_the_loop_thread(self):
        self.ctx.get_device_context("dev-0001")
        thread = self.ctx._thread
        self.assertTrue(thread.is_alive())
        self.ctx.close()
        self.assertFalse(thread.is_alive())

    def test_close_without_a_session(self):
        SyncContext(build_mcp(context_table()), timeout_s=5.0).close()

    def test_call_after_close_raises(self):
        self.ctx.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.ctx.get_device_context("dev-0001")


if __name__ == "__main__":
    unittest.main()
