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

# Get configuration from environment variables
kafka_brokers = os.getenv('KAFKA_BROKERS', 'kafka:9092')
topic = os.getenv('KAFKA_TOPIC', 'autoscale-demo')

# Kafka configuration
conf = {
    'bootstrap.servers': kafka_brokers,
    'client.id': 'autoscale-producer',
    'acks': 'all',
    'retries': 3,
    'linger.ms': 10
}

# Create producer instance
producer = Producer(conf)

# Delivery callback
def delivery_report(err, msg):
    if err is not None:
        logger.error(f'Message delivery failed: {err}')
    else:
        logger.debug(f'Message delivered to {msg.topic()} [{msg.partition()}] at offset {msg.offset()}')

# Generate random data
def generate_random_data():
    """Generate randomized sample data"""
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

def generate_message_key():
    """Generate randomized message key"""
    key_prefix = random.choice(['key', 'id', 'partition'])
    key_suffix = random.randint(1, 10)
    return f"{key_prefix}-{key_suffix}"

logger.info(f"Starting Kafka producer for topic '{topic}'")
logger.info(f"Bootstrap servers: {conf['bootstrap.servers']}")

t = 0
total_messages_sent = 0

try:
    while True:
        # Calculate rate using sinusoidal pattern
        rate = 550 + 450 * math.sin(t/60)

        logger.info(f"Cycle {t}: Target rate = {int(rate)} messages/second")

        for i in range(int(rate)):
            # Generate random key and value
            key = generate_message_key()
            value = generate_random_data()

            # Serialize to JSON
            value_json = json.dumps(value)

            # Send message
            producer.produce(
                topic=topic,
                key=key.encode('utf-8'),
                value=value_json.encode('utf-8'),
                callback=delivery_report
            )

            total_messages_sent += 1

            # Log every 100th message to avoid spam
            if (i + 1) % 100 == 0:
                logger.info(f"Sent {i + 1}/{int(rate)} messages in this cycle")

        # Trigger delivery report callbacks
        producer.poll(0)

        logger.info(f"Completed cycle {t}: Sent {int(rate)} messages. Total: {total_messages_sent}")

        time.sleep(1)
        t += 1

except KeyboardInterrupt:
    logger.info("Shutting down producer...")
except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
finally:
    # Flush any remaining messages
    logger.info("Flushing remaining messages...")
    producer.flush()
    logger.info(f"Producer stopped. Total messages sent: {total_messages_sent}")
