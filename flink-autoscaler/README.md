# Flink Autoscaler Demo

This demo showcases Apache Flink's autoscaling capabilities using Confluent Platform for Apache Flink on Kubernetes. It demonstrates how Flink automatically scales task parallelism based on workload using a Kafka producer/consumer pattern.

## Prerequisites

Before starting, ensure you have:

- Kubernetes cluster (e.g., kind, minikube, or cloud-based)
- **Confluent for Kubernetes** operator installed
- **Confluent Platform for Apache Flink** operator installed
- Required REST class resources created:
  - `CMFRestClass` (Confluent Metadata Framework REST class)
  - `KafkaRestClass`
- `kubectl` configured to access your cluster
- Maven 3.6+ (for building the Flink application)
- Docker (for building container images)

## Deployment Assumptions

This demo assumes the following namespace configuration:

- **Kafka cluster**: Deployed in the `kafka` namespace
- **Flink application**: Should be deployed to the `flink` namespace
- **Producer deployment**: Must be deployed in the same namespace as the Kafka cluster (default: `kafka` namespace), OR the `KAFKA_BROKERS` environment variable in `producer-deployment.yaml` must be updated to properly route to the Kafka cluster (e.g., `kafka.kafka.svc.cluster.local:9092` for cross-namespace access)

## Architecture

This demo consists of:

1. **Flink Java Application**: Consumes Avro messages from input topics, processes data with CPU-intensive transformations, and writes Avro messages to output topics
2. **Python Kafka Producer**: Generates sensor event data serialized as Avro messages with Schema Registry integration
3. **Kafka Topics**: Input and output topics with Avro schema registration
4. **Schema Registry**: Manages Avro schemas with OAuth bearer token authentication
5. **Autoscaler Configuration**: Aggressive autoscaling settings optimized for demo visibility

### Data Flow

```
Python Producer → Kafka Topic (Avro) → Flink Application → Kafka Topic (Avro)
       ↓                                        ↓
  Schema Registry (OAuth)            Schema Registry (OAuth)
```

The application uses **Avro serialization** with **Confluent Schema Registry** for strongly-typed, versioned data contracts between producers and consumers.

## Avro Schema Information

### Why Avro?

