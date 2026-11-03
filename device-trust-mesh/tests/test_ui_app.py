import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from starlette.testclient import TestClient

from dtm.ui import __main__ as ui_main
from dtm.ui.app import build_app
from dtm.ui.hub import Hub
from dtm.ui.model import Board, Coord

HEALTH = {"kafka_stale_since_ms": None, "context_stale_since_ms": 1234}


class OneEventHub(Hub):
    """A hub whose stream ends after the snapshot, because TestClient waits for the app to finish."""

    async def subscribe(self):
        stream = super().subscribe()
        try:
            yield await anext(stream)
        finally:
            await stream.aclose()


class AppTest(unittest.TestCase):
    def setUp(self):
        self.board = Board()
        self.hub = OneEventHub(self.board)
        self.client = TestClient(build_app(self.hub, lambda: HEALTH))

    def test_index_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("Device Trust Mesh", response.text)

    def test_static_serves_known_file(self):
        response = self.client.get("/static/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))

    def test_static_unknown_and_traversal_404(self):
        for path in ("/static/missing.js", "/static/..%2F__init__.py", "/static/../__init__.py",
                     "/static/%2e%2e/model.py", "/static/..%5Cmodel.py", "/static/.", "/static/sub/x.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_healthz_reports_staleness_200(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), HEALTH)

    def test_events_first_message_is_snapshot_when_empty(self):
        response = self.client.get("/events")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        fields = dict(line.split(": ", 1) for line in response.text.splitlines() if ": " in line)
        self.assertEqual(fields["event"], "snapshot")
        data = json.loads(fields["data"])
        self.assertEqual(data, Board().snapshot())
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["feed"], [])

    def test_messages_route_returns_held_row(self):
        held = {"message": {"message_id": "m1"}, "row": {"summary": "s", "n": None}}
        client = TestClient(build_app(self.hub, lambda: HEALTH, message=lambda i: held if i == "m1" else None,
                                      message_limit=7))
        response = client.get("/messages/m1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), held)
        missing = client.get("/messages/zzz")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"error": "not held", "limit": 7})

    def test_messages_route_defaults_to_not_held(self):
        response = self.client.get("/messages/m1")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "not held", "limit": 500})

    def test_messages_route_odd_ids_are_404_not_500(self):
        for path in ("/messages/%2e%2e", "/messages/a%20b", "/messages/" + "x" * 300):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_messages_route_serves_a_board_row_as_strict_json(self):
        board = Board()
        event_time = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        board.apply_task({"envelope": {"message_id": "m1", "event_time": event_time, "device_id": "dev-1"},
                          "from_agent": "a", "to_agent": "b", "task_id": None, "summary": "s"},
                         Coord("agent.tasks_human_readable", 0, 0, 0))
        client = TestClient(build_app(self.hub, lambda: HEALTH, message=board.message,
                                      message_limit=board.feed_limit))
        response = client.get("/messages/m1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["row"]["envelope"]["event_time"], round(event_time.timestamp() * 1000))

    def test_startup_binds_hub_to_running_loop(self):
        hub = mock.Mock()
        with TestClient(build_app(hub, lambda: HEALTH)):
            hub.bind.assert_called_once()


class MainTest(unittest.TestCase):
    def test_fake_runs_the_fake_feed_on_the_configured_port(self):
        with mock.patch.object(ui_main, "FakeFeed") as fake_feed, \
                mock.patch.object(ui_main.uvicorn, "run") as run, \
                mock.patch.object(ui_main.logging, "basicConfig"), \
                mock.patch.dict("os.environ", {"DTM_UI_PORT": "18099"}):
            ui_main.main(["--fake"])
        board, publish = fake_feed.call_args.args
        self.assertIsInstance(board, Board)
        self.assertEqual(publish.__name__, "publish_threadsafe")
        fake_feed.return_value.start.assert_called_once()
        fake_feed.return_value.stop.assert_called_once()
        self.assertEqual(run.call_args.kwargs["port"], 18099)

    def test_main_serves_board_messages_with_its_limit(self):
        with mock.patch.object(ui_main, "FakeFeed") as fake_feed, \
                mock.patch.object(ui_main.uvicorn, "run"), \
                mock.patch.object(ui_main.logging, "basicConfig"), \
                mock.patch.object(ui_main, "build_app") as build:
            ui_main.main(["--fake"])
        board = fake_feed.call_args.args[0]
        self.assertEqual(build.call_args.kwargs["message_limit"], board.feed_limit)
        self.assertEqual(build.call_args.kwargs["message"], board.message)

    def test_health_reports_board_stale_flags(self):
        board = Board()
        board.mark_stale("kafka", 99)
        self.assertEqual(ui_main.board_health(board),
                         {"kafka_stale_since_ms": 99, "context_stale_since_ms": None})


if __name__ == "__main__":
    unittest.main()
