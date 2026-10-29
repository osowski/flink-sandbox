import importlib
import unittest


class PackageLayoutTest(unittest.TestCase):
    def test_package_imports(self):
        for mod in ("dtm", "dtm.common", "dtm.simulator", "dtm.mesh"):
            with self.subTest(mod=mod):
                self.assertIsNotNone(importlib.import_module(mod))


if __name__ == "__main__":
    unittest.main()
