import asyncio
import json
import math
import threading
import unittest

import httpx

from dtm.ui.hub import Hub
from dtm.ui.model import Board
from dtm.ui.sources import ContextPoller


def refuse_constant(name):
    raise AssertionError(f"non-finite JSON constant {name}")


def strict_loads(text):
    return json.loads(text, parse_constant=refuse_constant)


class HubTest(unittest.IsolatedAsyncioTestCase):
    def make_hub(self, **kwargs) -> Hub:
        hub = Hub(Board(), **kwargs)
        hub.bind(asyncio.get_running_loop())
        return hub

    async def test_first_message_is_snapshot(self):
        hub = self.make_hub()
        sub = hub.subscribe()
        try:
            first = await asyncio.wait_for(anext(sub), 2)
        finally:
            await sub.aclose()
        self.assertEqual(first["event"], "snapshot")
        self.assertEqual(json.loads(first["data"]), Board().snapshot())

    async def test_deltas_batched_within_batch_ms(self):
        hub = self.make_hub(batch_ms=50)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            hub.publish_threadsafe([("a", {"n": 1}), ("b", {"n": 2})])
            hub.publish_threadsafe([("c", {"n": 3})])
            batch = await asyncio.wait_for(anext(sub), 2)
        finally:
            await sub.aclose()
        self.assertEqual(batch["event"], "batch")
        self.assertEqual(json.loads(batch["data"]),
                         [{"type": "a", "data": {"n": 1}}, {"type": "b", "data": {"n": 2}},
                          {"type": "c", "data": {"n": 3}}])

    async def test_empty_publish_sends_nothing(self):
        hub = self.make_hub(batch_ms=10)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            hub.publish_threadsafe([])
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(sub), 0.2)
        finally:
            await sub.aclose()

    async def test_overflow_resets_to_snapshot(self):
        hub = self.make_hub(batch_ms=10, queue_limit=2)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            hub.publish_threadsafe([("t", {"n": i}) for i in range(5)])
            reset = await asyncio.wait_for(anext(sub), 2)
            self.assertEqual(reset["event"], "snapshot")
            hub.publish_threadsafe([("t", {"n": 9})])
            after = await asyncio.wait_for(anext(sub), 2)
        finally:
            await sub.aclose()
        self.assertEqual(after["event"], "batch")
        self.assertEqual(json.loads(after["data"]), [{"type": "t", "data": {"n": 9}}])

    async def test_disconnect_removes_subscriber(self):
        hub = self.make_hub()
        sub = hub.subscribe()
        await asyncio.wait_for(anext(sub), 2)
        self.assertEqual(hub.subscriber_count(), 1)
        await sub.aclose()
        self.assertEqual(hub.subscriber_count(), 0)

    async def test_cancelled_consumer_removes_subscriber(self):
        hub = self.make_hub()

        async def consume():
            async for _ in hub.subscribe():
                pass

        task = asyncio.create_task(consume())
        for _ in range(100):
            if hub.subscriber_count():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(hub.subscriber_count(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(hub.subscriber_count(), 0)

    async def test_publish_threadsafe_from_thread(self):
        hub = self.make_hub(batch_ms=10)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            worker = threading.Thread(target=hub.publish_threadsafe, args=([("x", {"k": 1})],))
            worker.start()
            batch = await asyncio.wait_for(anext(sub), 2)
            worker.join()
        finally:
            await sub.aclose()
        self.assertEqual(json.loads(batch["data"]), [{"type": "x", "data": {"k": 1}}])

    async def test_publish_with_no_subscribers_is_harmless(self):
        hub = self.make_hub()
        hub.publish_threadsafe([("x", {})])
        await asyncio.sleep(0.01)
        self.assertEqual(hub.subscriber_count(), 0)


    async def test_lag_over_the_top_bucket_stays_strict_json(self):
        scrapes = iter(['dtm_materialization_lag_ms_bucket{le="50.0",topic="t"} 0\n'
                        'dtm_materialization_lag_ms_bucket{le="+Inf",topic="t"} 0\n',
                        'dtm_materialization_lag_ms_bucket{le="50.0",topic="t"} 0\n'
                        'dtm_materialization_lag_ms_bucket{le="+Inf",topic="t"} 7\n'])

        def handler(request):
            if request.url.path.endswith("/metrics"):
                return httpx.Response(200, text=next(scrapes))
            return httpx.Response(200, json=[])
        hub = self.make_hub(batch_ms=10)
        poller = ContextPoller(hub._board, httpx.Client(transport=httpx.MockTransport(handler)),
                               hub.publish_threadsafe, "http://context.test", metrics_every=1,
                               clock=lambda: 1000.0)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            poller.poll_once()
            poller.poll_once()
            batch = await asyncio.wait_for(anext(sub), 2)
        finally:
            await sub.aclose()
        summaries = [d["data"] for d in strict_loads(batch["data"]) if d["type"] == "summary"]
        self.assertTrue(summaries)
        self.assertEqual(summaries[-1]["context_lag_p95_ms"], 50.0)
        self.assertTrue(summaries[-1]["context_lag_p95_over"])
        late = hub.subscribe()
        try:
            snapshot = await asyncio.wait_for(anext(late), 2)
        finally:
            await late.aclose()
        summary = strict_loads(snapshot["data"])["summary"]
        self.assertEqual((summary["context_lag_p95_ms"], summary["context_lag_p95_over"]), (50.0, True))

    async def test_non_finite_value_is_rejected_not_written(self):
        hub = self.make_hub(batch_ms=10)
        sub = hub.subscribe()
        try:
            await asyncio.wait_for(anext(sub), 2)
            hub.publish_threadsafe([("t", {"n": math.inf})])
            with self.assertRaises(ValueError):
                await asyncio.wait_for(anext(sub), 2)
        finally:
            await sub.aclose()


class UnboundHubTest(unittest.TestCase):
    def test_publish_before_bind_is_ignored(self):
        Hub(Board()).publish_threadsafe([("x", {})])


if __name__ == "__main__":
    unittest.main()
