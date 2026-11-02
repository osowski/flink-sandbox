"""Test helpers shared by the agent tests: a bus on in-memory Kafka clients, request builders, settings."""
import dataclasses
import json
from datetime import datetime, timezone

from dtm.common.a2a import dumps, send_message_request
from dtm.common.bus import AgentBus, BusConfig, Inbound
from dtm.common.config import AgentSettings
from dtm.common.envelope import Envelope
from dtm.mesh.context.table import ContextTable
from dtm.mesh.llm import LLMRequest, LLMResponse
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


CONTEXT_TIME = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def context_table(devices=(), customers=(), signals=()) -> ContextTable:
    """Seed a ContextTable through ``apply``, as the materializer does.

    Each device is a dict with ``device_id`` plus optional overrides of the device.context fields
    (``context_seq`` 7, ``trust_status`` TRUSTED, ``customer_id`` cust-001 by default); each customer a
    dict with ``customer_id``; each signal a dict with ``device_id`` and optional ``signal_id``,
    ``signal_type``, ``severity``, ``detail`` and ``source_event_type``.
    """
    table = ContextTable(100)
    for offset, device in enumerate(devices):
        value = {"envelope": {"device_id": device["device_id"]}, "customer_id": "cust-001", "context_seq": 7,
                 "trust_status": "TRUSTED", "platform": "android", "os_version": "14", "rooted": False,
                 "last_country": "US", "last_lat": 37.77, "last_lon": -122.42, "last_event_type": "LOGIN",
                 "last_event_time": CONTEXT_TIME, **{k: v for k, v in device.items() if k != "device_id"}}
        table.apply("device.context", 0, offset, {"device_id": device["device_id"]}, value, 1)
    for offset, customer in enumerate(customers):
        value = {"envelope": {"device_id": "dev-0001"}, "device_count": 1, "trusted_device_count": 1,
                 "known_countries": "US", "recent_anomaly_count": 0, "account_age_days": 400,
                 "last_country": "US", "last_event_time": CONTEXT_TIME,
                 **{k: v for k, v in customer.items() if k != "customer_id"}}
        table.apply("customer.context", 0, offset, {"customer_id": customer["customer_id"]}, value, 1)
    for offset, signal in enumerate(signals):
        value = {"envelope": {"device_id": signal["device_id"], "event_time": CONTEXT_TIME},
                 "signal_id": f"sig-{offset}", "signal_type": "IMPOSSIBLE_TRAVEL", "severity": "HIGH",
                 "detail": "d", "source_event_type": "LOGIN",
                 **{k: v for k, v in signal.items() if k != "device_id"}}
        table.apply("device.risk-signals", 0, offset, None, value, 1)
    return table


class ScriptedProvider:
    """An LLMProvider returning queued responses in order, recording the requests it received."""

    def __init__(self, *responses: LLMResponse):
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if not self._responses:
            raise AssertionError("ScriptedProvider has no response queued")
        return self._responses.pop(0)
