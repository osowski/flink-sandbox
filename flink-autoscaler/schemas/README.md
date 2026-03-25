# Avro Schema Definitions

This directory contains Avro schema definitions for the Flink autoscaler demo pipeline. These schemas enable Schema Registry integration and CMF (Confluent Metadata Framework) auto-discovery of Flink SQL tables.

## Schema Files

### sensor-event-input-value.avsc
**Purpose**: Input schema for sensor events produced by the Python Kafka producer
**Namespace**: `com.confluent.examples.sensors`
**Record Name**: `SensorEvent`
**Used By**:
- Python producer (serialization)
- Flink application (deserialization from input topics)

**Schema Subjects** (in Schema Registry):
- `shapes-input-value`
- `colors-input-value`

**Fields**:
- `timestamp` (string): ISO 8601 timestamp
- `type` (string): Data type (temperature, pressure, humidity, speed, count)
- `location` (string): Sensor location (sensor-A, sensor-B, sensor-C, etc.)
- `value` (double): Numeric sensor reading
- `status` (string): Sensor status (normal, warning, critical)
- `id` (string): Unique event identifier

### sensor-event-output-value.avsc
**Purpose**: Output schema for processed events written by Flink application
**Namespace**: `com.confluent.examples.sensors`
**Record Name**: `ProcessedSensorEvent`
**Used By**:
- Flink application (serialization to output topics)

**Schema Subjects** (in Schema Registry):
- `shapes-output-value`
- `colors-output-value`

**Fields**:
- All fields from `SensorEvent` (timestamp, type, location, value, status, id)
- `encoded` (nullable string): Base64-encoded transformation result from Flink
- `error` (nullable string): Error message if processing failed
- `original` (nullable string): Original value if error occurred

## Schema Registry Integration

### Subject Naming Convention
Schemas are registered in Schema Registry using the following naming pattern:
```
<topic-name>-value
```

Examples:
- Topic: `shapes-input` → Subject: `shapes-input-value`
- Topic: `shapes-output` → Subject: `shapes-output-value`
- Topic: `colors-input` → Subject: `colors-input-value`
- Topic: `colors-output` → Subject: `colors-output-value`

### Compatibility Mode
All schemas use **BACKWARD** compatibility:
- New schema versions can read data written with old schemas
- Allows adding optional fields (with defaults)
- Cannot change field types or remove required fields
- Safe for rolling upgrades

## Usage

### Python Producer
The producer loads the input schema at runtime:

```python
from confluent_kafka import avro

# Load schema from file
with open('schemas/sensor-event-input-value.avsc', 'r') as f:
    value_schema_str = f.read()
value_schema = avro.loads(value_schema_str)

# Create AvroProducer
producer = AvroProducer({
    'bootstrap.servers': kafka_brokers,
    'schema.registry.url': sr_url,
    # ... other config
}, default_value_schema=value_schema)
```

### Flink Application
Flink uses the avro-maven-plugin to generate Java POJOs from these schemas:

```xml
<plugin>
    <groupId>org.apache.avro</groupId>
    <artifactId>avro-maven-plugin</artifactId>
    <executions>
        <execution>
            <phase>generate-sources</phase>
            <goals>
                <goal>schema</goal>
            </goals>
            <configuration>
                <sourceDirectory>${project.basedir}/../schemas</sourceDirectory>
                <outputDirectory>${project.build.directory}/generated-sources/avro</outputDirectory>
            </configuration>
        </execution>
    </executions>
</plugin>
```

Generated classes:
- `com.confluent.examples.sensors.SensorEvent`
- `com.confluent.examples.sensors.ProcessedSensorEvent`

## Schema Evolution

### Safe Changes (BACKWARD Compatible)
✅ Add optional field with default:
```json
{
  "name": "newField",
  "type": ["null", "string"],
  "default": null
}
```

✅ Remove optional field (if consumers don't require it)

✅ Add field aliases:
```json
{
  "name": "location",
  "type": "string",
  "aliases": ["sensor_location", "loc"]
}
```

### Unsafe Changes (Breaking)
❌ Change field type (string → int)
❌ Rename field without alias
❌ Remove required field
❌ Add required field without default

For breaking changes, create a new schema with a new subject name or increment major version.

## Validation

Validate schema syntax with Avro tools:

```bash
# Install avro-tools
pip install avro

# Validate schema
python -c "import avro.schema; avro.schema.parse(open('schemas/sensor-event-input-value.avsc').read())"
```

Test schema compatibility with Schema Registry:

```bash
# Test compatibility before deploying
curl -X POST \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  --data @schemas/sensor-event-input-value.avsc \
  http://schemaregistry.kafka.svc.cluster.local:8081/compatibility/subjects/shapes-input-value/versions/latest
```

## References

- [Avro Specification](https://avro.apache.org/docs/current/spec.html)
- [Confluent Schema Registry](https://docs.confluent.io/platform/current/schema-registry/index.html)
- [Schema Evolution and Compatibility](https://docs.confluent.io/platform/current/schema-registry/avro.html)

## Source

These schemas are derived from:
https://github.com/osowski/confluent-platform-gitops/blob/main/workloads/confluent-resources/overlays/flink-demo-rbac/schemas.yaml
