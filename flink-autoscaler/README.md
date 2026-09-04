# Flink Autoscaler Demo

This demo showcases Apache Flink's autoscaling capabilities using Confluent Platform for Apache Flink on Kubernetes. It demonstrates how Flink automatically scales task parallelism based on workload using a Kafka producer/consumer pattern.

> [!NOTE]
> **History:** this project originally shipped plain JSON messages. A migration to Avro with Confluent Schema Registry was designed and built, but sat unmerged for a time — during which this README incorrectly described a `v0.2.0` release as already being Avro-based. As of this version, the project is **Avro-only**: there is no JSON code path, and no dual-mode configuration to choose between. The JSON version is retired; it's mentioned here only as history, not as a supported option.

## Prerequisites

Before starting, ensure you have:

- Kubernetes cluster (e.g., kind, minikube, or cloud-based)
- **Confluent for Kubernetes** operator installed
- **Confluent Platform for Apache Flink** operator installed
- **Confluent Schema Registry**, reachable from both the Flink job and the Python producer — this is a hard requirement now, not optional, since every message is Avro-encoded against it
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

1. **Flink Java Application**: Consumes Avro-encoded `SensorEvent` messages from the `autoscale-demo` topic, processes data with CPU-intensive transformations, and writes Avro-encoded `ProcessedSensorEvent` messages to the `autoscale-demo-out` topic
2. **Python Kafka Producer**: Generates sensor event data, serialized as Avro via Schema Registry, to the `autoscale-demo` topic
3. **Kafka Topics**: Input and output topics with 21 partitions each
4. **Schema Registry**: Manages the `SensorEvent`/`ProcessedSensorEvent` Avro schemas — see [Avro Schema Information](#avro-schema-information) below
5. **Autoscaler Configuration**: Aggressive autoscaling settings optimized for demo visibility

### Data Flow

```
Python Producer → Kafka Topic (Avro) → Flink Application → Kafka Topic (Avro)
       ↓                                        ↓
  Schema Registry                     Schema Registry
```

## Avro Schema Information

Full schema definitions, subject naming, and compatibility rules live in
[`schemas/README.md`](schemas/README.md) — that directory is the single
source of truth both `flink-java-app` and `python-producer` build against
directly. In short:

- **Input** (`SensorEvent`): `timestamp`, `type`, `location`, `value`, `status`, `id` — all required.
- **Output** (`ProcessedSensorEvent`): the same six fields, plus nullable `encoded` (success case), `error`, and `original` (failure case — the Flink job emits one of these two shapes for every record it processes, rather than dropping records it can't transform).
- **Subjects**: `<topic-name>-value`, derived automatically from whatever topic name is configured — this demo's defaults are `autoscale-demo-value` and `autoscale-demo-out-value`.
- **Compatibility**: `BACKWARD` — safe to add new nullable fields with defaults (as `error`/`original` were), unsafe to change a field's type or remove a required field.

### Schema Registry OAuth (optional)

If your Schema Registry requires OAuth bearer-token authentication, add
these keys to `flinkConfiguration` (Flink job) and the corresponding env
vars (Python producer), the same way Kafka OAuth is configured below:

```yaml
flinkConfiguration:
  schema.registry.url: "http://schemaregistry.kafka.svc.cluster.local:8081"
  schema.registry.bearer.auth.credentials.source: "OAUTHBEARER"
  schema.registry.bearer.auth.issuer.endpoint.url: "http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token"
  schema.registry.bearer.auth.client.id: "sa-flink-app"
  schema.registry.bearer.auth.client.secret: "sa-flink-app-secret"
  schema.registry.bearer.auth.scope: ""
  schema.registry.bearer.auth.logical.cluster: ""
  schema.registry.bearer.auth.identity.pool.id: ""
```

The service account needs `ResourceOwner` on the relevant Schema Registry
subjects, e.g.:

```yaml
apiVersion: platform.confluent.io/v1beta1
kind: ConfluentRolebinding
spec:
  principal:
    type: user
    name: sa-flink-app
  role: ResourceOwner
  clustersScopeByIds:
    schemaRegistryClusterId: id_schemaregistry_kafka
  resourcePatterns:
    - patternType: PREFIXED
      name: autoscale-demo
      resourceType: Subject
```

## Quick Start

### 1. Build the Flink Java Application

Navigate to the Flink application directory and build the JAR. This now
also runs Avro code generation (`avro-maven-plugin`) against the schemas
in `../schemas/`, producing `SensorEvent`/`ProcessedSensorEvent` Java
classes before compiling:

```bash
cd flink-java-app
mvn clean package
cd ..
```

This creates `target/kafka-flink-job-1.0-SNAPSHOT.jar`.

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

The producer's Dockerfile now needs the shared `schemas/` directory in its
build context, so build it from `flink-autoscaler/` (not from inside
`python-producer/`):

```bash
docker build -f python-producer/Dockerfile.producer -t kafka-producer:latest .
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
    kafka.input.topic: autoscale-demo
    kafka.output.topic: autoscale-demo-out
    kafka.consumer.group.id: flink-consumer-group-beta

    # Schema Registry connection
    schema.registry.url: http://schemaregistry.kafka.svc.cluster.local:8081

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
```

**How it works:**
- All properties prefixed with `kafka.` are automatically extracted by the application
- The `kafka.` prefix is removed and properties are passed to Kafka connectors
- All properties prefixed with `schema.registry.` are extracted the same way and passed to the Avro deserializer/serializer
- This enables OAuth, SASL, SSL, and other Kafka/Schema-Registry client configurations
- Environment variables in JAAS config (e.g., `${env:KAFKA_OAUTH_CLIENT_ID}`) are resolved at runtime

#### Offset Management

The Flink application respects the `auto.offset.reset` configuration from FlinkApplication, allowing you to control whether to process all messages or only new ones.

**Configuration:**
```yaml
flinkConfiguration:
  kafka.consumer.auto.offset.reset: earliest  # or latest
```

**Available options:**
- **`earliest`** (recommended for testing): Process all messages from the beginning of the topic
  - Use when you want to replay historical data
  - Ensures all messages are processed after job restart
  - Enables message correlation between input and output topics

- **`latest`** (production default): Process only new messages that arrive after the job starts
  - Skips all existing messages in the topic
  - Lower latency on job startup
  - Suitable for real-time processing where historical data isn't needed

**Example scenario:**
```bash
# Producer writes 1000 messages to topic before Flink job starts
kubectl apply -f producer-deployment.yaml

# Wait for messages to be produced...

# Deploy Flink with earliest offset
kubectl apply -f flink-application-autoscale.yaml
# Result: Flink processes all 1000 messages + any new ones

# vs. Deploy Flink with latest offset
# Result: Flink skips the 1000 existing messages, processes only new messages
```

**Note:** The application no longer hardcodes offset behavior - it always respects your FlinkApplication configuration.

#### Method 2: Environment Variables (Legacy)

Set via `podTemplate` in the FlinkApplication spec. This method is maintained for backward compatibility but doesn't support Kafka/Schema-Registry security configuration.

```yaml
podTemplate:
  spec:
    containers:
      - name: flink-main-container
        env:
          - name: KAFKA_BOOTSTRAP_SERVERS
            value: kafka.kafka.svc.cluster.local:9092
          - name: KAFKA_TOPIC
            value: autoscale-demo
          - name: KAFKA_OUTPUT_TOPIC
            value: autoscale-demo-out
          - name: KAFKA_CONSUMER_GROUP
            value: flink-consumer-group-beta
          - name: SCHEMA_REGISTRY_URL
            value: http://schemaregistry.kafka.svc.cluster.local:8081
```

**Note:** When both Flink configuration properties and environment variables are set, Flink configuration takes precedence.

### Python Producer

Set in `producer-deployment.yaml`:

- `KAFKA_BROKERS`: `kafka:9092`
- `KAFKA_TOPIC`: `autoscale-demo`
- `SCHEMA_REGISTRY_URL`: `http://schemaregistry.kafka.svc.cluster.local:8081`
- `PYTHONUNBUFFERED`: `1`

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

### Schema Registry / Avro Errors

If the Flink job or producer logs show schema or registry errors:

1. Confirm `SCHEMA_REGISTRY_URL` (or `schema.registry.url`) actually resolves and is reachable from inside the cluster
2. If Schema Registry requires OAuth, confirm the `schema.registry.bearer.auth.*` properties/env vars are set — see [Schema Registry OAuth](#schema-registry-oauth-optional)
3. Confirm the service account has `ResourceOwner` on the relevant subject (`<topic>-value`)
4. A message written without the Confluent wire-format prefix (magic byte + 4-byte schema ID) — e.g. from a stray non-Avro producer — will make every consumer of that topic fail permanently on that offset; if this happens, purge and re-create the topic

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

## Additional Resources

- [Confluent Platform for Apache Flink Documentation](https://docs.confluent.io/platform/current/flink/index.html)
- [Apache Flink Autoscaling](https://nightlies.apache.org/flink/flink-docs-master/docs/deployment/elastic_scaling/)
- [Confluent for Kubernetes](https://docs.confluent.io/operator/current/overview.html)
- [Apache Avro Specification](https://avro.apache.org/docs/current/spec.html)
- [Confluent Schema Registry](https://docs.confluent.io/platform/current/schema-registry/index.html)
