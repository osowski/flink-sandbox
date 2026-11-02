import json
import unittest
from unittest import mock

from starlette.testclient import TestClient

from dtm.ui import __main__ as ui_main
from dtm.ui.app import build_app
from dtm.ui.hub import Hub
from dtm.ui.model import Board

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

    def test_startup_binds_hub_to_running_loop(self):
        hub = mock.Mock()
        with TestClient(build_app(hub, lambda: HEALTH)):
            hub.bind.assert_called_once()


class MainTest(unittest.TestCase):
    def test_fake_without_feed_exits_with_message(self):
        with mock.patch.dict("sys.modules", {"dtm.ui.fake": None}), \
                self.assertRaises(SystemExit) as raised, \
                mock.patch("sys.stderr") as stderr:
            ui_main.main(["--fake"])
        self.assertNotEqual(raised.exception.code, 0)
        written = "".join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn("fake feed not available", written)

    def test_health_reports_board_stale_flags(self):
        board = Board()
        board.mark_stale("kafka", 99)
        self.assertEqual(ui_main.board_health(board),
                         {"kafka_stale_since_ms": 99, "context_stale_since_ms": None})


if __name__ == "__main__":
    unittest.main()
