"""The simulator process: wires the engine to Kafka and runs its poll, tick, produce loop."""
import logging
import random
import signal
import threading
import time
from collections.abc import Mapping

from confluent_kafka import KafkaException
from confluent_kafka.serialization import MessageField, SerializationContext, SerializationError

from dtm.common.kafka import bootstrap_servers, consumer_config, producer_config, schema_registry_url
from dtm.simulator.engine import DEFAULT_RATE, Simulator
from dtm.simulator.pool import DevicePool

log = logging.getLogger(__name__)

DEVICE_EVENTS_TOPIC = "device.events"
POLICY_COMMANDS_TOPIC = "device.policy-commands"
CONTROL_TOPIC = "simulator.control"
POLICY_GROUP = "dtm.simulator.live"
CONTROL_GROUP = "dtm.simulator.control"
_POLL_TIMEOUT_S = 0.05
_FLUSH_TIMEOUT_S = 10.0


def _setting(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value else None


def _publish(producer, serializer, event: dict, on_delivery) -> bool:
    """Serialize and produce one device event; log and skip it if that fails. Return whether it was queued."""
    device_id = event["envelope"]["device_id"]
    try:
        value = serializer(event, SerializationContext(DEVICE_EVENTS_TOPIC, MessageField.VALUE))
        try:
            producer.produce(DEVICE_EVENTS_TOPIC, key=device_id.encode("utf-8"), value=value,
                             on_delivery=on_delivery)
        except BufferError:
            producer.poll(_POLL_TIMEOUT_S)
            producer.produce(DEVICE_EVENTS_TOPIC, key=device_id.encode("utf-8"), value=value,
                             on_delivery=on_delivery)
    except (SerializationError, BufferError, KafkaException):
        log.exception("skipping %s event for %s", event.get("event_type"), device_id)
        return False
    return True


def run(env: Mapping[str, str]) -> None:
    """Run the simulator until SIGINT or SIGTERM.

    ``DEVICE_CAP`` is required. ``SIM_RATE_PER_SEC``, ``SIM_SEED`` and ``DTM_VERSION`` are optional.
    """
    from confluent_kafka import Consumer, Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer

    cap = _setting(env, "DEVICE_CAP")
    if cap is None:
        raise ValueError("DEVICE_CAP is required")
    rate = _setting(env, "SIM_RATE_PER_SEC")
    seed = _setting(env, "SIM_SEED")
    simulator = Simulator(DevicePool(int(cap)), random.Random(int(seed)) if seed else random.Random(),
                          rate_per_sec=float(rate) if rate else DEFAULT_RATE,
                          version=_setting(env, "DTM_VERSION") or "dev")

    registry = SchemaRegistryClient({"url": schema_registry_url()})
    serializer = AvroSerializer(registry, schema_str=None,
                                conf={"auto.register.schemas": False, "use.latest.version": True})
    deserializer = AvroDeserializer(registry)
    bootstrap = bootstrap_servers()
    producer = Producer(producer_config(bootstrap))
    policy = Consumer(consumer_config(bootstrap, POLICY_GROUP, auto_offset_reset="latest",
                                      enable_auto_commit=True))
    control = Consumer(consumer_config(bootstrap, CONTROL_GROUP, auto_offset_reset="latest"))
    policy.subscribe([POLICY_COMMANDS_TOPIC])
    control.subscribe([CONTROL_TOPIC])

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def on_delivery(err, msg) -> None:
        if err is not None:
            log.error("delivery to %s failed: %s", msg.topic(), err)

    def consume(consumer, handle) -> None:
        msg = consumer.poll(_POLL_TIMEOUT_S)
        if msg is None:
            return
        if msg.error():
            log.warning("consume error on %s: %s", msg.topic(), msg.error())
            return
        try:
            record = deserializer(msg.value(), SerializationContext(msg.topic(), MessageField.VALUE))
            if record is None:
                return
            handle(record)
        except Exception:
            log.exception("skipping %s[%s]@%s", msg.topic(), msg.partition(), msg.offset())

    def now_ms() -> int:
        return time.time_ns() // 1_000_000

    log.info("simulator started: cap=%s", cap)
    try:
        while not stop.is_set():
            consume(policy, lambda cmd: simulator.on_policy_command(cmd, now_ms()))
            consume(control, simulator.on_control)
            for event in simulator.tick(now_ms()):
                _publish(producer, serializer, event, on_delivery)
            producer.poll(0)
    finally:
        producer.flush(_FLUSH_TIMEOUT_S)
        policy.close()
        control.close()
        log.info("simulator stopped")
