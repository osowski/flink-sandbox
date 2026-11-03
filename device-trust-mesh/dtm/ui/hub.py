"""Fans board deltas out to every connected browser as server-sent events.

Source threads call ``publish_threadsafe``; everything else runs on the event loop. Each subscriber gets
a snapshot first, then the deltas that arrive afterwards in batches of at most one per ``batch_ms``. A
subscriber that falls more than ``queue_limit`` deltas behind drops its backlog and gets a fresh snapshot.
"""
import asyncio
import json
import logging
from typing import AsyncIterator

from dtm.ui.model import Board, Delta

log = logging.getLogger(__name__)


class _Subscriber:
    def __init__(self):
        self.pending: list[Delta] = []
        self.overflowed = False
        self.wake = asyncio.Event()


class Hub:
    def __init__(self, board: Board, batch_ms: int = 200, queue_limit: int = 1000):
        self._board = board
        self._batch_s = batch_ms / 1000
        self._queue_limit = queue_limit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[_Subscriber] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish_threadsafe(self, deltas: list[Delta]) -> None:
        """Queue ``deltas`` for every subscriber from any thread; empty lists and an unbound hub are ignored."""
        loop = self._loop
        if not deltas or loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, list(deltas))
        except RuntimeError:
            log.debug("event loop is closed, dropping %d deltas", len(deltas))

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self) -> AsyncIterator[dict]:
        """Yield a snapshot, then a batch whenever deltas are pending, or a new snapshot after an overflow."""
        sub = _Subscriber()
        # Registered before the snapshot is taken, so no delta falls between the two.
        self._subscribers.add(sub)
        try:
            yield self._snapshot_event()
            while True:
                await sub.wake.wait()
                await asyncio.sleep(self._batch_s)
                sub.wake.clear()
                if sub.overflowed:
                    sub.overflowed = False
                    sub.pending.clear()
                    yield self._snapshot_event()
                elif sub.pending:
                    pending, sub.pending = sub.pending, []
                    yield {"event": "batch",
                           "data": json.dumps([{"type": t, "data": d} for t, d in pending], allow_nan=False)}
        finally:
            self._subscribers.discard(sub)

    def _snapshot_event(self) -> dict:
        return {"event": "snapshot", "data": json.dumps(self._board.snapshot(), allow_nan=False)}

    def _dispatch(self, deltas: list[Delta]) -> None:
        for sub in self._subscribers:
            if sub.overflowed:
                continue
            if len(sub.pending) + len(deltas) > self._queue_limit:
                sub.pending.clear()
                sub.overflowed = True
            else:
                sub.pending.extend(deltas)
            sub.wake.set()
