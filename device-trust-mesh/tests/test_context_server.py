import asyncio
import json
import os
import threading
import time
import unittest
import warnings
from unittest import mock
from datetime import datetime, timezone

import httpx
from mcp.client.client import Client

from dtm.common.metrics import CONTEXT_STALE_TOTAL, CONTEXT_TOOL_LATENCY_MS
from dtm.mesh.context.server import THREAD_LIMITER, allowed_hosts, build_api, build_mcp
from dtm.mesh.context.state import ContextState
from dtm.mesh.context.table import ContextTable

EVENT_TIME = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def ctx(device_id, seq, customer="cust-001", status="TRUSTED"):
    return {"envelope": {"device_id": device_id}, "customer_id": customer, "context_seq": seq,
            "trust_status": status, "platform": "ios", "os_version": "18.1", "rooted": False,
            "last_country": "US", "last_lat": 37.77, "last_lon": -122.42,
            "last_event_type": "LOGIN", "last_event_time": EVENT_TIME}


def customer_ctx():
    return {"envelope": {"device_id": "dev-0001"}, "device_count": 2, "trusted_device_count": 2,
            "known_countries": "US", "recent_anomaly_count": 0, "account_age_days": 400,
            "last_country": "US", "last_event_time": EVENT_TIME}


def risk_signal(signal_id, device_id="dev-0001"):
    return {"envelope": {"device_id": device_id, "event_time": EVENT_TIME}, "signal_id": signal_id,
            "signal_type": "IMPOSSIBLE_TRAVEL", "severity": "HIGH", "detail": "d",
            "source_event_type": "LOGIN"}


def seeded_table():
    table = ContextTable(20)
    table.apply("device.context", 0, 0, {"device_id": "dev-0001"}, ctx("dev-0001", 5), 1)
    table.apply("device.context", 0, 1, {"device_id": "dev-0002"},
                ctx("dev-0002", 3, status="SUSPECT"), 1)
    table.apply("customer.context", 0, 0, {"customer_id": "cust-001"}, customer_ctx(), 1)
    table.apply("device.risk-signals", 0, 0, None, risk_signal("sig-1"), 1)
    return table


class ReadyStub:
    def __init__(self, ready: bool):
        self.ready = threading.Event()
        if ready:
            self.ready.set()


def api_for(ready: bool = True, table: ContextTable | None = None):
    return build_api(ContextState(table or seeded_table(), materializer=ReadyStub(ready),
                                  wait_timeout_ms=10))


def tool_payload(result):
    payload = (result.structured_content if result.structured_content is not None
               else json.loads(result.content[0].text))
    return payload["result"] if set(payload) == {"result"} else payload


class ContextServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_listed(self):
        async with Client(build_mcp(seeded_table())) as c:
            self.assertEqual({t.name for t in (await c.list_tools()).tools},
                             {"get_device_context", "get_customer_context", "list_risk_signals",
                              "list_devices"})

    async def test_server_is_named_context_mcp(self):
        self.assertEqual(build_mcp(seeded_table()).name, "context-mcp")

    async def test_tool_results_match_table(self):
        async with Client(build_mcp(seeded_table())) as c:
            device = tool_payload(await c.call_tool("get_device_context", {"device_id": "dev-0001"}))
            self.assertEqual((device["status"], device["device"]["context_seq"]), ("OK", 5))
            customer = tool_payload(await c.call_tool("get_customer_context",
                                                      {"customer_id": "cust-001"}))
            self.assertEqual(customer["status"], "OK")
            signals = tool_payload(await c.call_tool("list_risk_signals", {"device_id": "dev-0001"}))
            self.assertEqual([s["signal_id"] for s in signals], ["sig-1"])
            devices = tool_payload(await c.call_tool("list_devices", {"trust_status": "SUSPECT"}))
            self.assertEqual([d["device_id"] for d in devices], ["dev-0002"])

    async def test_stale_increments_counter_and_observes_latency(self):
        stale_before = CONTEXT_STALE_TOTAL._value.get()
        latency_before = CONTEXT_TOOL_LATENCY_MS.labels("get_device_context")._sum.get()
        count_before = sum(s.value for s in CONTEXT_TOOL_LATENCY_MS.collect()[0].samples
                           if s.name.endswith("_count") and s.labels["tool"] == "get_device_context")
        async with Client(build_mcp(seeded_table(), wait_timeout_ms=10)) as c:
            r = tool_payload(await c.call_tool("get_device_context",
                                               {"device_id": "dev-0001", "min_seq": 99}))
        self.assertEqual(r["status"], "STALE")
        self.assertEqual(CONTEXT_STALE_TOTAL._value.get(), stale_before + 1)
        count_after = sum(s.value for s in CONTEXT_TOOL_LATENCY_MS.collect()[0].samples
                          if s.name.endswith("_count") and s.labels["tool"] == "get_device_context")
        self.assertEqual(count_after, count_before + 1)
        self.assertGreater(CONTEXT_TOOL_LATENCY_MS.labels("get_device_context")._sum.get(),
                           latency_before)

    async def test_waiting_lookup_does_not_block_others(self):
        server = build_mcp(seeded_table(), wait_timeout_ms=1000)
        async with Client(server) as a, Client(server) as b:
            t0 = time.monotonic()
            slow = asyncio.create_task(
                a.call_tool("get_device_context", {"device_id": "dev-0001", "min_seq": 99}))
            await asyncio.sleep(0.05)
            fast = tool_payload(await b.call_tool("get_device_context", {"device_id": "dev-0002"}))
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(fast["status"], "OK")
            self.assertEqual(tool_payload(await slow)["status"], "STALE")

    async def test_rest_mirror_and_health(self):
        app = api_for()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1") as h:
            self.assertEqual((await h.get("/v1/devices/dev-0001")).json()["status"], "OK")
            self.assertEqual((await h.get("/v1/devices/dev-0001?min_seq=99")).json()["status"],
                             "STALE")
            self.assertEqual((await h.get("/v1/devices/none")).json()["status"], "NOT_FOUND")
            self.assertEqual(
                [d["device_id"] for d in (await h.get("/v1/devices?trust_status=SUSPECT")).json()],
                ["dev-0002"])
            self.assertEqual(
                [d["device_id"] for d in (await h.get("/v1/devices?customer_id=cust-001")).json()],
                ["dev-0001", "dev-0002"])
            self.assertEqual((await h.get("/v1/customers/cust-001")).json()["status"], "OK")
            self.assertEqual(
                [s["signal_id"] for s in (await h.get("/v1/devices/dev-0001/signals?limit=5")).json()],
                ["sig-1"])
            health = await h.get("/healthz")
            self.assertEqual((health.status_code, health.json()), (200, {"ready": True}))
            metrics = await h.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertTrue(metrics.headers["content-type"].startswith("text/plain"))
            self.assertIn(b"dtm_tracked_devices", metrics.content)

    async def test_health_503_until_ready(self):
        state = ContextState(seeded_table(), materializer=ReadyStub(False), wait_timeout_ms=10)
        app = build_api(state)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1") as h:
            r = await h.get("/healthz")
            self.assertEqual((r.status_code, r.json()), (503, {"ready": False}))
            state.materializer.ready.set()
            self.assertEqual((await h.get("/healthz")).status_code, 200)

    async def test_mcp_mounted_in_api(self):
        app = api_for()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://127.0.0.1") as h:
                r = await h.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"accept": "application/json, text/event-stream"})
        self.assertEqual(r.status_code, 200)
        names = {t["name"] for t in r.json()["result"]["tools"]}
        self.assertIn("get_device_context", names)

    async def test_mcp_host_allow_list(self):
        app = api_for()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"accept": "application/json, text/event-stream"}

        async def status(host):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url=f"http://{host}") as h:
                return (await h.post("/mcp/", json=body, headers=headers)).status_code

        async with app.router.lifespan_context(app):
            self.assertEqual(await status("device-trust-mesh-serve-svc:8000"), 200)
            self.assertEqual(await status("device-trust-mesh-serve-svc.device-trust-mesh.svc:8000"), 200)
            with self.assertLogs("mcp.server.transport_security", "WARNING"):
                self.assertEqual(await status("evil.example.com"), 421)

    async def test_mcp_endpoint_without_trailing_slash_does_not_redirect(self):
        app = api_for()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"accept": "application/json, text/event-stream"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://127.0.0.1") as h:
                for path in ("/mcp", "/mcp/"):
                    with self.subTest(path=path):
                        r = await h.post(path, json=body, headers=headers)
                        self.assertEqual(r.status_code, 200)
                        self.assertIn("get_device_context",
                                      {t["name"] for t in r.json()["result"]["tools"]})

    def test_thread_limiter_matches_max_ongoing_requests(self):
        self.assertEqual(THREAD_LIMITER.total_tokens, 32)

    def test_extra_allowed_hosts_from_env(self):
        with mock.patch.dict(os.environ, {"DTM_CONTEXT_ALLOWED_HOSTS": "mcp.example.test, other"}):
            hosts = allowed_hosts()
        self.assertIn("mcp.example.test:*", hosts)
        self.assertIn("mcp.example.test", hosts)
        self.assertIn("other:*", hosts)
        self.assertIn("127.0.0.1:*", hosts)

    def test_state_ready_reflects_materializer(self):
        self.assertTrue(ContextState(seeded_table(), ReadyStub(True), 10).ready())
        self.assertFalse(ContextState(seeded_table(), ReadyStub(False), 10).ready())
        self.assertTrue(ContextState(seeded_table(), None, 10).ready())


