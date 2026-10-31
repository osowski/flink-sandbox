"""The mesh envelope carried on every record, in its Avro (snake_case) and A2A (camelCase) forms."""
from dataclasses import dataclass, fields
from datetime import datetime

ENVELOPE_URI = "urn:device-trust-mesh:envelope:v1"


def to_epoch_ms(v: int | datetime | None) -> int | None:
    """Return epoch milliseconds for an int or a datetime, passing ``None`` through."""
    if v is None or isinstance(v, int):
        return v
    return round(v.timestamp() * 1000)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.capitalize() for w in rest)


_OPTIONAL_WITHOUT_DEFAULT = ("causation_id", "customer_id", "device_seq", "required_context_seq")


def _with_absent_optionals(values: dict) -> dict:
    return {**dict.fromkeys(_OPTIONAL_WITHOUT_DEFAULT), **values}


@dataclass(frozen=True)
class Envelope:
    message_id: str
    correlation_id: str
    causation_id: str | None
    agent_id: str
    agent_version: str
    config_version: str
    event_time: int
    device_id: str
    customer_id: str | None
    device_seq: int | None
    required_context_seq: int | None
    mode: str = "LIVE"
    replay_run_id: str | None = None

    def to_avro(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def to_a2a(self) -> dict:
        return {_camel(f.name): getattr(self, f.name)
                for f in fields(self) if getattr(self, f.name) is not None}

    @classmethod
    def from_a2a(cls, d: dict) -> "Envelope":
        values = {f.name: d[_camel(f.name)] for f in fields(cls) if _camel(f.name) in d}
        return cls(**_with_absent_optionals(values))

    @classmethod
    def from_avro(cls, d: dict) -> "Envelope":
        values = {f.name: d[f.name] for f in fields(cls) if f.name in d}
        values["event_time"] = to_epoch_ms(values["event_time"])
        return cls(**_with_absent_optionals(values))