This demo uses [Apache Avro](https://avro.apache.org/) for message serialization with the following benefits:

- **Strongly-typed**: Schema defines exact data structure with type safety
- **Compact**: Binary encoding is smaller than JSON (important for high-throughput scenarios)
- **Schema Evolution**: Forward and backward compatibility for changing data structures
- **Self-documenting**: Schema serves as documentation for data format
- **Language-agnostic**: Schemas work across Python, Java, and other languages

### Schema Registry Integration

The demo integrates with **Confluent Schema Registry** for centralized schema management:

- **Automatic Registration**: Producers register schemas on first use
- **Schema Versioning**: Each schema change creates a new version
- **Compatibility Checking**: Registry enforces compatibility rules during registration
- **OAuth Authentication**: Uses bearer token authentication with Keycloak
- **CMF Table Discovery**: Flink SQL can auto-discover tables from Schema Registry

### Schema Definitions

Schemas are defined in `schemas/` directory and copied to `flink-java-app/src/main/avro/` for Java code generation.

#### Input Schema: SensorEvent

```json
{
  "type": "record",
  "name": "SensorEvent",
  "namespace": "com.confluent.examples.sensors",
  "fields": [
    {"name": "timestamp", "type": "string"},
    {"name": "type", "type": "string"},
    {"name": "location", "type": "string"},
    {"name": "value", "type": "double"},
    {"name": "status", "type": "string"},
    {"name": "id", "type": "string"}
  ]
}
```

**Registered as**: `colors-input-value` and `shapes-input-value`

#### Output Schema: ProcessedSensorEvent

```json
{
  "type": "record",
  "name": "ProcessedSensorEvent",
  "namespace": "com.confluent.examples.sensors",
  "fields": [
    {"name": "timestamp", "type": "string"},
    {"name": "type", "type": "string"},
    {"name": "location", "type": "string"},
    {"name": "value", "type": "double"},
    {"name": "status", "type": "string"},
    {"name": "id", "type": "string"},
    {"name": "encoded", "type": "string"}
  ]
}
```

**Registered as**: `colors-output-value` and `shapes-output-value`

**Note**: The output schema adds an `encoded` field containing a Base64-encoded transformation of the timestamp and location, demonstrating schema evolution (adding a field).

### Schema Registry OAuth Configuration

Both the Python producer and Flink application authenticate to Schema Registry using OAuth bearer tokens:

**Python Producer** (`producer.py`):
```python
schema_registry_conf = {
    'url': 'http://schemaregistry.kafka.svc.cluster.local:8081',
    'bearer.auth.credentials.source': 'OAUTHBEARER',
    'bearer.auth.issuer.endpoint.url': 'http://keycloak...token',
    'bearer.auth.client.id': 'sa-colors-producer',
    'bearer.auth.client.secret': 'sa-colors-producer-secret'
}
```

**Flink Application** (`flink-application-colors.yaml`):
```yaml
flinkConfiguration:
  schema.registry.url: "http://schemaregistry.kafka.svc.cluster.local:8081"
  schema.registry.bearer.auth.credentials.source: "OAUTHBEARER"
  schema.registry.bearer.auth.token.endpoint.url: "http://keycloak...token"
  schema.registry.bearer.auth.client.id: "sa-colors-flink"
  schema.registry.bearer.auth.client.secret: "sa-colors-flink-secret"
```

### RBAC Permissions

Service accounts require **ResourceOwner** role on Schema Registry subjects:

```yaml
apiVersion: platform.confluent.io/v1beta1
kind: ConfluentRolebinding
spec:
  principal:
    type: user
    name: sa-colors-flink
  role: ResourceOwner
  clustersScopeByIds:
    schemaRegistryClusterId: id_schemaregistry_kafka
  resourcePatterns:
    - patternType: PREFIXED
      name: colors-
      resourceType: Subject
```

This grants permission to register, read, and evolve schemas with the `colors-` prefix.

## Schema Evolution

### Compatibility Modes

Confluent Schema Registry supports multiple compatibility modes:

| Mode | Description | Use Case |
|------|-------------|----------|
| **BACKWARD** | New schema can read old data | Consumer upgrades (default) |
| **FORWARD** | Old schema can read new data | Producer upgrades |
| **FULL** | Both backward and forward | Flexible upgrades |
| **NONE** | No compatibility checking | Development/testing |

**Default mode**: BACKWARD (new consumers can process old messages)

### Safe Schema Changes

#### ✅ Backward Compatible (Safe)

These changes allow new consumers to read old data:

- **Adding optional fields** (with default values)
  ```json
  {"name": "new_field", "type": ["null", "string"], "default": null}
  ```
- **Removing fields** (old messages have the field, new schema ignores it)
- **Adding enum values** (if used with defaults)

#### ✅ Forward Compatible (Safe)

These changes allow old consumers to read new data:

- **Removing optional fields**
- **Adding fields with defaults**
- **Removing enum values** (that aren't used)

#### ❌ Incompatible (Breaking)

These changes break compatibility:

- **Removing required fields** (no default)
- **Changing field types** (e.g., `string` → `int`)
- **Renaming fields** (seen as remove + add)
- **Adding required fields without defaults**

### Example: Adding a Field (Backward Compatible)

Our demo demonstrates this with the `encoded` field:

**Before** (SensorEvent):
```json
{
  "fields": [
    {"name": "timestamp", "type": "string"},
    {"name": "id", "type": "string"}
  ]
}
```

**After** (ProcessedSensorEvent):
```json
{
  "fields": [
    {"name": "timestamp", "type": "string"},
    {"name": "id", "type": "string"},
    {"name": "encoded", "type": "string"}  // New field added
  ]
}
```

This is **backward compatible** because:
- Old consumers (without `encoded`) can still read the first 6 fields
- New consumers get the additional `encoded` field
- Flink application evolved to add this field during processing

### Testing Schema Evolution

To test schema evolution in this demo:

1. **Register the base schema**:
   ```bash
   # Start producer to register input schema
   kubectl scale deployment colors-producer -n flink-colors --replicas=1
   ```

2. **Verify registration**:
   ```bash
   # Check Schema Registry (requires curl in cluster)
   kubectl exec -it schemaregistry-0 -n kafka -- \
     curl http://localhost:8081/subjects/colors-input-value/versions/latest
   ```

3. **Evolve the schema**:
   - Modify `schemas/sensor-event-input-value.avsc`
   - Add a new optional field with a default value
   - Rebuild and redeploy the producer

4. **Verify compatibility**:
   - Schema Registry will accept the new version if compatible
   - Old Flink consumers continue processing without redeployment
   - New Flink consumers see the additional field

### Best Practices

1. **Always use optional fields** for new additions:
   ```json
   {"name": "new_field", "type": ["null", "string"], "default": null}
   ```

2. **Test compatibility before deploying**:
   - Use Schema Registry's compatibility API
   - Test with consumer/producer combinations

3. **Version your schemas**:
   - Keep schema files in version control
   - Document breaking changes in commit messages

4. **Use meaningful defaults**:
   ```json
   {"name": "region", "type": "string", "default": "unknown"}
   ```

5. **Plan for evolution**:
   - Design schemas with future changes in mind
   - Use unions and optional fields liberally
   - Avoid required fields unless truly necessary

### Schema Registry API

Check schema compatibility before registering:

```bash
# Test if new schema is compatible
curl -X POST http://schemaregistry:8081/compatibility/subjects/colors-input-value/versions/latest \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"schema": "{\"type\":\"record\",...}"}'
```

Response:
```json
{"is_compatible": true}
```

## Quick Start

### 1. Build the Flink Java Application

Navigate to the Flink application directory and build the JAR:

```bash
cd flink-java-app
mvn clean package
cd ..
```

This creates `target/kafka-flink-job-1.0-SNAPSHOT.jar` with:
- Avro schema-generated Java classes (`SensorEvent`, `ProcessedSensorEvent`)
- Confluent Schema Registry client libraries
- Flink Avro serialization/deserialization support
- All dependencies in a shaded JAR (~24MB)

### 2. Build the Flink Container Image

Build the Flink application container image:

```bash
cd flink-java-app
docker build -t flink-kafka-demo:latest .
cd ..
```

**Option A: Push to Container Registry**

```bash
# Tag for your registry
docker tag flink-kafka-demo:latest <your-registry>/flink-kafka-demo:latest

# Push to registry
docker push <your-registry>/flink-kafka-demo:latest
```

Update `flink-application-autoscale.yaml`:
- Change `image:` to `<your-registry>/flink-kafka-demo:latest`
- Change `imagePullPolicy:` to `Always` or `IfNotPresent`

**Option B: Load into kind (for local clusters)**

```bash
kind load docker-image flink-kafka-demo:latest
```

Keep `imagePullPolicy: Never` in the YAML.

### 3. Build the Python Producer Container Image

Build the Kafka producer image:

```bash
cd python-producer
docker build -t kafka-producer:latest -f Dockerfile.producer .
cd ..
```

**Option A: Push to Container Registry**

```bash
# Tag for your registry
docker tag kafka-producer:latest <your-registry>/kafka-producer:latest

# Push to registry
docker push <your-registry>/kafka-producer:latest
```

Update `python-producer/producer-deployment.yaml`:
- Change `image:` to `<your-registry>/kafka-producer:latest`
- Change `imagePullPolicy:` to `Always` or `IfNotPresent`

**Option B: Load into kind (for local clusters)**

```bash
kind load docker-image kafka-producer:latest
```

Keep `imagePullPolicy: Never` in the YAML.

### 4. Create Kafka Topics

Create the input and output topics:

```bash
kubectl apply -f kafka-topics.yaml -n kafka
```

Verify topics are created:

```bash
kubectl get kafkatopics -n kafka
```

You should see:
- `autoscale-demo` (21 partitions)
- `autoscale-demo-out` (21 partitions)

### 5. Deploy the Flink Application

Deploy the Flink application with autoscaling enabled:

```bash
kubectl apply -f flink-application-autoscale.yaml
```

Monitor the Flink application status:

```bash
kubectl get flinkapplication -n flink
kubectl describe flinkapplication flink-kafka-autoscale-demo -n flink
```

Check Flink pods:

```bash
kubectl get pods -n flink -l app=flink-kafka-demo
```

### 6. Deploy the Kafka Producer

Start the producer to generate load:

```bash
kubectl apply -f python-producer/producer-deployment.yaml -n kafka
```

Verify the producer is running:

```bash
kubectl get pods -l app=kafka-producer -n kafka
kubectl logs -f deployment/kafka-producer -n kafka
```

## Observing Autoscaling

### Watch Task Manager Scaling

Monitor the Flink TaskManagers as they scale up:

```bash
kubectl get pods -n flink -l app=flink-kafka-demo -w
```

### Check Autoscaler Metrics

View the FlinkApplication autoscaler status:

```bash
kubectl describe flinkapplication flink-kafka-autoscale-demo -n flink | grep -A 20 "Autoscaler"
```

### Monitor Job Parallelism

Check the Flink job parallelism changes:

```bash
kubectl get flinkapplication flink-kafka-autoscale-demo -n flink -o jsonpath='{.status.jobStatus.parallelism}'
```

## Autoscaler Configuration

The demo uses aggressive autoscaling settings for quick demonstration:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `target.utilization` | 0.08 (8%) | Very low threshold for fast scaling |
| `metrics.window` | 2m | Metrics collection window |
| `stabilization.interval` | 30s | Quick decision making |
| `scale-down.grace-period` | 2m | Wait before scaling down |
| `scale-down.interval` | 5m | Cooldown after scaling |
| `max-parallelism` | 4 | Maximum parallelism limit |

These settings are optimized for demo visibility. For production, use more conservative values.

## Configuration

### Flink Application Configuration

The Flink application supports configuration via two methods:

#### Method 1: Flink Configuration Properties (Recommended)

Set Kafka properties in the FlinkApplication spec under `flinkConfiguration`. This is the recommended approach for OAuth/SASL authentication and other advanced Kafka configurations.

```yaml
apiVersion: platform.confluent.io/v1beta1
kind: FlinkApplication
metadata:
  name: flink-kafka-autoscale-demo
spec:
  flinkConfiguration:
    # Kafka connection
    kafka.bootstrap.servers: kafka.kafka.svc.cluster.local:9071
    kafka.input.topic: colors-input
    kafka.output.topic: colors-output
    kafka.consumer.group.id: flink-consumer-group-beta

    # Kafka security (OAuth example)
    kafka.security.protocol: SASL_PLAINTEXT
    kafka.sasl.mechanism: OAUTHBEARER
    kafka.sasl.oauthbearer.token.endpoint.url: http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token
    kafka.sasl.login.callback.handler.class: org.apache.kafka.common.security.oauthbearer.OAuthBearerLoginCallbackHandler
    kafka.sasl.jaas.config: |
      org.apache.kafka.common.security.oauthbearer.OAuthBearerLoginModule required
      clientId="${env:KAFKA_OAUTH_CLIENT_ID}"
      clientSecret="${env:KAFKA_OAUTH_CLIENT_SECRET}"
      tokenEndpointUri="http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token";

    # Schema Registry configuration (for Avro serialization)
    schema.registry.url: "http://schemaregistry.kafka.svc.cluster.local:8081"
    schema.registry.bearer.auth.credentials.source: "OAUTHBEARER"
    schema.registry.bearer.auth.token.endpoint.url: "http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token"
    schema.registry.bearer.auth.client.id: "sa-colors-flink"
    schema.registry.bearer.auth.client.secret: "sa-colors-flink-secret"
```

**How it works:**
- All properties prefixed with `kafka.` are automatically extracted by the application and passed to Kafka connectors (prefix removed)
- All properties prefixed with `schema.registry.` are extracted and passed to Avro serializers/deserializers
- This enables OAuth, SASL, SSL, and other Kafka client configurations
- Schema Registry OAuth authentication uses the same pattern as Kafka
- Environment variables in JAAS config (e.g., `${env:KAFKA_OAUTH_CLIENT_ID}`) are resolved at runtime

#### Method 2: Environment Variables (Legacy)

Set via `podTemplate` in the FlinkApplication spec. This method is maintained for backward compatibility but doesn't support Kafka security configuration.

```yaml
podTemplate:
  spec:
    containers:
      - name: flink-main-container
        env:
          - name: KAFKA_BOOTSTRAP_SERVERS
            value: kafka.kafka.svc.cluster.local:9092
          - name: KAFKA_TOPIC
            value: colors-input
          - name: KAFKA_OUTPUT_TOPIC
            value: colors-output
          - name: KAFKA_CONSUMER_GROUP
            value: flink-consumer-group-beta
          - name: SCHEMA_REGISTRY_URL
            value: http://schemaregistry.kafka.svc.cluster.local:8081
```

**Note:** When both Flink configuration properties and environment variables are set, Flink configuration takes precedence. The environment variable method does not support OAuth authentication for Schema Registry.

### Python Producer

Set in `producer-deployment.yaml`:

- `KAFKA_BOOTSTRAP_SERVERS`: Kafka bootstrap servers (e.g., `kafka.kafka.svc.cluster.local:9071`)
- `KAFKA_TOPIC`: Input topic name (e.g., `colors-input`)
- `KAFKA_SECURITY_PROTOCOL`: `SASL_PLAINTEXT` (for OAuth)
- `KAFKA_SASL_MECHANISM`: `OAUTHBEARER`
- `KAFKA_OAUTH_TOKEN_ENDPOINT_URL`: OAuth token endpoint
- `KAFKA_OAUTH_CLIENT_ID`: Service account client ID (from secret)
- `KAFKA_OAUTH_CLIENT_SECRET`: Service account secret (from secret)
- `SCHEMA_REGISTRY_URL`: Schema Registry endpoint
- `MESSAGE_RATE`: Messages per second (default: `10`)
- `PYTHONUNBUFFERED`: `1`

**Note**: The producer automatically reuses Kafka OAuth credentials for Schema Registry authentication.

## Cleanup

Remove all resources:

```bash
# Delete producer
kubectl delete -f python-producer/producer-deployment.yaml -n kafka

# Delete Flink application
kubectl delete -f flink-application-autoscale.yaml -n flink

# Delete Kafka topics
kubectl delete -f kafka-topics.yaml -n kafka
```

## Troubleshooting

### Flink Application Not Starting

Check the JobManager logs:

```bash
kubectl logs -n flink -l component=jobmanager,app=flink-kafka-demo
```

### Producer Not Sending Data

Check producer logs:

```bash
kubectl logs -l app=kafka-producer -n kafka
```

### No Autoscaling Observed

1. Verify autoscaler is enabled in FlinkApplication spec
2. Check that CMFRestClass is properly configured
3. Ensure sufficient load is being generated by the producer
4. Review autoscaler metrics window and stabilization settings

### Topics Not Created

Verify KafkaRestClass exists:

```bash
kubectl get kafkarestclass -n kafka
```

Check Kafka cluster status:

```bash
kubectl get kafka -n kafka
```

### Schema Registry Issues

Check if schemas are registered:

```bash
# List all subjects
kubectl exec -it schemaregistry-0 -n kafka -- \
  curl http://localhost:8081/subjects

# Get specific schema version
kubectl exec -it schemaregistry-0 -n kafka -- \
  curl http://localhost:8081/subjects/colors-input-value/versions/latest
```

Verify OAuth authentication:

```bash
# Check producer logs for Schema Registry auth errors
kubectl logs -l app=colors-producer -n flink-colors | grep -i "schema\|403\|401"

# Check Flink logs for Schema Registry auth errors
kubectl logs -l app=flink -n flink-colors | grep -i "schema\|403\|401"
```

Common issues:
- **403 Forbidden**: Missing ConfluentRolebinding for Subject permissions
- **Connection refused**: Schema Registry not running or incorrect URL
- **Invalid token**: OAuth credentials incorrect or expired

## Additional Resources

### Flink & Autoscaling
- [Confluent Platform for Apache Flink Documentation](https://docs.confluent.io/platform/current/flink/index.html)
- [Apache Flink Autoscaling](https://nightlies.apache.org/flink/flink-docs-master/docs/deployment/elastic_scaling/)
- [Confluent for Kubernetes](https://docs.confluent.io/operator/current/overview.html)

### Avro & Schema Registry
- [Apache Avro Documentation](https://avro.apache.org/docs/current/)
- [Confluent Schema Registry Documentation](https://docs.confluent.io/platform/current/schema-registry/index.html)
- [Schema Evolution and Compatibility](https://docs.confluent.io/platform/current/schema-registry/avro.html#compatibility-types)
- [Flink Avro Format](https://nightlies.apache.org/flink/flink-docs-release-1.18/docs/connectors/datastream/formats/avro/)

### Security & OAuth
- [Confluent RBAC Documentation](https://docs.confluent.io/platform/current/security/rbac/index.html)
- [Schema Registry Security](https://docs.confluent.io/platform/current/schema-registry/security/index.html)
- [OAuth/OIDC Authentication](https://docs.confluent.io/platform/current/kafka/authentication_sasl/authentication_sasl_oauth.html)
