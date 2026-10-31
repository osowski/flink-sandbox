import unittest

from dtm.simulator.engine import Simulator
from dtm.simulator.runner import simulator_from_env


class SimulatorFromEnvTest(unittest.TestCase):
    def test_device_cap_is_required(self):
        with self.assertRaisesRegex(ValueError, "DEVICE_CAP"):
            simulator_from_env({})

    def test_rate_must_be_finite_and_at_most_100_per_second(self):
        for bad in ("nan", "inf", "-inf", "-1", "100.5", "fast"):
            with self.subTest(rate=bad), self.assertRaisesRegex(ValueError, "SIM_RATE_PER_SEC"):
                simulator_from_env({"DEVICE_CAP": "3", "SIM_RATE_PER_SEC": bad})

    def test_valid_settings_build_a_simulator(self):
        for env in ({"DEVICE_CAP": "3"}, {"DEVICE_CAP": "3", "SIM_RATE_PER_SEC": "100", "SIM_SEED": "7"},
                    {"DEVICE_CAP": "3", "SIM_RATE_PER_SEC": "0", "DTM_VERSION": "abc123"}):
            with self.subTest(env=env):
                self.assertIsInstance(simulator_from_env(env), Simulator)


if __name__ == "__main__":
    unittest.main()
