"""In-memory context table: the latest device and customer context plus recent risk signals.

One condition guards every structure, so a lookup waiting for a context sequence to be
materialized wakes as soon as ``apply`` lands it and never holds the lock while it waits.
"""
import logging
import threading
import time
from bisect import bisect_right
from typing import Callable

from dtm.common.envelope import to_epoch_ms
from dtm.common.metrics import DEVICE_CAP_EXCEEDED, TRACKED_DEVICES

SIGNALS_PER_DEVICE = 50

DEVICE_CONTEXT = "device.context"
CUSTOMER_CONTEXT = "customer.context"
RISK_SIGNALS = "device.risk-signals"

log = logging.getLogger(__name__)

_DEVICE_FIELDS = ("customer_id", "context_seq", "trust_status", "platform", "os_version", "rooted",
                  "last_country", "last_lat", "last_lon", "last_event_type")
_CUSTOMER_FIELDS = ("device_count", "trusted_device_count", "known_countries",
                    "recent_anomaly_count", "account_age_days", "last_country")


class ContextTable:
    def __init__(self, device_cap: int, clock: Callable[[], float] = time.time):
        self._device_cap = device_cap
        self._clock = clock
        self._cond = threading.Condition()
        self._devices: dict[str, dict] = {}
        self._customers: dict[str, dict] = {}
        self._devices_by_customer: dict[str, set[str]] = {}
        self._signals: dict[str, list[dict]] = {}
        self._signal_ids: dict[str, set[str]] = {}
        self._seen_device_ids: set[str] = set()
        self._offsets: dict[tuple[str, int], int] = {}
        self._materialized_at = 0
        self._cap_logged = False
        TRACKED_DEVICES.set(0)
        DEVICE_CAP_EXCEEDED.set(0)

    def apply(self, topic: str, partition: int, offset: int, key: dict | None,
              value: dict | None, timestamp_ms: int) -> None:
        if topic not in (DEVICE_CONTEXT, CUSTOMER_CONTEXT, RISK_SIGNALS):
            raise ValueError(f"unexpected topic {topic!r}")
        with self._cond:
            if topic == DEVICE_CONTEXT:
                self._apply_device(key["device_id"], value)
            elif topic == CUSTOMER_CONTEXT:
                self._apply_customer(key["customer_id"], value)
            else:
                self._apply_signal(value)
            self._offsets[(topic, partition)] = offset
            self._materialized_at = round(self._clock() * 1000)
            self._cond.notify_all()

    def get_device(self, device_id: str, min_seq: int | None = None,
                   wait_timeout_ms: int = 2000) -> dict:
        with self._cond:
            if min_seq is not None and wait_timeout_ms > 0:
                self._cond.wait_for(lambda: self._satisfies(device_id, min_seq),
                                    timeout=wait_timeout_ms / 1000)
            row = self._devices.get(device_id)
            if row is None:
                status = "NOT_FOUND" if min_seq is None else "STALE"
            elif min_seq is not None and not self._satisfies(device_id, min_seq):
                status = "STALE"
            else:
                status = "OK"
            return {"status": status, "device": dict(row) if row else None,
                    "as_of": self._as_of(device_id)}

    def get_customer(self, customer_id: str) -> dict:
        with self._cond:
            row = self._customers.get(customer_id)
            if row is None:
                return {"status": "NOT_FOUND", "customer": None}
            devices = [{"device_id": d, "trust_status": self._devices[d]["trust_status"]}
                       for d in sorted(self._devices_by_customer.get(customer_id, ()))]
            return {"status": "OK", "customer": {**row, "devices": devices}}

    def list_risk_signals(self, device_id: str, limit: int = 10) -> list[dict]:
        with self._cond:
            newest_first = reversed(self._signals.get(device_id, ()))
            return [dict(s) for _, s in zip(range(max(limit, 0)), newest_first)]

    def list_devices(self, customer_id: str | None = None,
                     trust_status: str | None = None) -> list[dict]:
        with self._cond:
            ids = (sorted(self._devices_by_customer.get(customer_id, ())) if customer_id is not None
                   else sorted(self._devices))
            rows = (self._devices[d] for d in ids)
            return [dict(r) for r in rows if trust_status is None or r["trust_status"] == trust_status]

    def as_of(self, device_id: str | None) -> dict:
        with self._cond:
            return self._as_of(device_id)

    def offsets(self) -> dict[tuple[str, int], int]:
        with self._cond:
            return dict(self._offsets)

    def _satisfies(self, device_id: str, min_seq: int) -> bool:
        row = self._devices.get(device_id)
        return row is not None and row["context_seq"] is not None and row["context_seq"] >= min_seq

    def _as_of(self, device_id: str | None) -> dict:
        row = self._devices.get(device_id) if device_id is not None else None
        return {"contextSeq": row["context_seq"] if row else None,
                "materializedAt": self._materialized_at,
                "sourceOffsets": [{"topic": t, "partition": p, "offset": o}
                                  for (t, p), o in sorted(self._offsets.items())]}

    def _apply_device(self, device_id: str, value: dict | None) -> None:
        row = None
        if value is not None:
            row = {"device_id": device_id, **{f: value[f] for f in _DEVICE_FIELDS},
                   "last_event_time": to_epoch_ms(value["last_event_time"])}
        self._seen_device_ids.add(device_id)
        if len(self._seen_device_ids) > self._device_cap:
            DEVICE_CAP_EXCEEDED.set(1)
            if not self._cap_logged:
                self._cap_logged = True
                log.error("more than %d distinct devices seen on %s", self._device_cap,
                          DEVICE_CONTEXT)
        old = self._devices.pop(device_id, None)
        if old is not None and old["customer_id"] is not None:
            members = self._devices_by_customer.get(old["customer_id"])
            if members is not None:
                members.discard(device_id)
                if not members:
                    del self._devices_by_customer[old["customer_id"]]
        if row is not None:
            self._devices[device_id] = row
            if row["customer_id"] is not None:
                self._devices_by_customer.setdefault(row["customer_id"], set()).add(device_id)
        TRACKED_DEVICES.set(len(self._devices))

    def _apply_customer(self, customer_id: str, value: dict | None) -> None:
        if value is None:
            self._customers.pop(customer_id, None)
            return
        self._customers[customer_id] = {
            "customer_id": customer_id,
            **{f: value[f] for f in _CUSTOMER_FIELDS},
            "last_event_time": to_epoch_ms(value["last_event_time"])}

    def _apply_signal(self, value: dict) -> None:
        device_id = value["envelope"]["device_id"]
        signal = {"signal_id": value["signal_id"], "signal_type": value["signal_type"],
                  "severity": value["severity"], "detail": value["detail"],
                  "source_event_type": value["source_event_type"],
                  "event_time": to_epoch_ms(value["envelope"]["event_time"])}
        seen = self._signal_ids.setdefault(device_id, set())
        if signal["signal_id"] in seen:
            return
        seen.add(signal["signal_id"])
        window = self._signals.setdefault(device_id, [])
        position = bisect_right(window, signal["event_time"], key=lambda s: s["event_time"])
        if len(window) >= SIGNALS_PER_DEVICE:
            if position == 0:
                return
            del window[0]
            position -= 1
        window.insert(position, signal)
