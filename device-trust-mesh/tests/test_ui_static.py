import unittest
from html.parser import HTMLParser
from importlib import resources

from starlette.testclient import TestClient

from dtm.ui.app import build_app
from dtm.ui.hub import Hub
from dtm.ui.model import Board

DOM_IDS = ("pane-devices", "pane-traffic", "pane-pairs", "summary", "devices-table", "traffic-graph",
           "feed", "pairs-table", "pane-activity", "activity-cards")
STATIC = ("index.html", "app.js", "style.css")
EXEC_SUMMARY = ("Every customer device that touches a financial account (a phone, a tablet or a browser) is either "
                "trusted or a risk. This use case shows how a bank can manage that trust in real time with a mesh of "
                "cooperating AI agents built on Kafka, Flink and KubeRay. Each agent has one narrow job: enrolment, "
                "risk scoring, step-up checks, revocation or compliance. Together they replace rigid, batch-driven "
                "device policy with continuous, explainable decisions.")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.maximize_buttons = 0
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.details: list[dict] = []
        self._details_stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            entry = {"open": "open" in attrs, "ids": []}
            self.details.append(entry)
            self._details_stack.append(entry)
        if "id" in attrs:
            self.ids.append(attrs["id"])
            for entry in self._details_stack:
                entry["ids"].append(attrs["id"])
        if tag == "button" and "maximize" in (attrs.get("class") or "").split():
            self.maximize_buttons += 1
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "details" and self._details_stack:
            self._details_stack.pop()


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
        self.assertEqual(page.maximize_buttons, 4)
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

    def test_activity_pane_holds_bubbles_and_links_rows(self):
        app_js = _static_text("app.js")
        self.assertIn("BUBBLE_HOLD_MS = 2000", app_js)
        self.assertIn('"activity"', app_js)
        self.assertIn("data-device-id", app_js)
        for name in ("Risk", "Enrolment", "Step-up", "Compliance", "Revocation"):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', app_js)

    def test_pairs_pane_sits_under_devices(self):
        html = _static_text("index.html")
        self.assertLess(html.index('"pane-devices"'), html.index('"pane-pairs"'))
        self.assertLess(html.index('"pane-pairs"'), html.index('"pane-traffic"'))
        self.assertLess(html.index('"pane-traffic"'), html.index('"pane-activity"'))

    def test_message_viewer_dialog_and_fetch(self):
        page = _Collector()
        page.feed(_static_text("index.html"))
        self.assertEqual(page.ids.count("message-viewer"), 1)
        app_js = _static_text("app.js")
        self.assertIn("/messages/${encodeURIComponent(", app_js)
        self.assertIn("no longer held — the UI keeps the most recent", app_js)
        self.assertIn("showModal()", app_js)

    def test_escape_leaves_maximized_pane_while_a_dialog_is_open(self):
        self.assertIn('document.querySelector("dialog[open]")', _static_text("app.js"))

    def test_explainer_has_summary_and_both_diagrams(self):
        html = _static_text("index.html")
        self.assertIn("Agentic Device Trust Mesh: executive summary", html)
        self.assertIn(EXEC_SUMMARY, " ".join(html.split()))
        page = _Collector()
        page.feed(html)
        for dom_id in ("explain-open", "explainer", "diagram-agents", "diagram-architecture"):
            with self.subTest(id=dom_id):
                self.assertEqual(page.ids.count(dom_id), 1)
        self.assertIn("The Complete Architecture", html)
        for topic in ("agent.tasks.risk", "agent.tasks.enrolment", "agent.tasks.step-up", "agent.task-events"):
            with self.subTest(topic=topic):
                self.assertIn(topic, html)

    def test_architecture_section_is_collapsed_by_default(self):
        page = _Collector()
        page.feed(_static_text("index.html"))
        holders = [d for d in page.details if "diagram-architecture" in d["ids"]]
        self.assertEqual(len(holders), 1)
        self.assertFalse(holders[0]["open"])

    def test_explainer_opens_only_from_its_button(self):
        app_js = _static_text("app.js")
        self.assertIn('$("#explain-open").addEventListener("click"', app_js)
        self.assertNotIn("<dialog id=\"explainer\" open", _static_text("index.html"))

    def test_static_assets_served_with_types(self):
        client = TestClient(build_app(Hub(Board()), lambda: {}))
        for name, media in (("app.js", "javascript"), ("style.css", "text/css")):
            with self.subTest(name=name):
                response = client.get(f"/static/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(media, response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
