"""``dtm-ctl``: the presenter's command line for the simulator's ``simulator.control`` topic."""
import argparse
import sys
import time
import uuid

from dtm.common.envelope import Envelope
from dtm.common.kafka import bootstrap_servers, producer_config, schema_registry_url
from dtm.simulator.engine import SCENARIOS

CONTROL_TOPIC = "simulator.control"
NO_SCENARIO_KEY = "control"
CLI_AGENT_ID = "cli:dtm-ctl"
_FLUSH_TIMEOUT_S = 10.0


def control_record(command: str | None, *, scenario: str | None = None, rate_per_sec: float | None = None,
                   count: int | None = None, now_ms: int) -> dict:
    """Return an Avro ``SimulatorControl`` record."""
    envelope = Envelope(
        message_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        causation_id=None,
        agent_id=CLI_AGENT_ID,
        agent_version="dev",
        config_version="n/a",
        event_time=now_ms,
        device_id="n/a",
        customer_id=None,
        device_seq=None,
        required_context_seq=None,
    )
    return {"envelope": envelope.to_avro(), "scenario": scenario, "command": command,
            "rate_per_sec": rate_per_sec, "count": count}


def _non_negative_rate(text: str) -> float:
    rate = float(text)
    if rate < 0:
        raise argparse.ArgumentTypeError("rate must not be negative")
    return rate


def _positive_count(text: str) -> int:
    count = int(text)
    if count < 1:
        raise argparse.ArgumentTypeError("count must be at least 1")
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dtm-ctl", description="Control the device simulator.")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("start", help="resume paced enrolments")
    sub.add_parser("stop", help="pause paced enrolments")
    rate = sub.add_parser("rate", help="set enrolments per second (0 pauses emission)")
    rate.add_argument("rate", type=_non_negative_rate)
    inject = sub.add_parser("inject", help="queue a scenario ahead of the paced mix")
    inject.add_argument("scenario", choices=SCENARIOS)
    inject.add_argument("--count", type=_positive_count, default=None)
    return parser


def parse(argv: list[str]) -> tuple[str | None, str | None, float | None, int | None]:
    """Return ``(command, scenario, rate_per_sec, count)`` for a command line."""
    args = _parser().parse_args(argv)
    if args.verb == "rate":
        return None, None, args.rate, None
    if args.verb == "inject":
        return "INJECT", args.scenario, None, args.count
    return args.verb.upper(), None, None, None


def _send(record: dict, key: str) -> None:
    from confluent_kafka import Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    serializer = AvroSerializer(SchemaRegistryClient({"url": schema_registry_url()}), schema_str=None,
                                conf={"auto.register.schemas": False, "use.latest.version": True})
    failures: list = []

    def delivered(err, _msg) -> None:
        if err is not None:
            failures.append(err)

    producer = Producer(producer_config(bootstrap_servers()))
    producer.produce(CONTROL_TOPIC, key=key.encode("utf-8"),
                     value=serializer(record, SerializationContext(CONTROL_TOPIC, MessageField.VALUE)),
                     on_delivery=delivered)
    if producer.flush(_FLUSH_TIMEOUT_S) or failures:
        raise RuntimeError(f"control record was not delivered: {failures[0] if failures else 'flush timed out'}")


def main(argv: list[str]) -> int:
    command, scenario, rate, count = parse(argv)
    record = control_record(command, scenario=scenario, rate_per_sec=rate, count=count,
                            now_ms=time.time_ns() // 1_000_000)
    try:
        _send(record, scenario or NO_SCENARIO_KEY)
    except Exception as exc:
        print(f"dtm-ctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
