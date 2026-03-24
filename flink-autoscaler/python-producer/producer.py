import time
import math
import json
import logging
import random
import string
import os
from confluent_kafka import Producer
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_producer_config():
    """Create Kafka producer configuration with optional OAuth support."""

    # Get configuration from environment variables
    kafka_brokers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', os.getenv('KAFKA_BROKERS', 'kafka:9092'))

    # Base configuration
    config = {
        'bootstrap.servers': kafka_brokers,
        'client.id': 'autoscale-producer',
        'acks': 'all',
        'retries': 3,
        'linger.ms': 10
    }

    # Add security configuration if specified
    security_protocol = os.getenv('KAFKA_SECURITY_PROTOCOL')
    if security_protocol:
        config['security.protocol'] = security_protocol
        logger.info(f"Security protocol: {security_protocol}")

        # OAuth configuration
        sasl_mechanism = os.getenv('KAFKA_SASL_MECHANISM')
        if sasl_mechanism == 'OAUTHBEARER':
            config['sasl.mechanism'] = 'OAUTHBEARER'
            config['sasl.oauthbearer.method'] = 'oidc'  # CRITICAL: Use OIDC for client credentials
            config['sasl.oauthbearer.client.id'] = os.getenv('KAFKA_OAUTH_CLIENT_ID')
            config['sasl.oauthbearer.client.secret'] = os.getenv('KAFKA_OAUTH_CLIENT_SECRET')
            config['sasl.oauthbearer.token.endpoint.url'] = os.getenv('KAFKA_OAUTH_TOKEN_ENDPOINT_URL')

            # Optional scope configuration
            oauth_scope = os.getenv('KAFKA_OAUTH_SCOPE')
            if oauth_scope:
                config['sasl.oauthbearer.scope'] = oauth_scope

            logger.info(f"OAuth authentication enabled for client: {config['sasl.oauthbearer.client.id']}")
            logger.info(f"Token endpoint: {config['sasl.oauthbearer.token.endpoint.url']}")

    return config


def delivery_report(err, msg):
    """Callback for message delivery reports."""
    if err:
        if 'SASL' in str(err) or 'authentication' in str(err).lower():
            logger.error(f"Authentication error: {err}")
            logger.error("Check OAuth credentials and Keycloak connectivity")
            logger.error("Verify KAFKA_OAUTH_CLIENT_ID and KAFKA_OAUTH_CLIENT_SECRET environment variables")
        elif 'authorization' in str(err).lower() or 'not authorized' in str(err).lower():
            logger.error(f"Authorization error: {err}")
            logger.error("Check RBAC permissions for the service account")
        else:
            logger.error(f"Message delivery failed: {err}")
    else:
        logger.debug(f'Message delivered to {msg.topic()} [{msg.partition()}] at offset {msg.offset()}')


def generate_random_data():
    """Generate randomized sample data (original autoscaler format)"""
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


def generate_shapes_data(message_id):
    """Generate shapes data for Flink demo"""
    shapes = ["circle", "square", "triangle", "diamond", "trapezoid"]
    colors = ["red", "blue", "green", "yellow", "orange"]

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "message_id": message_id,
        "producer": "python-kafka-producer",
        "shape": random.choice(shapes),
        "size": random.randint(1, 100),
        "color": random.choice(colors)
    }


def generate_colors_data(message_id):
    """Generate colors data for Flink demo"""
    colors = ["red", "green", "blue", "yellow", "orange"]

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "message_id": message_id,
        "producer": "python-kafka-producer",
        "color": random.choice(colors),
        "intensity": random.randint(1, 100),
        "hue": random.randint(0, 360)
    }


def generate_message_key():
    """Generate randomized message key"""
    key_prefix = random.choice(['key', 'id', 'partition'])
    key_suffix = random.randint(1, 10)
    return f"{key_prefix}-{key_suffix}"


def main():
    # Get configuration from environment
    topic = os.getenv('KAFKA_TOPIC', 'autoscale-demo')
    data_type = os.getenv('DATA_TYPE', 'autoscale')  # 'autoscale', 'shapes', 'colors'

    # Determine rate mode: fixed rate or sinusoidal
    message_rate_env = os.getenv('MESSAGE_RATE')
    use_fixed_rate = message_rate_env is not None
    fixed_rate = int(message_rate_env) if use_fixed_rate else 10

    logger.info("=" * 60)
    logger.info("Python Kafka Producer Starting")
    logger.info("=" * 60)
    logger.info(f"Topic: {topic}")
    logger.info(f"Data type: {data_type}")
    if use_fixed_rate:
        logger.info(f"Message rate: {fixed_rate} msg/sec (fixed)")
    else:
        logger.info("Message rate: sinusoidal pattern (550±450 msg/sec)")
    logger.info("=" * 60)

    # Create producer with OAuth-aware configuration
    config = create_producer_config()
    logger.info(f"Bootstrap servers: {config['bootstrap.servers']}")

    try:
        producer = Producer(config)
        logger.info("Producer created successfully")
    except Exception as e:
        logger.error(f"Failed to create producer: {e}")
        return

    # Message generation loop
    t = 0
    total_messages_sent = 0

    try:
        while True:
            if use_fixed_rate:
                # Fixed rate mode (for shapes/colors demos)
                rate = fixed_rate
                sleep_time = 1.0 / rate if rate > 0 else 1.0

                for i in range(rate):
                    # Generate message based on data type
                    if data_type == 'shapes':
                        value = generate_shapes_data(total_messages_sent)
                    elif data_type == 'colors':
                        value = generate_colors_data(total_messages_sent)
                    else:
                        value = generate_random_data()

                    key = str(total_messages_sent)

                    # Send message
                    producer.produce(
                        topic=topic,
                        key=key.encode('utf-8'),
                        value=json.dumps(value).encode('utf-8'),
                        callback=delivery_report
                    )

                    total_messages_sent += 1

                    # Poll for delivery reports
                    producer.poll(0)

                    # Rate limiting
                    time.sleep(sleep_time)

                # Log progress every 100 messages
                if total_messages_sent % 100 == 0:
                    logger.info(f"Produced {total_messages_sent} messages")

            else:
                # Sinusoidal rate mode (for autoscaler demos)
                rate = 550 + 450 * math.sin(t/60)

                logger.info(f"Cycle {t}: Target rate = {int(rate)} messages/second")

                for i in range(int(rate)):
                    key = generate_message_key()
                    value = generate_random_data()

                    # Send message
                    producer.produce(
                        topic=topic,
                        key=key.encode('utf-8'),
                        value=json.dumps(value).encode('utf-8'),
                        callback=delivery_report
                    )

                    total_messages_sent += 1

                    # Log every 100th message
                    if (i + 1) % 100 == 0:
                        logger.info(f"Sent {i + 1}/{int(rate)} messages in this cycle")

                # Poll for delivery reports
                producer.poll(0)

                logger.info(f"Completed cycle {t}: Sent {int(rate)} messages. Total: {total_messages_sent}")

                time.sleep(1)
                t += 1

    except KeyboardInterrupt:
        logger.info("Shutting down producer (KeyboardInterrupt)...")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)

    finally:
        # Flush remaining messages
        logger.info("Flushing remaining messages...")
        remaining = producer.flush(timeout=10)
        if remaining > 0:
            logger.warning(f"{remaining} messages were not delivered")
        logger.info(f"Producer stopped. Total messages sent: {total_messages_sent}")


if __name__ == "__main__":
    main()
