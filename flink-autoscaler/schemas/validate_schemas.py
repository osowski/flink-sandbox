#!/usr/bin/env python3
"""Validate the canonical Avro schemas: valid JSON, expected record name,
and expected field names/types. Run after any edit to the .avsc files."""
import json
import os
import sys

SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))

EXPECTED = {
    "sensor-event-input-value.avsc": {
        "record_name": "SensorEvent",
        "fields": {
            "timestamp": "string",
            "type": "string",
            "location": "string",
            "value": "double",
            "status": "string",
            "id": "string",
        },
    },
    "sensor-event-output-value.avsc": {
        "record_name": "ProcessedSensorEvent",
        "fields": {
            "timestamp": "string",
            "type": "string",
            "location": "string",
            "value": "double",
            "status": "string",
            "id": "string",
            "encoded": ["null", "string"],
            "error": ["null", "string"],
            "original": ["null", "string"],
        },
    },
}


def check(filename, expected):
    path = os.path.join(SCHEMA_DIR, filename)
    with open(path) as f:
        schema = json.load(f)

    assert schema["name"] == expected["record_name"], (
        f"{filename}: expected record name {expected['record_name']!r}, got {schema['name']!r}"
    )

    actual_fields = {field["name"]: field["type"] for field in schema["fields"]}
    assert actual_fields == expected["fields"], (
        f"{filename}: field mismatch.\nExpected: {expected['fields']}\nActual:   {actual_fields}"
    )
    print(f"OK: {filename} ({schema['name']}, {len(actual_fields)} fields)")


def main():
    for filename, expected in EXPECTED.items():
        check(filename, expected)
    print("All schemas valid.")


if __name__ == "__main__":
    sys.exit(main())
