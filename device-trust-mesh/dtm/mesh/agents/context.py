"""A synchronous face on context-mcp for agent handlers, which run on a plain thread.

One asyncio loop runs on a daemon thread and holds one ``ContextClient`` session open. The MCP client
keeps its transport in task groups that must be entered and exited from one task, so a dedicated task owns
the session from open to close; each call runs as its own task on the loop and uses that session. A call
that raises, including one that times out, closes the session, and the next call opens a fresh one.

Every wait is bounded: a call raises ``TimeoutError`` within ``timeout_s`` of getting the session, within a
grace of that however long it queued behind another call or its session took to close, and a call made
once ``close`` has begun raises ``RuntimeError``.
"""
import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver import MCPServer

from dtm.common.context_client import ContextClient

log = logging.getLogger(__name__)

_THREAD_JOIN_GRACE_S = 5.0
_DROP_TIMEOUT_S = 1.0  # how long a session's owner task may take to close before it is cancelled
_CALL_GRACE_S = 2.0  # how long past ``timeout_s`` a caller waits for its call, which covers closing the session


class _Session:
    """An open ``ContextClient`` and the task that owns it until ``stop`` is set."""

    def __init__(self, client: ContextClient, stop: asyncio.Event, owner: asyncio.Task):
        self.client = client
        self.stop = stop
        self.owner = owner


class SyncContext:
    def __init__(self, target: str | MCPServer, *, timeout_s: float = 10.0):
        self._target = target
        self._timeout_s = timeout_s
        self._loop = asyncio.new_event_loop()
        self._session: _Session | None = None
        self._session_lock = asyncio.Lock()
        self._closed = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="dtm-context-loop", daemon=True)
        self._thread.start()

    def get_device_context(self, device_id: str, min_seq: int | None = None) -> dict:
        return self._call(lambda client: client.get_device_context(device_id, min_seq))

    def get_customer_context(self, customer_id: str) -> dict:
        return self._call(lambda client: client.get_customer_context(customer_id))

    def list_risk_signals(self, device_id: str, limit: int = 10) -> list[dict]:
        return self._call(lambda client: client.list_risk_signals(device_id, limit))

    def close(self) -> None:
        """Close the session on its loop, stop the loop and join its thread; safe to call again."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(
                self._timeout_s + _CALL_GRACE_S)
        except Exception:
            log.exception("context session did not close cleanly")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(self._timeout_s + _THREAD_JOIN_GRACE_S)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _call(self, use: Callable[[ContextClient], Awaitable]):
        # Submitting under the lock that ``close`` takes to set ``_closed`` puts every accepted call on the
        # loop ahead of the shutdown, which then waits for it.
        with self._state_lock:
            if self._closed:
                raise RuntimeError("SyncContext is closed")
            future = asyncio.run_coroutine_threadsafe(self._with_session(use), self._loop)
        try:
            return future.result(self._timeout_s + _CALL_GRACE_S)
        except TimeoutError:
            if future.done():
                raise
            future.cancel()
            raise TimeoutError(f"context call did not finish within {self._timeout_s + _CALL_GRACE_S:g}s") from None

    async def _with_session(self, use: Callable[[ContextClient], Awaitable]):
        """Open the session if needed and run ``use`` on it within the timeout; any failure closes the session."""
        async with self._session_lock:
            try:
                async with asyncio.timeout(self._timeout_s):
                    session = self._session or await self._open()
                    return await use(session.client)
            except BaseException:
                await self._drop()
                raise

    async def _open(self) -> _Session:
        ready: asyncio.Future[ContextClient] = self._loop.create_future()
        stop = asyncio.Event()

        async def own() -> None:
            try:
                async with ContextClient(self._target) as client:
                    # A caller that gave up while the session opened has already set ``stop``.
                    if not ready.done():
                        ready.set_result(client)
                    await stop.wait()
            except Exception as e:
                if not ready.done():
                    ready.set_exception(e)
                else:
                    log.warning("context session ended with an error: %r", e)
            finally:
                if not ready.done():
                    ready.cancel()

        owner = self._loop.create_task(own())
        try:
            client = await ready
        except BaseException:
            stop.set()
            await asyncio.gather(owner, return_exceptions=True)
            raise
        self._session = _Session(client, stop, owner)
        return self._session

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.stop.set()
            await asyncio.wait({session.owner}, timeout=_DROP_TIMEOUT_S)
            if not session.owner.done():
                log.warning("context session did not close within %gs; cancelling it", _DROP_TIMEOUT_S)
                session.owner.cancel()

    async def _shutdown(self) -> None:
        async with self._session_lock:
            await self._drop()
        await self._loop.shutdown_asyncgens()
        await self._loop.shutdown_default_executor()
