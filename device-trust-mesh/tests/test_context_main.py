import asyncio
import subprocess
import sys
import unittest
from pathlib import Path

import httpx

from dtm.mesh.context.__main__ import server_config
from dtm.mesh.context.state import ContextState
from dtm.mesh.context.table import ContextTable

PROJECT = Path(__file__).resolve().parents[1]


def empty_state() -> ContextState:
    return ContextState(ContextTable(20), None, 2000)


class ServerConfigTest(unittest.TestCase):
    def test_default_port_and_settings(self):
        config = server_config(empty_state(), {})
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertIs(config.access_log, False)
        self.assertEqual(config.workers, 1)
        self.assertEqual(config.timeout_graceful_shutdown, 10)
        self.assertEqual(config.log_level, "info")

    def test_port_from_env(self):
        self.assertEqual(server_config(empty_state(), {"DTM_CONTEXT_PORT": "9090"}).port, 9090)

    def test_rejects_bad_port(self):
        for value in ("abc", "0", "70000"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "DTM_CONTEXT_PORT"):
                    server_config(empty_state(), {"DTM_CONTEXT_PORT": value})

    def test_app_serves_context_routes(self):
        app = server_config(empty_state(), {}).app

        async def fetch(path):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                return await client.get(path)

        for path in ("/healthz", "/livez", "/metrics", "/v1/devices"):
            with self.subTest(path=path):
                response = asyncio.run(fetch(path))
                self.assertEqual(response.status_code, 200)
        self.assertEqual(asyncio.run(fetch("/v1/devices")).json(), [])


class RaylessImportTest(unittest.TestCase):
    def test_imports_without_ray(self):
        code = (
            "import sys\n"
            "sys.modules['ray'] = None\n"
            "import dtm.mesh.context.__main__ as m\n"
            "from dtm.mesh.context.state import ContextState\n"
            "from dtm.mesh.context.table import ContextTable\n"
            "m.server_config(ContextState(ContextTable(20), None, 2000), {})\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], cwd=PROJECT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
