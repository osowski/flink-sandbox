"""Loaders for the vendored Avro subjects and A2A JSON Schemas."""
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


class UnknownSchema(KeyError):
    """Raised when no vendored schema exists for the requested name."""


def _load(kind: str, suffix: str, name: str) -> dict:
    directory = SCHEMA_DIR / kind
    known = {p.stem for p in directory.glob(f"*{suffix}")}
    if name not in known:
        raise UnknownSchema(f"no {kind} schema named {name!r}")
    return json.loads((directory / f"{name}{suffix}").read_text(encoding="utf-8"))


def load_avro_schema(subject: str) -> dict:
    """Return the Avro schema for a Schema Registry subject, e.g. ``device.events-value``."""
    return _load("avro", ".avsc", subject)


def load_a2a_schema(topic: str) -> dict:
    """Return the JSON Schema for an A2A topic, e.g. ``agent.tasks.risk``."""
    return _load("a2a", ".json", topic)


@lru_cache(maxsize=None)
def a2a_validator(topic: str) -> Draft202012Validator:
    """Return a cached Draft 2020-12 validator for an A2A topic."""
    schema = load_a2a_schema(topic)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)
