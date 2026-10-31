"""Builders for the Avro ``DeviceEvent`` records the simulator produces to ``device.events``."""
import uuid

from dtm.common.envelope import Envelope
from dtm.simulator.fingerprints import GEO
from dtm.simulator.pool import Device

AGENT_ID = "simulator:device-simulator"


def device_event(device: Device, event_type: str, *, seq: int, now_ms: int, correlation_id: str,
                 fp: dict, country: str, causation_id: str | None = None,
                 enrolment_task_id: str | None = None, version: str = "dev") -> dict:
    if country not in GEO:
        raise ValueError(f"unknown country {country!r}")
    lat, lon = GEO[country]
    envelope = Envelope(
        message_id=str(uuid.uuid4()),
        correlation_id=correlation_id,
        causation_id=causation_id,
        agent_id=AGENT_ID,
        agent_version=version,
        config_version="n/a",
        event_time=now_ms,
        device_id=device.device_id,
        customer_id=device.customer_id,
        device_seq=seq,
        required_context_seq=None,
    )
    return {
        "envelope": envelope.to_avro(),
        "event_type": event_type,
        "fingerprint": fp,
        "geo": {"country": country, "lat": lat, "lon": lon},
        "account_age_days": device.account_age_days,
        "enrolment_task_id": enrolment_task_id,
    }
