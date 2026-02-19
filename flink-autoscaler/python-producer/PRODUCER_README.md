# Kafka Producer Application

Python Kafka producer using Confluent Kafka libraries with randomized data generation.

## Features

- Uses Confluent Kafka Python client
- Generates randomized message keys and values
- Sinusoidal message rate pattern (550 + 450 * sin(t/60))
- Comprehensive logging
- JSON-formatted messages with timestamps
- Delivery callbacks for monitoring

## Building the Docker Image

```bash
docker build -t kafka-producer:latest -f Dockerfile.producer .
```

## Loading Image into kind Cluster

```bash
kind load docker-image kafka-producer:latest
```

## Deploying to kind

```bash
kubectl apply -f producer-deployment.yaml
```

## Viewing Logs

```bash
kubectl logs -f deployment/kafka-producer
```

## Configuration

The producer is configured via environment variables:

### Environment Variables

- **KAFKA_BROKERS**: Kafka bootstrap servers (default: `kafka:9092`)
- **KAFKA_TOPIC**: Topic name to produce messages to (default: `autoscale-demo`)

To change these settings, modify the environment variables in `producer-deployment.yaml`:

```yaml
env:
- name: KAFKA_BROKERS
  value: "my-kafka-service:9092"
- name: KAFKA_TOPIC
  value: "my-topic-name"
```

## Message Format

**Key:** `key-{1-10}`, `id-{1-10}`, or `partition-{1-10}` (randomized)

**Value:**
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

## Cleanup

```bash
kubectl delete -f producer-deployment.yaml
```
