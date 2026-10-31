import asyncio
import threading
import time
import unittest
from unittest import mock

from agent_fakes import context_table
from dtm.common.context_client import ContextClient
from dtm.mesh.agents import context as context_module
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

    def test_call_queued_behind_a_stuck_lock_holder_times_out(self):
        ctx = SyncContext(build_mcp(context_table(devices=DEVICES), wait_timeout_ms=50), timeout_s=0.3)
        self.addCleanup(ctx.close)

        async def hold():
            async with ctx._session_lock:
                await asyncio.sleep(10)

        holder = asyncio.run_coroutine_threadsafe(hold(), ctx._loop)
        self.addCleanup(holder.cancel)
        while not ctx._session_lock.locked():
            time.sleep(0.01)
        started = time.monotonic()
        with mock.patch.object(context_module, "_CALL_GRACE_S", 0.2), self.assertRaises(TimeoutError):
            ctx.get_device_context("dev-0001")
        self.assertLess(time.monotonic() - started, 3)

    def test_session_teardown_that_never_finishes_does_not_hang_the_call(self):
        original_aexit = Client.__aexit__

        async def wedged(client, *exc_info):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
            return await original_aexit(client, *exc_info)

        async def failing(client, *args, **kwargs):
            raise RuntimeError("transport down")

        started = time.monotonic()
        with mock.patch.object(context_module, "_DROP_TIMEOUT_S", 0.2), \
                mock.patch.object(Client, "call_tool", failing), mock.patch.object(Client, "__aexit__", wedged):
            with self.assertLogs(context_module.log, "WARNING") as logs, \
                    self.assertRaisesRegex(RuntimeError, "transport down"):
                self.ctx.get_device_context("dev-0001")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("did not close", logs.output[0])

    def test_timeout_while_opening_leaves_no_session_and_the_next_call_succeeds(self):
        original_aenter = Client.__aenter__

        async def slow_open(client):
            await asyncio.sleep(0.3)
            return await original_aenter(client)

        quick = SyncContext(build_mcp(context_table(devices=DEVICES), wait_timeout_ms=50), timeout_s=0.1)
        self.addCleanup(quick.close)
        with self.assertNoLogs(context_module.log), mock.patch.object(Client, "__aenter__", slow_open):
            with self.assertRaises(TimeoutError):
                quick.get_device_context("dev-0001")
            time.sleep(0.5)
        quick._timeout_s = 5.0
        self.assertEqual(quick.get_device_context("dev-0001")["status"], "OK")

    def test_calls_racing_close_all_return(self):
        ctx = SyncContext(build_mcp(context_table(devices=DEVICES), wait_timeout_ms=50), timeout_s=2.0)
        self.addCleanup(ctx.close)
        outcomes = []

        def worker():
            for _ in range(200):
                try:
                    ctx.get_device_context("dev-0001")
                    outcomes.append("ok")
                except RuntimeError:
                    outcomes.append("closed")
                    return
                except TimeoutError:
                    outcomes.append("timeout")
                    return

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        ctx.close()
        for t in threads:
            t.join(10)
        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertNotIn("timeout", outcomes)

    def test_call_on_a_stopped_loop_fails_instead_of_hanging(self):
        ctx = SyncContext(build_mcp(context_table(devices=DEVICES), wait_timeout_ms=50), timeout_s=0.3)
        loop = ctx._loop
        with mock.patch.object(loop, "close"):
            loop.call_soon_threadsafe(loop.stop)
            ctx._thread.join(5)
        self.assertFalse(ctx._thread.is_alive())
        started = time.monotonic()
        with mock.patch.object(context_module, "_CALL_GRACE_S", 0.2), self.assertRaises(TimeoutError):
            ctx.get_device_context("dev-0001")
        self.assertLess(time.monotonic() - started, 3)
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()
        ctx._closed = True

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
