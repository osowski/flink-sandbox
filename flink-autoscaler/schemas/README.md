# Avro Schema Definitions

This directory is the single canonical source for the Avro schemas used by
both `flink-java-app` (Maven, via `avro-maven-plugin`) and `python-producer`
(loaded at runtime). Neither subproject keeps its own copy — see each
project's build configuration for how it reads these files directly.

## Schema Files

### sensor-event-input-value.avsc

- **Purpose**: Input schema for sensor events produced by the Python Kafka producer
- **Namespace**: `com.confluent.examples.sensors`
- **Record Name**: `SensorEvent`
- **Used by**: Python producer (serialization), Flink application (deserialization from the input topic)
- **Fields**: `timestamp` (string), `type` (string), `location` (string), `value` (double), `status` (string), `id` (string) — all required

### sensor-event-output-value.avsc

- **Purpose**: Output schema for events processed by the Flink application
- **Namespace**: `com.confluent.examples.sensors`
- **Record Name**: `ProcessedSensorEvent`
- **Used by**: Flink application (serialization to the output topic)
- **Fields**: all `SensorEvent` fields, plus three nullable fields with `default: null`:
  - `encoded` — Base64-encoded transformation result, present on success
  - `error` — error message, present only if processing failed
  - `original` — string form of the input event, present only if processing failed

  The `error`/`original` fields exist so a processing failure can still be
  emitted as a valid `ProcessedSensorEvent` record instead of being dropped
  — mirroring how the project's original JSON version emitted an error
  object rather than silently discarding the record.

## Schema Registry subject naming

Subjects follow the Confluent default `<topic-name>-value` (`TopicNameStrategy`)
convention, derived automatically from whatever topic name the Flink job and
producer are configured with (`kafka.input.topic`/`kafka.output.topic` or the
`KAFKA_TOPIC`/`KAFKA_OUTPUT_TOPIC` env vars). This repo's own demo defaults
to `autoscale-demo-value` and `autoscale-demo-out-value`; a deployer that
overrides the topic names (as `confluent-platform-gitops`'s
`colors-and-shapes` workload does, using `colors-input`/`colors-output`)
gets correspondingly different subjects with no code change required.

## Compatibility mode

Both schemas are registered with **BACKWARD** compatibility: a new schema
version can read data written under an older version of the same schema.
This allows adding new optional (nullable, defaulted) fields — like
`error`/`original` were added here — without breaking existing consumers.
It does not allow changing a field's type or removing a required field
without a default.

## Validating changes

Run this after editing either `.avsc` file:

```bash
python3 flink-autoscaler/schemas/validate_schemas.py
```

It checks both files are valid JSON, have the expected Avro record name, and
have the expected field names/types — catching typos and accidental field
removals before they reach a build.

## References

- [Avro Specification](https://avro.apache.org/docs/current/spec.html)
- [Confluent Schema Registry](https://docs.confluent.io/platform/current/schema-registry/index.html)
- [Schema Evolution and Compatibility](https://docs.confluent.io/platform/current/schema-registry/avro.html)
