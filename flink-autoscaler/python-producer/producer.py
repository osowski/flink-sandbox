import time
import math
import logging
import random
import string
import os
from confluent_kafka import Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import StringSerializer, SerializationContext, MessageField
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_avro_schema_string(schema_file):
    """Load Avro schema from file as string."""
    schema_path = os.path.join(os.path.dirname(__file__), 'schemas', schema_file)
    try:
        with open(schema_path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Schema file not found: {schema_path}")
        logger.error("Make sure schemas/ directory exists with .avsc files")
        raise
    except Exception as e:
        logger.error(f"Failed to load schema from {schema_path}: {e}")
        raise


def create_producer_config():
    """Create Kafka producer configuration with OAuth support."""

    # Get configuration from environment variables
    kafka_brokers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', os.getenv('KAFKA_BROKERS', 'kafka:9092'))
    schema_registry_url = os.getenv('SCHEMA_REGISTRY_URL', 'http://schemaregistry.kafka.svc.cluster.local:8081')

    # Kafka producer configuration
    kafka_config = {
        'bootstrap.servers': kafka_brokers,
        'client.id': 'autoscale-producer-avro',
        'acks': 'all',
        'retries': 3,
        'linger.ms': 10
    }

    # Schema Registry configuration
    schema_registry_conf = {
        'url': schema_registry_url
    }

    # Add Kafka security configuration if specified
    security_protocol = os.getenv('KAFKA_SECURITY_PROTOCOL')
    if security_protocol:
        kafka_config['security.protocol'] = security_protocol
        logger.info(f"Security protocol: {security_protocol}")

        # OAuth configuration for Kafka
        sasl_mechanism = os.getenv('KAFKA_SASL_MECHANISM')
        if sasl_mechanism == 'OAUTHBEARER':
            kafka_config['sasl.mechanism'] = 'OAUTHBEARER'
            kafka_config['sasl.oauthbearer.method'] = 'oidc'
            kafka_config['sasl.oauthbearer.client.id'] = os.getenv('KAFKA_OAUTH_CLIENT_ID')
            kafka_config['sasl.oauthbearer.client.secret'] = os.getenv('KAFKA_OAUTH_CLIENT_SECRET')
            kafka_config['sasl.oauthbearer.token.endpoint.url'] = os.getenv('KAFKA_OAUTH_TOKEN_ENDPOINT_URL')

            # Optional scope configuration
            oauth_scope = os.getenv('KAFKA_OAUTH_SCOPE')
            if oauth_scope:
                kafka_config['sasl.oauthbearer.scope'] = oauth_scope

            # Configure Schema Registry OAuth using bearer.auth.* properties
            # For on-prem/CP Schema Registry, set logical.cluster and identity.pool.id to empty strings
            schema_registry_conf['bearer.auth.credentials.source'] = 'OAUTHBEARER'
            schema_registry_conf['bearer.auth.issuer.endpoint.url'] = os.getenv('KAFKA_OAUTH_TOKEN_ENDPOINT_URL')
            schema_registry_conf['bearer.auth.client.id'] = os.getenv('KAFKA_OAUTH_CLIENT_ID')
            schema_registry_conf['bearer.auth.client.secret'] = os.getenv('KAFKA_OAUTH_CLIENT_SECRET')
            schema_registry_conf['bearer.auth.scope'] = os.getenv('KAFKA_OAUTH_SCOPE', '')  # Optional scope
            schema_registry_conf['bearer.auth.logical.cluster'] = ''  # Empty for on-prem/CP
            schema_registry_conf['bearer.auth.identity.pool.id'] = ''  # Empty for on-prem/CP

            logger.info(f"Schema Registry authentication: OAuth Bearer (client: {os.getenv('KAFKA_OAUTH_CLIENT_ID')})")
            logger.info(f"Kafka OAuth enabled for client: {kafka_config['sasl.oauthbearer.client.id']}")
            logger.info(f"Token endpoint: {kafka_config['sasl.oauthbearer.token.endpoint.url']}")

    logger.info(f"Schema Registry URL: {schema_registry_url}")

    return kafka_config, schema_registry_conf


def delivery_report(err, msg):
    """Callback for message delivery reports."""
    if err:
        if 'SASL' in str(err) or 'authentication' in str(err).lower():
            logger.error(f"Authentication error: {err}")
            logger.error("Check OAuth credentials and Keycloak connectivity")
        elif 'authorization' in str(err).lower() or 'not authorized' in str(err).lower():
            logger.error(f"Authorization error: {err}")
            logger.error("Check RBAC permissions for the service account")
        elif 'schema' in str(err).lower() or 'registry' in str(err).lower():
            logger.error(f"Schema Registry error: {err}")
            logger.error("Check Schema Registry connectivity and permissions")
        else:
            logger.error(f"Message delivery failed: {err}")
    else:
        # Log first successful message with value inspection
        if msg.offset() < 5:
            value_bytes = msg.value()
            if value_bytes:
                logger.info(f'Message delivered to {msg.topic()} [{msg.partition()}] offset {msg.offset()}, value starts with: {list(value_bytes[:10])}')
            else:
                logger.info(f'Message delivered to {msg.topic()} [{msg.partition()}] offset {msg.offset()}, value is None')
        logger.debug(f'Message delivered to {msg.topic()} [{msg.partition()}] at offset {msg.offset()}')


def generate_random_data():
    """Generate randomized sample data matching SensorEvent Avro schema"""
    data_types = ['temperature', 'pressure', 'humidity', 'speed', 'count']
    locations = ['sensor-A', 'sensor-B', 'sensor-C', 'sensor-D', 'sensor-E']

    return {
        'timestamp': datetime.utcnow().isoformat(),
        'type': random.choice(data_types),
        'location': random.choice(locations),
        'value': round(random.uniform(0, 100), 2),
        'status': random.choice(['normal', 'warning', 'critical']),
        'id': ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    }


def sensor_event_to_dict(sensor_event, ctx):
    """
    Serialization function for AvroSerializer.
    Converts dict to dict (pass-through since we're already generating dicts).
    Required by AvroSerializer to properly encode with schema ID.
    """
    return sensor_event


def generate_message_key():
    """Generate randomized message key"""
    key_prefix = random.choice(['key', 'id', 'partition'])
    key_suffix = random.randint(1, 10)
    return f"{key_prefix}-{key_suffix}"


def main():
    # Get configuration from environment
    topic = os.getenv('KAFKA_TOPIC', 'autoscale-demo')
    data_type = os.getenv('DATA_TYPE', 'autoscale')

    # Determine rate mode: fixed rate or sinusoidal
    message_rate_env = os.getenv('MESSAGE_RATE')
    use_fixed_rate = message_rate_env is not None
    fixed_rate = int(message_rate_env) if use_fixed_rate else 10

    logger.info("=" * 60)
    logger.info("Python Kafka Producer Starting (Avro Mode)")
    logger.info("=" * 60)
    logger.info(f"Topic: {topic}")
    logger.info(f"Data type: {data_type}")
    if use_fixed_rate:
        logger.info(f"Message rate: {fixed_rate} msg/sec (fixed)")
    else:
        logger.info("Message rate: sinusoidal pattern (550±450 msg/sec)")
    logger.info("=" * 60)

    # Load Avro schema
    try:
        value_schema_str = load_avro_schema_string('sensor-event-input-value.avsc')
        logger.info("Loaded Avro schema: SensorEvent (namespace: com.confluent.examples.sensors)")
    except Exception as e:
        logger.error(f"Failed to load Avro schema: {e}")
        logger.error("Cannot proceed without schema - exiting")
        return

    # Create producer with OAuth-aware configuration
    kafka_config, schema_registry_conf = create_producer_config()
    logger.info(f"Bootstrap servers: {kafka_config['bootstrap.servers']}")
    logger.info(f"Schema Registry: {schema_registry_conf['url']}")

    try:
        # Create Schema Registry client with built-in OAuth bearer authentication
        schema_registry_client = SchemaRegistryClient(schema_registry_conf)
        if 'bearer.auth.credentials.source' in schema_registry_conf:
            logger.info("Schema Registry client created with OAuth bearer auth (bearer.auth.* config)")
        else:
            logger.info("Schema Registry client created (no OAuth)")

        # Create Avro serializer for values with serialization function
        avro_serializer = AvroSerializer(
            schema_registry_client,
            value_schema_str,
            sensor_event_to_dict
        )

        # String serializer for keys
        string_serializer = StringSerializer('utf_8')

        # Create regular Producer (will manually serialize)
        producer = Producer(kafka_config)
        logger.info("Producer created successfully")
        logger.info("Schema will be auto-registered on first message (if not exists)")
    except Exception as e:
        logger.error(f"Failed to create producer: {e}")
        logger.error("Check Schema Registry connectivity and OAuth configuration")
        import traceback
        traceback.print_exc()
        return

    # Message generation loop
    t = 0
    total_messages_sent = 0

    try:
        while True:
            if use_fixed_rate:
                # Fixed rate mode
                rate = fixed_rate
                sleep_time = 1.0 / rate if rate > 0 else 1.0

                for i in range(rate):
                    value = generate_random_data()
                    key = str(total_messages_sent)

                    # Manually serialize key and value
                    key_bytes = string_serializer(key, SerializationContext(topic, MessageField.KEY))
                    value_bytes = avro_serializer(value, SerializationContext(topic, MessageField.VALUE))

                    # Debug: Log first 3 messages' serialized bytes
                    if total_messages_sent < 3:
                        logger.info(f"Message {total_messages_sent} - value_bytes type: {type(value_bytes)}, len: {len(value_bytes) if value_bytes else 0}")
                        logger.info(f"Message {total_messages_sent} - first 15 bytes: {list(value_bytes[:15])}")
                        logger.info(f"Message {total_messages_sent} - hex: {value_bytes[:15].hex()}")

                    # Produce message with serialized bytes
                    producer.produce(
                        topic=topic,
                        key=key_bytes,
                        value=value_bytes,
                        on_delivery=delivery_report
                    )

                    total_messages_sent += 1
                    producer.poll(0)
                    time.sleep(sleep_time)

                if total_messages_sent % 100 == 0:
                    logger.info(f"Produced {total_messages_sent} messages")

            else:
                # Sinusoidal rate mode
                rate = 550 + 450 * math.sin(t/60)
                logger.info(f"Cycle {t}: Target rate = {int(rate)} messages/second")

                for i in range(int(rate)):
                    key = generate_message_key()
                    value = generate_random_data()

                    # Manually serialize key and value
                    key_bytes = string_serializer(key, SerializationContext(topic, MessageField.KEY))
                    value_bytes = avro_serializer(value, SerializationContext(topic, MessageField.VALUE))

                    # Produce message with serialized bytes
                    producer.produce(
                        topic=topic,
                        key=key_bytes,
                        value=value_bytes,
                        on_delivery=delivery_report
                    )

                    total_messages_sent += 1

                    if (i + 1) % 100 == 0:
                        logger.info(f"Sent {i + 1}/{int(rate)} messages in this cycle")

                producer.poll(0)
                logger.info(f"Completed cycle {t}: Sent {int(rate)} messages. Total: {total_messages_sent}")
                time.sleep(1)
                t += 1

    except KeyboardInterrupt:
        logger.info("Shutting down producer (KeyboardInterrupt)...")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)

    finally:
        logger.info("Flushing remaining messages...")
        producer.flush(timeout=10)
        logger.info(f"Producer stopped. Total messages sent: {total_messages_sent}")


if __name__ == "__main__":
    main()
