"""Test helpers shared by the agent tests: a bus on in-memory Kafka clients, request builders, settings."""
import dataclasses
import json

from dtm.common.a2a import dumps, send_message_request
from dtm.common.bus import AgentBus, BusConfig, Inbound
from dtm.common.config import AgentSettings
from dtm.common.envelope import Envelope
from fakes import FakeConsumer, FakeProducer

EVENT_TIME = 1793323937770
ROUTER_ENVELOPE = Envelope("router-0", "corr-1", None, "flink:dtm-a2a-task-router", "1.0", "n/a", EVENT_TIME,
                           "dev-0001", "cust-001", 7, 7)
RISK_REQUEST = {"deviceId": "dev-0001", "customerId": "cust-001", "requiredContextSeq": 7,
                "fingerprintSummary": "Pixel 8, Android 14, rooted=false"}


def make_bus(agent: str) -> AgentBus:
    return AgentBus(agent, BusConfig(), producer=FakeProducer(), consumer=FakeConsumer(),
                    avro_serialize=lambda topic, value: json.dumps(value).encode())


def produced(bus: AgentBus, topic: str) -> list[tuple[str, dict]]:
    """Return ``(key, value)`` for each record produced to the logical topic, decoding JSON for A2A and Avro alike."""
    return [(r.key.decode(), json.loads(r.value))
            for r in bus._producer.records if r.topic == bus.topic(topic)]


def request_inbound(topic: str, data: dict, *, task_id: str | None = None, mode: str = "LIVE",
                    replay_run_id: str | None = None, offset: int = 0) -> Inbound:
    """Build a record as the router writes it: a SendMessage request carrying a router-style envelope."""
    envelope = dataclasses.replace(ROUTER_ENVELOPE, message_id=f"router-{offset}", mode=mode,
                                   replay_run_id=replay_run_id)
    request = send_message_request(envelope, data, "Assess the device", task_id=task_id)
    return Inbound(topic, 0, offset, "dev-0001", {}, json.loads(dumps(request)), 0)


def settings(name: str, **over) -> AgentSettings:
    base = AgentSettings(name, True, 1, None, None, {}, "cfg-1", {})
    return dataclasses.replace(base, **over)
