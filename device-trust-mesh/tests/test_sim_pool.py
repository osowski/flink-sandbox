import unittest

from dtm.simulator.fingerprints import fingerprint
from dtm.simulator.pool import MAX_CAP, DevicePool


class DevicePoolTest(unittest.TestCase):
    def test_ids_are_the_fixed_pool(self):
        self.assertEqual([d.device_id for d in DevicePool(3).devices], ["dev-0001", "dev-0002", "dev-0003"])
        self.assertEqual(len(DevicePool(500).devices), 500)

    def test_cap_bounds(self):
        self.assertEqual(MAX_CAP, 500)
        for bad in (0, 501, -1):
            self.assertRaises(ValueError, DevicePool, bad)

    def test_customers_own_three_devices(self):
        self.assertEqual([d.device_id for d in DevicePool(6).customers()["cust-002"]],
                         ["dev-0004", "dev-0005", "dev-0006"])

    def test_partial_last_customer(self):
        customers = DevicePool(4).customers()
        self.assertEqual(sorted(customers), ["cust-001", "cust-002"])
        self.assertEqual([d.device_id for d in customers["cust-002"]], ["dev-0004"])

    def test_platform_alternates(self):
        self.assertEqual([d.platform for d in DevicePool(4).devices], ["IOS", "ANDROID", "IOS", "ANDROID"])

    def test_home_country_and_account_age_follow_customer_index(self):
        devices = {d.device_id: d for d in DevicePool(15).devices}
        expected = {1: ("US", 400), 2: ("DE", 30), 3: ("FR", 400), 4: ("CA", 30), 5: ("GB", 400)}
        for customer_index, (country, age) in expected.items():
            device = devices[f"dev-{(customer_index - 1) * 3 + 1:04d}"]
            self.assertEqual(device.customer_id, f"cust-{customer_index:03d}")
            self.assertEqual((device.home_country, device.account_age_days), (country, age))

    def test_seq_monotonic(self):
        p = DevicePool(1)
        a = p.next_seq("dev-0001", 1000)
        b = p.next_seq("dev-0001", 1000)
        self.assertGreater(b, a)

    def test_seq_floor_is_microsecond_time(self):
        self.assertEqual(DevicePool(1).next_seq("dev-0001", 1000), 1_000_000)

    def test_seq_is_per_device(self):
        p = DevicePool(2)
        p.next_seq("dev-0001", 1000)
        self.assertEqual(p.next_seq("dev-0002", 1000), 1_000_000)

    def test_seq_survives_restart(self):
        a = DevicePool(1).next_seq("dev-0001", 1_000_000)
        self.assertGreater(DevicePool(1).next_seq("dev-0001", 1_000_001), a)


class FingerprintTest(unittest.TestCase):
    def test_outdated_versions_below_flink_thresholds(self):
        self.assertLess(int(fingerprint("IOS", "OUTDATED")["os_version"].split(".")[0]), 17)
        self.assertLess(int(fingerprint("ANDROID", "OUTDATED")["os_version"].split(".")[0]), 13)
        self.assertGreaterEqual(int(fingerprint("IOS", "CLEAN")["os_version"].split(".")[0]), 17)
        self.assertGreaterEqual(int(fingerprint("ANDROID", "CLEAN")["os_version"].split(".")[0]), 13)

    def test_exact_values(self):
        self.assertEqual(fingerprint("IOS", "CLEAN"), {"platform": "IOS", "os_version": "18.1", "rooted": False,
                                                       "browser_signals": None, "network_type": "WIFI"})
        self.assertEqual(fingerprint("ANDROID", "CLEAN")["os_version"], "15")
        self.assertEqual(fingerprint("IOS", "OUTDATED")["os_version"], "15.7")
        self.assertEqual(fingerprint("ANDROID", "OUTDATED")["os_version"], "11")

    def test_only_rooted_kind_is_rooted(self):
        self.assertTrue(fingerprint("ANDROID", "ROOTED")["rooted"])
        self.assertFalse(fingerprint("ANDROID", "CLEAN")["rooted"])
        self.assertFalse(fingerprint("ANDROID", "OUTDATED")["rooted"])

    def test_rooted_version_is_current(self):
        self.assertEqual(fingerprint("IOS", "ROOTED")["os_version"], "18.1")

    def test_unknown_platform_or_kind_rejected(self):
        self.assertRaises(ValueError, fingerprint, "WINDOWS", "CLEAN")
        self.assertRaises(ValueError, fingerprint, "IOS", "WEIRD")

    def test_returns_independent_dicts(self):
        fingerprint("IOS", "CLEAN")["rooted"] = True
        self.assertFalse(fingerprint("IOS", "CLEAN")["rooted"])


if __name__ == "__main__":
    unittest.main()
