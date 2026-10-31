"""The fixed, cap-bounded pool of simulated devices and their per-device event sequence."""
from dataclasses import dataclass

MAX_CAP = 500
DEVICES_PER_CUSTOMER = 3
HOME_COUNTRIES = ("GB", "US", "DE", "FR", "CA")


@dataclass(frozen=True)
class Device:
    device_id: str
    customer_id: str
    platform: str
    home_country: str
    account_age_days: int


def _device(i: int) -> Device:
    customer_index = (i - 1) // DEVICES_PER_CUSTOMER + 1
    return Device(
        device_id=f"dev-{i:04d}",
        customer_id=f"cust-{customer_index:03d}",
        platform="IOS" if i % 2 == 1 else "ANDROID",
        home_country=HOME_COUNTRIES[customer_index % len(HOME_COUNTRIES)],
        account_age_days=400 if customer_index % 2 == 1 else 30,
    )


class DevicePool:
    def __init__(self, cap: int):
        if not 1 <= cap <= MAX_CAP:
            raise ValueError(f"cap must be between 1 and {MAX_CAP}, got {cap}")
        self.devices: tuple[Device, ...] = tuple(_device(i) for i in range(1, cap + 1))
        self._last_seq: dict[str, int] = {}

    def customers(self) -> dict[str, list[Device]]:
        grouped: dict[str, list[Device]] = {}
        for device in self.devices:
            grouped.setdefault(device.customer_id, []).append(device)
        return grouped

    def next_seq(self, device_id: str, now_ms: int) -> int:
        """Return the next ``device_seq``: strictly increasing per device, and floored at the
        microsecond clock so a fresh pool never reissues a value a previous run produced."""
        seq = max(self._last_seq.get(device_id, 0) + 1, now_ms * 1000)
        self._last_seq[device_id] = seq
        return seq
