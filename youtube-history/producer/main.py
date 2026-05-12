import argparse
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from confluent_kafka import Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import SerializationContext, MessageField

from credentials import fetch_confluent_credentials
from parse import extract_entries, make_key

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Single source of truth for the schema is terraform/schemas/raw_watch_event.avsc.
# The content loaded here must match that file verbatim — any divergence causes
# SR error 40403 at runtime because auto.register.schemas=False does an exact lookup.
_SCHEMA_PATH = Path(__file__).parent.parent / "terraform" / "schemas" / "raw_watch_event.avsc"
RAW_WATCH_EVENT_SCHEMA = _SCHEMA_PATH.read_text()


def _resolve_username(cli_arg: str | None, env_val: str | None) -> str:
    raw = cli_arg or env_val or ""
    return raw.strip() or "no-username-provided"


def build_producer_config(bootstrap_servers: str, creds: dict) -> dict:
    return {
        "bootstrap.servers": bootstrap_servers,
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "PLAIN",
        "sasl.username": creds["kafka_api_key"],
        "sasl.password": creds["kafka_api_secret"],
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "snappy",
    }


def produce_events(producer, avro_serializer, topic: str, input_file: str, username: str, flush_every: int = 500):
    entries = extract_entries(input_file)
    log.info("Producing %d events to %s", len(entries), topic)

    failed: list[str] = []

    def _delivery_report(err, msg):
        if err:
            log.error("Delivery failed for key %s: %s", msg.key(), err)
            failed.append(msg.key())

    for i, entry in enumerate(entries, 1):
        record = {
            "video_id":     entry["video_id"],
            "url":          entry["url"],
            "title":        entry["title"],
            "channel_name": entry["channel_name"],
            "channel_url":  entry["channel_url"],
            "watched_at":   entry["watched_at"],
            "username":     username,
            "produced_at":  int(time.time() * 1000),
        }
        key = make_key(entry["watched_at"], entry["video_id"])
        value = avro_serializer(record, SerializationContext(topic, MessageField.VALUE))

        producer.produce(
            topic=topic,
            key=key,
            value=value,
            on_delivery=_delivery_report,
        )

        if i % flush_every == 0:
            producer.flush()
            log.info("Flushed at %d events", i)

    producer.flush()

    if failed:
        raise RuntimeError(f"{len(failed)} message(s) failed delivery")

    log.info("Done. %d events produced.", len(entries))


def main():
    parser = argparse.ArgumentParser(description="Produce YouTube watch history events to Kafka")
    parser.add_argument("--username", default=None, help="YouTube account handle (e.g. @osowski)")
    args = parser.parse_args()

    bootstrap_servers = os.environ["BOOTSTRAP_SERVERS"]
    sr_url            = os.environ["SCHEMA_REGISTRY_URL"]
    input_file        = os.environ["INPUT_FILE"]
    sm_secret_path    = os.environ["SM_SECRET_PATH"]
    aws_region        = os.environ.get("AWS_REGION", "us-east-1")
    topic             = os.environ.get("KAFKA_TOPIC", "yt.raw.watch.events")
    username          = _resolve_username(args.username, os.environ.get("YOUTUBE_USERNAME"))

    log.info("Producing as username: %s", username)

    try:
        creds = fetch_confluent_credentials(sm_secret_path, aws_region)
    except Exception as exc:
        log.error("Failed to fetch credentials from Secrets Manager: %s", type(exc).__name__)
        raise SystemExit(1) from exc

    sr_client = SchemaRegistryClient({
        "url": sr_url,
        "basic.auth.user.info": f"{creds['sr_api_key']}:{creds['sr_api_secret']}",
    })
    avro_serializer = AvroSerializer(
        sr_client,
        RAW_WATCH_EVENT_SCHEMA,
        conf={"auto.register.schemas": False},
    )

    producer = Producer(build_producer_config(bootstrap_servers, creds))
    produce_events(producer, avro_serializer, topic, input_file, username)


if __name__ == "__main__":
    main()
