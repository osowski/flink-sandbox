import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PACKAGED = ("dtm/schemas/avro/device.events-value.avsc", "dtm/schemas/a2a/agent.tasks.risk.json",
            "dtm/prompts/risk.v1", "dtm/schemas/SOURCE", "dtm/py.typed")


@unittest.skipIf(shutil.which("uv") is None, "uv is not installed; it is the only wheel builder in the dev env")
class WheelTest(unittest.TestCase):
    def test_wheel_carries_schemas_and_py_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "src"), Path(tmp, "dist")
            src.mkdir()
            shutil.copytree(PROJECT / "dtm", src / "dtm", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(PROJECT / "pyproject.toml", src)
            build = subprocess.run(["uv", "build", "--wheel", "--quiet", "-o", str(out), str(src)],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            wheel, = out.glob("*.whl")
            with zipfile.ZipFile(wheel) as zf:
                names = set(zf.namelist())
        for name in PACKAGED:
            with self.subTest(name=name):
                self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