class StateEnvTest(unittest.TestCase):
    def test_missing_device_cap_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEVICE_CAP"):
                ContextState.from_env()

    def test_non_integer_device_cap_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"DEVICE_CAP": "lots"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEVICE_CAP.*'lots'"):
                ContextState.from_env()

    def test_check_alive_raises_once_the_materializer_thread_has_died(self):
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        state = ContextState(seeded_table(), ReadyStub(True), 10, materializer_thread=dead)
        with self.assertRaisesRegex(RuntimeError, "materializer"):
            state.check_alive()

    def test_check_alive_passes_for_a_live_or_absent_thread(self):
        stop = threading.Event()
        live = threading.Thread(target=stop.wait)
        live.start()
        try:
            ContextState(seeded_table(), ReadyStub(True), 10, materializer_thread=live).check_alive()
            ContextState(seeded_table(), ReadyStub(True), 10).check_alive()
        finally:
            stop.set()
            live.join()


def serve_module():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ray.serve import emits its own pydantic deprecations
        import dtm.mesh.context_mcp as m
    return m


class ServeAppTest(unittest.TestCase):
    def test_deployment_allows_enough_ongoing_requests_for_waiting_lookups(self):
        self.assertEqual(serve_module().ContextMcp.max_ongoing_requests, 32)

    def test_check_health_raises_when_the_materializer_thread_died(self):
        m = serve_module()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        check_health = m.ContextMcp.func_or_class.check_health
        state = ContextState(seeded_table(), ReadyStub(True), 10, materializer_thread=dead)
        with mock.patch.object(m, "get_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "materializer"):
                check_health(None)
        with mock.patch.object(m, "get_state",
                               return_value=ContextState(seeded_table(), ReadyStub(True), 10)):
            check_health(None)

    def test_ready_wait_gives_up_early_when_the_materializer_thread_dies(self):
        m = serve_module()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        state = ContextState(seeded_table(), ReadyStub(False), 10, materializer_thread=dead)
        t0 = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "materializer"):
            m.wait_until_ready(state, timeout_s=30)
        self.assertLess(time.monotonic() - t0, 2)

    def test_ready_wait_times_out(self):
        m = serve_module()
        with self.assertRaisesRegex(RuntimeError, "not caught up"):
            m.wait_until_ready(ContextState(seeded_table(), ReadyStub(False), 10), timeout_s=0.1)

    def test_ready_wait_returns_once_ready(self):
        serve_module().wait_until_ready(ContextState(seeded_table(), ReadyStub(True), 10),
                                        timeout_s=0.1)

    def test_serve_app_import_path(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # ray.serve import emits its own pydantic deprecations
            from ray import serve

            import dtm.mesh.context_mcp as m
        self.assertIsInstance(m.app, serve.Application)


if __name__ == "__main__":
    unittest.main()
