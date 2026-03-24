# Kafka Producer Application

Python Kafka producer using Confluent Kafka libraries with support for OAuth authentication, multiple data generation modes, and flexible rate control.

## Features

- **OAuth Authentication Support** - SASL/OAUTHBEARER with OIDC client credentials flow
- **Multiple Data Modes** - Autoscaler (default), shapes, or colors data generation
- **Flexible Rate Control** - Fixed rate or sinusoidal pattern
- **Backward Compatible** - Works with secured and unsecured Kafka clusters
- **Enhanced Error Handling** - Detailed authentication and authorization error reporting
- Comprehensive logging with structured output
- JSON-formatted messages with timestamps
- Delivery callbacks for monitoring

## Building the Docker Image

```bash
docker build -t quay.io/osowski/python-kafka-producer:latest -f Dockerfile.producer .
```

## Pushing to Container Registry

```bash
docker push quay.io/osowski/python-kafka-producer:latest
```

## Loading Image into kind Cluster (Local Development)

```bash
kind load docker-image quay.io/osowski/python-kafka-producer:latest
```

## Deploying to Kubernetes

### Option 1: Basic Deployment (Unsecured Kafka)

```bash
kubectl apply -f producer-deployment.yaml
```

### Option 2: OAuth-Secured Kafka Deployment

See the confluent-platform-gitops repository for production-ready Deployment manifests with OAuth configuration:
- `workloads/flink-resources/overlays/flink-demo-rbac/producers.yaml`

## Viewing Logs

```bash
kubectl logs -f deployment/kafka-producer

# Or for labeled deployments
kubectl logs -n flink-shapes -l app=shapes-producer -f
kubectl logs -n flink-colors -l app=colors-producer -f
```

## Configuration

The producer is configured via environment variables.

### Core Configuration

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap servers | - | Yes* |
| `KAFKA_BROKERS` | Legacy: Kafka bootstrap servers | `kafka:9092` | Yes* |
| `KAFKA_TOPIC` | Topic name to produce messages to | `autoscale-demo` | Yes |
| `DATA_TYPE` | Data generation mode: `autoscale`, `shapes`, or `colors` | `autoscale` | No |
| `MESSAGE_RATE` | Fixed rate in msg/sec (enables fixed rate mode) | - | No |

*Note: Either `KAFKA_BOOTSTRAP_SERVERS` or `KAFKA_BROKERS` must be set.

### OAuth Authentication (Optional)

| Variable | Description | Example |
|----------|-------------|---------|
| `KAFKA_SECURITY_PROTOCOL` | Security protocol | `SASL_PLAINTEXT` |
| `KAFKA_SASL_MECHANISM` | SASL mechanism | `OAUTHBEARER` |
| `KAFKA_OAUTH_CLIENT_ID` | OAuth client ID | `sa-shapes-producer` |
| `KAFKA_OAUTH_CLIENT_SECRET` | OAuth client secret | `sa-shapes-producer-secret` |
| `KAFKA_OAUTH_TOKEN_ENDPOINT_URL` | OAuth token endpoint | `http://keycloak:8080/realms/confluent/protocol/openid-connect/token` |
| `KAFKA_OAUTH_SCOPE` | Optional OAuth scope | - |

## Usage Examples

### Example 1: Autoscaler Mode (Sinusoidal Pattern, Unsecured)

Default behavior - generates sensor data at a sinusoidal rate (550±450 msg/sec).

```bash
export KAFKA_BROKERS="kafka:9092"
export KAFKA_TOPIC="autoscale-demo"

python producer.py
```

### Example 2: Shapes Mode (Fixed Rate, OAuth-Secured)

Generates shape data at a fixed rate with OAuth authentication.

```bash
export KAFKA_BOOTSTRAP_SERVERS="kafka.kafka.svc.cluster.local:9071"
export KAFKA_TOPIC="shapes-input"
export DATA_TYPE="shapes"
export MESSAGE_RATE="10"

# OAuth configuration
export KAFKA_SECURITY_PROTOCOL="SASL_PLAINTEXT"
export KAFKA_SASL_MECHANISM="OAUTHBEARER"
export KAFKA_OAUTH_CLIENT_ID="sa-shapes-producer"
export KAFKA_OAUTH_CLIENT_SECRET="sa-shapes-producer-secret"
export KAFKA_OAUTH_TOKEN_ENDPOINT_URL="http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token"

python producer.py
```

### Example 3: Colors Mode (Fixed Rate, OAuth-Secured)

Generates color data at a fixed rate with OAuth authentication.

