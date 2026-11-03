import unittest
from html.parser import HTMLParser
from importlib import resources

from starlette.testclient import TestClient

from dtm.ui.app import build_app
from dtm.ui.hub import Hub
from dtm.ui.model import Board

DOM_IDS = ("pane-devices", "pane-traffic", "pane-pairs", "summary", "devices-table", "traffic-graph",
           "feed", "pairs-table")
STATIC = ("index.html", "app.js", "style.css")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.maximize_buttons = 0
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "button" and "maximize" in (attrs.get("class") or "").split():
            self.maximize_buttons += 1
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs["href"])


def _static_text(name: str) -> str:
    return (resources.files("dtm.ui") / "static" / name).read_text(encoding="utf-8")


class StaticFilesTest(unittest.TestCase):
    def test_static_files_packaged(self):
        static = resources.files("dtm.ui") / "static"
        for name in STATIC:
            with self.subTest(name=name):
                self.assertTrue((static / name).is_file())

    def test_index_has_panes_and_controls(self):
        response = TestClient(build_app(Hub(Board()), lambda: {})).get("/")
        self.assertEqual(response.status_code, 200)
        page = _Collector()
        page.feed(response.text)
        for dom_id in DOM_IDS:
            with self.subTest(id=dom_id):
                self.assertEqual(page.ids.count(dom_id), 1)
        self.assertEqual(page.maximize_buttons, 3)
        self.assertIn("/static/app.js", page.scripts)
        self.assertIn("/static/style.css", page.stylesheets)

    def test_page_loads_nothing_from_other_hosts(self):
        page = _Collector()
        page.feed(_static_text("index.html"))
        for url in page.scripts + page.stylesheets:
            with self.subTest(url=url):
                self.assertTrue(url.startswith("/static/"))
        self.assertNotIn("@import", _static_text("style.css"))

    def test_app_js_uses_eventsource_and_handles_both_events(self):
        app_js = _static_text("app.js")
        self.assertIn('EventSource("/events")', app_js)
        self.assertIn('"snapshot"', app_js)
        self.assertIn('"batch"', app_js)

    def test_summary_names_context_materialization(self):
        self.assertIn('"context materialization p50 / p95"', _static_text("app.js"))

    def test_materialization_kpi_is_approximate_and_explained(self):
        app_js = _static_text("app.js")
        self.assertIn("Kafka record timestamp → applied in context-mcp", app_js)
        self.assertIn("`~${", app_js)

    def test_static_assets_served_with_types(self):
        client = TestClient(build_app(Hub(Board()), lambda: {}))
        for name, media in (("app.js", "javascript"), ("style.css", "text/css")):
            with self.subTest(name=name):
                response = client.get(f"/static/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(media, response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
