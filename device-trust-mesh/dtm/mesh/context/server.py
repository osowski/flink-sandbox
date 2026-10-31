"""The context-mcp surface: four MCP tools and a REST mirror over one context table.

Table calls block (a ``min_seq`` lookup waits for materialization), so each tool runs its call on a
worker thread and a waiting lookup never stalls other calls on the same replica.
"""
import os
import time
from contextlib import asynccontextmanager
from functools import partial

import anyio.to_thread
from fastapi import FastAPI, Response
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from dtm.common.metrics import CONTEXT_STALE_TOTAL, CONTEXT_TOOL_LATENCY_MS
from dtm.mesh.context.state import ContextState
from dtm.mesh.context.table import ContextTable

SERVER_NAME = "context-mcp"

_SERVE_SERVICE = "device-trust-mesh-serve-svc"
_SERVE_NAMESPACE = "device-trust-mesh"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def allowed_hosts() -> list[str]:
    """Return the Host values the MCP endpoint accepts: loopback, the Serve Service names and ``DTM_CONTEXT_ALLOWED_HOSTS``.

    Each name is accepted with any port and without one. The variable is a comma-separated list of extra host names.
    """
    service = (_SERVE_SERVICE, f"{_SERVE_SERVICE}.{_SERVE_NAMESPACE}",
               f"{_SERVE_SERVICE}.{_SERVE_NAMESPACE}.svc",
               f"{_SERVE_SERVICE}.{_SERVE_NAMESPACE}.svc.cluster.local")
    extra = [h.strip() for h in os.environ.get("DTM_CONTEXT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = (*_LOOPBACK_HOSTS, *service, *extra)
    return [*hosts, *(f"{host}:*" for host in hosts)]


async def _timed(tool: str, fn, *args, **kwargs):
    start = time.monotonic()
    try:
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
    finally:
        CONTEXT_TOOL_LATENCY_MS.labels(tool).observe((time.monotonic() - start) * 1000)


def build_mcp(table: ContextTable, wait_timeout_ms: int = 2000) -> MCPServer:
    mcp = MCPServer(SERVER_NAME, log_level="WARNING")

    @mcp.tool()
    async def get_device_context(device_id: str, min_seq: int | None = None) -> dict:
        """Return the latest context for a device.

        With min_seq the call waits briefly for that context sequence to be materialized; status is
        OK, STALE (the sequence has not arrived yet; retry) or NOT_FOUND.
        """
        result = await _timed("get_device_context", table.get_device, device_id, min_seq,
                              wait_timeout_ms)
        if result["status"] == "STALE":
            CONTEXT_STALE_TOTAL.inc()
        return result

    @mcp.tool()
    async def get_customer_context(customer_id: str) -> dict:
        """Return the aggregated context for a customer and the trust status of each of their devices."""
        return await _timed("get_customer_context", table.get_customer, customer_id)

    @mcp.tool()
    async def list_risk_signals(device_id: str, limit: int = 10) -> list[dict]:
        """Return the most recent risk signals for a device, newest first."""
        return await _timed("list_risk_signals", table.list_risk_signals, device_id, limit)

    @mcp.tool()
    async def list_devices(customer_id: str | None = None, trust_status: str | None = None) -> list[dict]:
        """List device contexts, optionally filtered by customer and trust status."""
        return await _timed("list_devices", table.list_devices, customer_id, trust_status)

    return mcp


def build_api(state: ContextState) -> FastAPI:
    mcp = build_mcp(state.table, state.wait_timeout_ms)
    # Host validation stays on; callers reach the endpoint by the Serve Service's cluster DNS name.
    hosts = allowed_hosts()
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[f"http://{host}" for host in hosts]))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    api = FastAPI(lifespan=lifespan)
    api.mount("/mcp", mcp_app)

    @api.get("/v1/devices/{device_id}/signals")
    async def signals(device_id: str, limit: int = 10):
        return await _timed("list_risk_signals", state.table.list_risk_signals, device_id, limit)

    @api.get("/v1/devices/{device_id}")
    async def device(device_id: str, min_seq: int | None = None):
        result = await _timed("get_device_context", state.table.get_device, device_id, min_seq,
                              state.wait_timeout_ms)
        if result["status"] == "STALE":
            CONTEXT_STALE_TOTAL.inc()
        return result

    @api.get("/v1/devices")
    async def devices(customer_id: str | None = None, trust_status: str | None = None):
        return await _timed("list_devices", state.table.list_devices, customer_id, trust_status)

    @api.get("/v1/customers/{customer_id}")
    async def customer(customer_id: str):
        return await _timed("get_customer_context", state.table.get_customer, customer_id)

    @api.get("/healthz")
    async def healthz(response: Response):
        ready = state.ready()
        response.status_code = 200 if ready else 503
        return {"ready": ready}

    @api.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return api