```bash
export KAFKA_BOOTSTRAP_SERVERS="kafka.kafka.svc.cluster.local:9071"
export KAFKA_TOPIC="colors-input"
export DATA_TYPE="colors"
export MESSAGE_RATE="10"

# OAuth configuration (same as shapes example)
export KAFKA_SECURITY_PROTOCOL="SASL_PLAINTEXT"
export KAFKA_SASL_MECHANISM="OAUTHBEARER"
export KAFKA_OAUTH_CLIENT_ID="sa-colors-producer"
export KAFKA_OAUTH_CLIENT_SECRET="sa-colors-producer-secret"
export KAFKA_OAUTH_TOKEN_ENDPOINT_URL="http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token"

python producer.py
```

### Example 4: Kubernetes Deployment with OAuth

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shapes-producer
  namespace: flink-shapes
spec:
  replicas: 1
  selector:
    matchLabels:
      app: shapes-producer
  template:
    metadata:
      labels:
        app: shapes-producer
    spec:
      containers:
        - name: producer
          image: quay.io/osowski/python-kafka-producer:latest
          env:
            # Kafka connection
            - name: KAFKA_BOOTSTRAP_SERVERS
              value: "kafka.kafka.svc.cluster.local:9071"
            - name: KAFKA_TOPIC
              value: "shapes-input"
            - name: DATA_TYPE
              value: "shapes"
            - name: MESSAGE_RATE
              value: "10"

            # OAuth configuration
            - name: KAFKA_SECURITY_PROTOCOL
              value: "SASL_PLAINTEXT"
            - name: KAFKA_SASL_MECHANISM
              value: "OAUTHBEARER"
            - name: KAFKA_OAUTH_TOKEN_ENDPOINT_URL
              value: "http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent/protocol/openid-connect/token"

            # OAuth credentials from secret
            - name: KAFKA_OAUTH_CLIENT_ID
              valueFrom:
                secretKeyRef:
                  name: shapes-producer-oauth
                  key: client-id
            - name: KAFKA_OAUTH_CLIENT_SECRET
              valueFrom:
                secretKeyRef:
                  name: shapes-producer-oauth
                  key: client-secret
```

## Data Generation Modes

### Autoscale Mode (`DATA_TYPE=autoscale`)

Generates randomized sensor data with sinusoidal rate pattern (default behavior).

**Rate:** 550 + 450 * sin(t/60) messages per second (oscillates between ~100-1000 msg/sec)

**Message Format:**
```json
{
  "timestamp": "2024-01-01T12:00:00.000000",
  "type": "temperature|pressure|humidity|speed|count",
  "location": "sensor-A|sensor-B|sensor-C|sensor-D|sensor-E",
  "value": 42.15,
  "status": "normal|warning|critical",
  "id": "abc123de"
}
```

**Key:** `key-{1-10}`, `id-{1-10}`, or `partition-{1-10}` (randomized)

### Shapes Mode (`DATA_TYPE=shapes`)

Generates shape-based data for Flink demos.

**Rate:** Controlled by `MESSAGE_RATE` environment variable (e.g., 10 msg/sec)

**Message Format:**
```json
{
  "timestamp": "2024-01-01T12:00:00.000000",
  "message_id": 123,
  "producer": "python-kafka-producer",
  "shape": "circle|square|triangle|diamond|trapezoid",
  "size": 42,
  "color": "red|blue|green|yellow|orange"
}
```

**Key:** Sequential integer (message_id)

### Colors Mode (`DATA_TYPE=colors`)

Generates color-based data for Flink demos.

**Rate:** Controlled by `MESSAGE_RATE` environment variable (e.g., 10 msg/sec)

**Message Format:**
```json
{
  "timestamp": "2024-01-01T12:00:00.000000",
  "message_id": 123,
  "producer": "python-kafka-producer",
  "color": "red|green|blue|yellow|orange",
  "intensity": 85,
  "hue": 240
}
```

**Key:** Sequential integer (message_id)

## Rate Control

### Sinusoidal Pattern (Default)

When `MESSAGE_RATE` is not set, the producer uses a sinusoidal pattern to vary throughput:

```
rate = 550 + 450 * sin(t/60)
```

This creates a wave pattern oscillating between approximately 100-1000 messages per second, useful for testing autoscaling behavior.

### Fixed Rate

When `MESSAGE_RATE` is set, the producer sends a constant number of messages per second:

```bash
export MESSAGE_RATE="10"  # Send 10 messages per second
```

This is useful for consistent data generation in demos and testing.

## OAuth Authentication

The producer supports OAuth/SASL authentication using the OIDC client credentials flow.

### How It Works

1. Producer starts and reads OAuth credentials from environment variables
2. Connects to Kafka using SASL_PLAINTEXT with OAUTHBEARER mechanism
3. Authenticates to OAuth provider (e.g., Keycloak) using client credentials
4. Receives JWT token from token endpoint
5. Presents token to Kafka broker on connection
6. Kafka validates token and checks RBAC permissions
7. Producer writes messages to authorized topics

### Error Handling

The producer provides detailed error messages for common authentication and authorization failures:

**Authentication Errors:**
```
Authentication error: SASL authentication failed
Check OAuth credentials and Keycloak connectivity
Verify KAFKA_OAUTH_CLIENT_ID and KAFKA_OAUTH_CLIENT_SECRET environment variables
```

**Authorization Errors:**
```
Authorization error: Not authorized to access topic
Check RBAC permissions for the service account
```

### Security Best Practices

**Production Deployments:**
- Use Kubernetes Secrets to store OAuth credentials (never hardcode)
- Use TLS (SASL_SSL) instead of SASL_PLAINTEXT in production
- Rotate client secrets regularly
- Use dedicated service accounts per application
- Apply least-privilege RBAC permissions

**Example with Kubernetes Secret:**
```yaml
# Create secret
apiVersion: v1
kind: Secret
metadata:
  name: shapes-producer-oauth
  namespace: flink-shapes
