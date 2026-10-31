"""Client for context-mcp: the agents' only path to business data."""
import json

from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer


class ContextClient:
    """Async context manager over an MCP client; ``target`` is an endpoint URL or an in-process server."""

    def __init__(self, target: str | MCPServer, *, stale_retries: int = 3):
        self._target = target
        self._stale_retries = stale_retries
        self._client: Client | None = None

    async def __aenter__(self) -> "ContextClient":
        client = Client(self._target)
        await client.__aenter__()
        self._client = client
        return self

    async def __aexit__(self, *exc_info) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.__aexit__(*exc_info)

    async def get_device_context(self, device_id: str, min_seq: int | None = None) -> dict:
        """Return the device context, retrying a STALE result up to ``stale_retries`` times."""
        args = {"device_id": device_id}
        if min_seq is not None:
            args["min_seq"] = min_seq
        result = await self._call("get_device_context", args)
        for _ in range(self._stale_retries):
            if result["status"] != "STALE":
                break
            result = await self._call("get_device_context", args)
        return result

    async def get_customer_context(self, customer_id: str) -> dict:
        return await self._call("get_customer_context", {"customer_id": customer_id})

    async def list_risk_signals(self, device_id: str, limit: int = 10) -> list[dict]:
        return await self._call("list_risk_signals", {"device_id": device_id, "limit": limit})

    async def list_devices(self, customer_id: str | None = None,
                           trust_status: str | None = None) -> list[dict]:
        args = {k: v for k, v in (("customer_id", customer_id), ("trust_status", trust_status))
                if v is not None}
        return await self._call("list_devices", args)

    async def _call(self, tool: str, args: dict):
        if self._client is None:
            raise RuntimeError("ContextClient must be used as an async context manager")
        result = await self._client.call_tool(tool, args)
        text = result.content[0].text if result.content and hasattr(result.content[0], "text") else ""
        if result.is_error:
            raise RuntimeError(f"context tool {tool} failed: {text}")
        if result.structured_content is not None:
            return _unwrap(result.structured_content)
        if not text:
            raise RuntimeError(f"context tool {tool} returned no content")
        return json.loads(text)


def _unwrap(structured: dict):
    """Return the payload of a tool result; a list result is wrapped under a ``result`` key."""
    if set(structured) == {"result"}:
        return structured["result"]
    return structured