type: Opaque
stringData:
  client-id: sa-shapes-producer
  client-secret: sa-shapes-producer-secret

# Reference in Deployment
env:
  - name: KAFKA_OAUTH_CLIENT_ID
    valueFrom:
      secretKeyRef:
        name: shapes-producer-oauth
        key: client-id
  - name: KAFKA_OAUTH_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: shapes-producer-oauth
        key: client-secret
```

## Dependencies

**Python Packages:**
- `confluent-kafka==2.3.0` - Confluent Kafka Python client (includes OAuth support)

OAuth authentication is supported in confluent-kafka since version 2.0.0. No additional dependencies required.

## Troubleshooting

### Producer Won't Connect to Kafka

**Symptoms:** Connection timeout or refused

**Solutions:**
- Verify `KAFKA_BOOTSTRAP_SERVERS` is correct
- Check network connectivity: `kubectl exec -it <pod> -- nc -zv kafka.kafka.svc.cluster.local 9071`
- Verify Kafka is running: `kubectl get pods -n kafka`
- Check Kafka listener configuration (port 9071 for OAuth, 9092 for plaintext)

### OAuth Authentication Failures

**Symptoms:** "SASL authentication failed" errors

**Solutions:**
- Verify OAuth credentials are correct
- Check Keycloak is accessible: `kubectl exec -it <pod> -- curl http://keycloak.keycloak.svc.cluster.local:8080/realms/confluent`
- Verify token endpoint URL is correct
- Check service account exists in Keycloak
- Verify service account has correct client secret

### Authorization Failures

**Symptoms:** "Not authorized to access topic" errors

**Solutions:**
- Verify RBAC permissions are configured for the service account
- Check ConfluentRoleBindings exist: `kubectl get confluentrolebindings -n kafka`
- Verify service account has ResourceOwner role on the topic pattern
- Check service account group membership in Keycloak

### No Messages Appearing in Topic

**Symptoms:** Producer logs show success but consumers see no messages

**Solutions:**
- Verify topic exists: `kubectl exec -it -n kafka kafka-0 -- kafka-topics --list --bootstrap-server localhost:9092`
- Check producer logs for delivery errors: `kubectl logs -l app=shapes-producer`
- Verify consumer is reading from correct topic and offset
- Check Kafka broker logs for errors

## Cleanup

```bash
# Delete deployment
kubectl delete -f producer-deployment.yaml

# Or for labeled deployments
kubectl delete deployment shapes-producer -n flink-shapes
kubectl delete deployment colors-producer -n flink-colors

# Delete secrets
kubectl delete secret shapes-producer-oauth -n flink-shapes
kubectl delete secret colors-producer-oauth -n flink-colors
```

## Related Documentation

**Confluent Platform GitOps:**
- Production deployment manifests: `workloads/flink-resources/overlays/flink-demo-rbac/producers.yaml`
- OAuth secret configuration: `workloads/flink-resources/overlays/flink-demo-rbac/producer-oauth-secrets.yaml`
- RBAC permissions: `workloads/confluent-resources/overlays/flink-demo-rbac/confluentrolebindings.yaml`

**Confluent Documentation:**
- [Confluent Python Client](https://docs.confluent.io/kafka-clients/python/current/overview.html)
- [OAuth Authentication](https://docs.confluent.io/kafka-clients/python/current/overview.html#authentication-oauth)
- [RBAC Authorization](https://docs.confluent.io/platform/current/security/rbac/index.html)

## License

See repository root for license information.
