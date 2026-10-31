import copy
import json
import unittest

import confluent_kafka
from confluent_kafka import KafkaError, KafkaException
from prometheus_client import REGISTRY

from dtm.common.a2a import (A2AValidationError, dumps, send_message_request, status_update)
from dtm.common.bus import AgentBus, BusConfig, CommitFailed, ConsumeNotAllowed, DeliveryFailed, Inbound
from dtm.common.envelope import Envelope
from dtm.common.kafka import bootstrap_servers, consumer_config, producer_config, schema_registry_url
from fakes import FakeConsumer, FakeProducer, msg

EVENT_TIME = 1793323937770
ENV = Envelope("m-1", "c-1", None, "agent:risk", "dev", "abc123", EVENT_TIME, "dev-0001", "cust-001", 5, 5)
REQ = send_message_request(ENV, {"taskType": "DECIDE_ENROLMENT", "deviceId": "dev-0001"}, "Decide enrolment")
STATUS = status_update("m-1", "t-1", "c-1", "TASK_STATE_WORKING", ENV, "2026-10-30T01:32:17.770Z")
CMD = {"envelope": ENV.to_avro(), "device_id": "dev-0001", "command": "ALLOW"}


REPLAY = {"mode": "replay", "replayRunId": "r1"}
REPLAY_CONFIG = dict(mode="replay", replay_run_id="r1")


def bus(agent, config=None):
    return AgentBus(agent, config or BusConfig(), producer=FakeProducer(), consumer=FakeConsumer(),
                    avro_serialize=lambda topic, value: json.dumps(value).encode(),
                    clock=lambda: (EVENT_TIME + 40) / 1000)


def inbound(topic="agent.tasks.risk", offset=0, headers=None, value=None):
    return Inbound(topic, 0, offset, "dev-0001", headers or {}, value, 0)


_SKIPPED = {b"[1]", b"5", b'"s"', b"\xff\xfe"}


def hop_latency_sum(agent, topic):
    return REGISTRY.get_sample_value("dtm_hop_latency_ms_sum", {"agent": agent, "topic": topic}) or 0.0


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = BusConfig()
        self.assertEqual((c.mode, c.start_position, c.start_value, c.topic_prefix, c.policy_commands,
                          c.replay_run_id), ("live", "committed", None, "", "enabled", None))
        self.assertEqual(c.allowed_consume, ("agent.tasks.<self>", "agent.task-events", "agent.cards"))

    def test_from_dict_reads_camel_case(self):
        c = BusConfig.from_dict({"mode": "replay", "startPosition": "timestamp", "startValue": 5,
                                 "topicPrefix": "replay.r1.", "policyCommands": "redirect",
                                 "allowedConsume": ["agent.cards"], "replayRunId": "r1"})
        self.assertEqual((c.mode, c.start_position, c.start_value, c.topic_prefix, c.policy_commands,
                          c.allowed_consume, c.replay_run_id),
                         ("replay", "timestamp", 5, "replay.r1.", "redirect", ("agent.cards",), "r1"))

    def test_from_dict_empty_is_defaults(self):
        self.assertEqual(BusConfig.from_dict({}), BusConfig())

    def test_redirect_requires_prefix(self):
        with self.assertRaises(ValueError):
            BusConfig.from_dict({"policyCommands": "redirect", "topicPrefix": ""})

    def test_offset_and_timestamp_require_start_value(self):
        for position in ("offset", "timestamp"):
            with self.subTest(position=position), self.assertRaises(ValueError):
                BusConfig.from_dict({**REPLAY, "startPosition": position})

    def test_rewinding_positions_require_replay_with_run_id(self):
        for position, value in (("earliest", None), ("offset", {0: 1}), ("timestamp", 5)):
            for extra in ({}, {"mode": "replay"}, {"replayRunId": "r1"}):
                with self.subTest(position=position, extra=extra), self.assertRaises(ValueError):
                    BusConfig.from_dict({"startPosition": position, "startValue": value, **extra})
            BusConfig.from_dict({**REPLAY, "startPosition": position, "startValue": value})

    def test_offset_start_value_is_a_partition_mapping_with_int_keys(self):
        c = BusConfig.from_dict({**REPLAY, "startPosition": "offset", "startValue": {"0": 120, 3: 98}})
        self.assertEqual(c.start_value, {0: 120, 3: 98})

    def test_offset_start_value_rejects_non_mappings_and_bad_entries(self):
        for value in (42, [1, 2], {}, {"x": 1}, {0: -1}, {0: 1.5}, {-1: 3}, {0: True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BusConfig.from_dict({**REPLAY, "startPosition": "offset", "startValue": value})

    def test_timestamp_start_value_is_a_single_int(self):
        for value in ({0: 1}, "5", 1.5, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BusConfig.from_dict({**REPLAY, "startPosition": "timestamp", "startValue": value})

    def test_enums_validated(self):
        for key, value in (("mode", "batch"), ("startPosition", "latest"), ("policyCommands", "off")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                BusConfig.from_dict({key: value})


class KafkaConfigTest(unittest.TestCase):
    def test_every_consumer_config_reads_committed(self):
        self.assertEqual(consumer_config("b:9", "g")["isolation.level"], "read_committed")

    def test_consumer_config_values(self):
        c = consumer_config("b:9", "g")
        self.assertEqual({k: c[k] for k in ("bootstrap.servers", "group.id", "auto.offset.reset",
                                            "enable.auto.commit", "fetch.min.bytes", "fetch.wait.max.ms")},
                         {"bootstrap.servers": "b:9", "group.id": "g", "auto.offset.reset": "latest",
                          "enable.auto.commit": False, "fetch.min.bytes": 1, "fetch.wait.max.ms": 50})
        c = consumer_config("b:9", "g", auto_offset_reset="earliest", enable_auto_commit=True)
        self.assertEqual((c["auto.offset.reset"], c["enable.auto.commit"]), ("earliest", True))

    def test_producer_config_low_latency(self):
        self.assertEqual({k: producer_config("b:9")[k] for k in ("linger.ms", "acks", "enable.idempotence",
                                                                 "compression.type")},
                         {"linger.ms": 5, "acks": "all", "enable.idempotence": True, "compression.type": "lz4"})
        self.assertEqual(producer_config("b:9")["bootstrap.servers"], "b:9")

    def test_endpoints_default_to_in_cluster(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bootstrap_servers(), "kafka.kafka.svc.cluster.local:9071")
            self.assertEqual(schema_registry_url(), "http://schemaregistry.kafka.svc.cluster.local:8081")
        with mock.patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": "x:1", "SCHEMA_REGISTRY_URL": "http://sr:2"}):
            self.assertEqual((bootstrap_servers(), schema_registry_url()), ("x:1", "http://sr:2"))


class SubscribeTest(unittest.TestCase):
    def test_group_id_template(self):
        self.assertEqual(bus("risk").group_id, "dtm.risk.live")
        self.assertEqual(bus("risk", BusConfig(mode="replay", replay_run_id="r1")).group_id,
                         "dtm.risk.replay.r1")

    def test_subscribe_outside_allow_list_refused(self):
        with self.assertRaises(ConsumeNotAllowed):
            bus("risk").subscribe(["device.context"])
        with self.assertRaises(ConsumeNotAllowed):
            bus("risk").subscribe(["agent.tasks.enrolment"])

    def test_refused_subscribe_does_not_subscribe(self):
        b = bus("risk")
        with self.assertRaises(ConsumeNotAllowed):
            b.subscribe(["agent.cards", "device.context"])
        self.assertIsNone(b._consumer.subscribed)

    def test_subscribe_allowed_topics(self):
        b = bus("risk")
        b.subscribe(["agent.tasks.risk", "agent.task-events", "agent.cards"])
        self.assertEqual(b._consumer.subscribed, ["agent.tasks.risk", "agent.task-events", "agent.cards"])

    def test_custom_allow_list(self):
        b = bus("compliance", BusConfig(allowed_consume=("agent.task-events",)))
        b.subscribe(["agent.task-events"])
        with self.assertRaises(ConsumeNotAllowed):
            b.subscribe(["agent.tasks.compliance"])

    def test_subscribe_applies_prefix(self):
        b = bus("risk", BusConfig(topic_prefix="replay.r1."))
        b.subscribe(["agent.tasks.risk"])
        self.assertEqual(b._consumer.subscribed, ["replay.r1.agent.tasks.risk"])

    def test_committed_start_leaves_offsets_to_the_group(self):
        b = bus("risk")
        b.subscribe(["agent.tasks.risk"])
        parts = b._consumer.trigger_assign([("agent.tasks.risk", 0)])
        self.assertEqual(parts[0].offset, confluent_kafka.OFFSET_INVALID)
        self.assertEqual(b._consumer.assigned, parts)

    def test_start_position_earliest_resets_assignment(self):
        b = bus("risk", BusConfig(start_position="earliest", **REPLAY_CONFIG))
        b.subscribe(["agent.tasks.risk"])
        parts = b._consumer.trigger_assign([("agent.tasks.risk", 0)])
        self.assertEqual(parts[0].offset, confluent_kafka.OFFSET_BEGINNING)
        self.assertEqual(b._consumer.assigned, parts)

    def test_committed_partition_is_not_rewound(self):
        for position, value in (("earliest", None), ("offset", {0: 42, 1: 42}), ("timestamp", 1000)):
            with self.subTest(position=position):
                b = bus("risk", BusConfig(start_position=position, start_value=value, **REPLAY_CONFIG))
                b._consumer.committed_offsets[("agent.tasks.risk", 0)] = 5
                b.subscribe(["agent.tasks.risk"])
                parts = b._consumer.trigger_assign([("agent.tasks.risk", 0), ("agent.tasks.risk", 1)])
                self.assertEqual(parts[0].offset, confluent_kafka.OFFSET_INVALID)
                self.assertNotEqual(parts[1].offset, confluent_kafka.OFFSET_INVALID)
                self.assertEqual(b._consumer.assigned, parts)

    def test_committed_lookup_error_raises(self):
        b = bus("risk", BusConfig(start_position="earliest", **REPLAY_CONFIG))
        b._consumer.committed_errors[("agent.tasks.risk", 0)] = "broker down"
        b.subscribe(["agent.tasks.risk"])
        with self.assertRaises(RuntimeError):
            b._consumer.trigger_assign([("agent.tasks.risk", 0)])

    def test_start_position_offset_is_per_partition(self):
        b = bus("risk", BusConfig(start_position="offset", start_value={"0": 42, "2": 7}, **REPLAY_CONFIG))
        b.subscribe(["agent.tasks.risk"])
        parts = b._consumer.trigger_assign([("agent.tasks.risk", p) for p in range(3)])
        self.assertEqual([p.offset for p in parts], [42, confluent_kafka.OFFSET_BEGINNING, 7])
        self.assertEqual(b._consumer.assigned, parts)

    def test_start_position_timestamp_resolves_offsets(self):
        b = bus("risk", BusConfig(start_position="timestamp", start_value=1000, **REPLAY_CONFIG))
        b._consumer.time_offsets[("agent.tasks.risk", 0, 1000)] = 17
        b.subscribe(["agent.tasks.risk"])
        parts = b._consumer.trigger_assign([("agent.tasks.risk", 0), ("agent.tasks.risk", 1)])
        self.assertEqual([(p.partition, p.offset) for p in b._consumer.assigned],
                         [(0, 17), (1, confluent_kafka.OFFSET_END)])
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(t is not None for t in b._consumer.lookup_timeouts))

    def test_timestamp_resolution_error_raises(self):
        b = bus("risk", BusConfig(start_position="timestamp", start_value=1000, **REPLAY_CONFIG))
        b._consumer.time_errors[("agent.tasks.risk", 0)] = "timed out"
        b.subscribe(["agent.tasks.risk"])
        with self.assertRaises(RuntimeError):
            b._consumer.trigger_assign([("agent.tasks.risk", 0)])


class SendTest(unittest.TestCase):
    def test_send_sets_headers_and_key(self):
        b = bus("risk")
        b.send("enrolment", REQ, key="dev-0001")
        rec = b._producer.records[-1]
        self.assertEqual(rec.topic, "agent.tasks.enrolment")
        self.assertEqual(rec.key, b"dev-0001")
        self.assertEqual(rec.headers, {"a2a-reply-topic": "agent.task-events", "a2a-message-id": REQ["id"],
                                       "a2a-correlation-id": "c-1", "dtm-agent-id": "agent:risk",
                                       "dtm-mode": "LIVE"})

    def test_send_keeps_integers(self):
        b = bus("risk")
        b.send("enrolment", REQ, key="dev-0001")
        self.assertIn(b'"eventTime":1793323937770', b._producer.records[-1].value)
        self.assertNotIn(b'1793323937770.0', b._producer.records[-1].value)

    def test_send_value_is_the_callers_record(self):
        b = bus("risk")
        b.send("enrolment", REQ, key="dev-0001")
        self.assertEqual(b._producer.records[-1].value, dumps(REQ))

    def test_send_invalid_record_not_produced(self):
        b = bus("risk")
        bad = copy.deepcopy(REQ)
        del bad["params"]["message"]["parts"]
        with self.assertRaises(A2AValidationError):
            b.send("enrolment", bad, key="dev-0001")
        self.assertEqual(b._producer.records, [])

    def test_send_applies_prefix_to_topic_and_reply_header(self):
        b = bus("risk", BusConfig(topic_prefix="replay.r1."))
        b.send("enrolment", REQ, key="dev-0001")
        rec = b._producer.records[-1]
        self.assertEqual(rec.topic, "replay.r1.agent.tasks.enrolment")
        self.assertEqual(rec.headers["a2a-reply-topic"], "replay.r1.agent.task-events")

    def test_reply_uses_inbound_reply_topic(self):
        b = bus("risk", BusConfig(topic_prefix="replay.r1."))
        b.reply(inbound(headers={"a2a-reply-topic": "replay.r1.agent.task-events"}), STATUS, key="dev-0001")
        self.assertEqual(b._producer.records[-1].topic, "replay.r1.agent.task-events")

    def test_reply_ignores_header_outside_the_prefix(self):
        b = bus("risk", BusConfig(topic_prefix="replay.r1."))
        with self.assertLogs("dtm.common.bus", "WARNING"):
            b.reply(inbound(headers={"a2a-reply-topic": "agent.task-events"}), STATUS, key="dev-0001")
        self.assertEqual(b._producer.records[-1].topic, "replay.r1.agent.task-events")

    def test_reply_without_inbound_uses_shared_topic(self):
        b = bus("risk")
        b.reply(None, STATUS, key="dev-0001")
        rec = b._producer.records[-1]
        self.assertEqual((rec.topic, rec.key, rec.value), ("agent.task-events", b"dev-0001", dumps(STATUS)))
        self.assertEqual(rec.headers["a2a-correlation-id"], "c-1")
        self.assertEqual(rec.headers["a2a-message-id"], "m-1")
        self.assertEqual(rec.headers["dtm-agent-id"], "agent:risk")

    def test_reply_invalid_record_not_produced(self):
        b = bus("risk")
        with self.assertRaises(A2AValidationError):
            b.reply(None, REQ, key="dev-0001")
        self.assertEqual(b._producer.records, [])

    def test_publish_card(self):
        from dtm.common.a2a import agent_card
        skills = [{"id": "s", "name": "S", "description": "d", "tags": ["t"]}]
        card = agent_card("risk", "Scores risk", skills, "abc123", "b:9071")
        b = bus("risk")
        b.publish_card(card)
        rec = b._producer.records[-1]
        self.assertEqual((rec.topic, rec.key, rec.value), ("agent.cards", b"risk", dumps(card)))

    def test_publish_invalid_card_not_produced(self):
        b = bus("risk")
        with self.assertRaises(A2AValidationError):
            b.publish_card({"name": "risk"})
        self.assertEqual(b._producer.records, [])

    def test_tombstone_card(self):
        b = bus("risk")
        b.tombstone_card("risk")
        rec = b._producer.records[-1]
        self.assertEqual((rec.topic, rec.key, rec.value), ("agent.cards", b"risk", None))


class AvroTest(unittest.TestCase):
    def test_policy_commands_modes(self):
        for mode, expected in (("enabled", {"device.policy-commands"}),
                               ("redirect", {"replay.r1.device.policy-commands"}), ("disabled", set())):
            with self.subTest(mode=mode):
                b = bus("enrolment", BusConfig(topic_prefix="replay.r1.", policy_commands=mode))
                b.produce_avro("device.policy-commands", CMD, key="dev-0001")
                self.assertEqual(b.produced_topics(), expected)
                self.assertEqual(len(b._producer.records), len(expected))

    def test_produce_avro_serializes_value_and_key(self):
        b = bus("enrolment")
        b.produce_avro("device.policy-commands", CMD, key="dev-0001")
        rec = b._producer.records[-1]
        self.assertEqual((rec.key, rec.value), (b"dev-0001", json.dumps(CMD).encode()))

    def test_other_avro_topics_follow_the_prefix(self):
        b = bus("enrolment", BusConfig(topic_prefix="replay.r1."))
        b.produce_avro("audit.log", {"x": 1}, key="dev-0001")
        self.assertEqual(b.produced_topics(), {"replay.r1.audit.log"})

    def test_serializer_receives_the_logical_topic(self):
        seen = []
        b = AgentBus("enrolment", BusConfig(topic_prefix="replay.r1.", policy_commands="redirect"),
                     producer=FakeProducer(), consumer=FakeConsumer(),
                     avro_serialize=lambda topic, value: seen.append(topic) or b"")
        b.produce_avro("device.policy-commands", CMD, key="dev-0001")
        self.assertEqual(seen, ["device.policy-commands"])

    def test_produced_topics_includes_a2a_traffic(self):
        b = bus("risk")
        b.send("enrolment", REQ, key="dev-0001")
        b.tombstone_card("risk")
        self.assertEqual(b.produced_topics(), {"agent.tasks.enrolment", "agent.cards"})


class CommitTest(unittest.TestCase):
    def test_commit_after_flush_and_raises_on_delivery_error(self):
        b = bus("risk")
        b._producer.fail_next_delivery = True
        b.send("enrolment", REQ, key="dev-0001")
        with self.assertRaises(DeliveryFailed):
            b.commit(inbound(offset=7))
        self.assertEqual(b._consumer.committed, [])
        b.send("enrolment", REQ, key="dev-0001")
        b.commit(inbound(offset=7))
        self.assertEqual(b._consumer.committed[-1], ("agent.tasks.risk", 0, 8))

    def test_commit_raises_when_broker_rejects_the_commit(self):
        b = bus("risk")
        b._consumer.commit_error = "rebalance in progress"
        with self.assertRaises(CommitFailed):
            b.commit(inbound(offset=3))

    def test_commit_maps_a_consumer_exception_to_commit_failed(self):
        for code in (KafkaError.REBALANCE_IN_PROGRESS, KafkaError.ILLEGAL_GENERATION, KafkaError.UNKNOWN_MEMBER_ID):
            with self.subTest(code=code):
                b = bus("risk")
                b._consumer.commit_raises = KafkaException(KafkaError(code))
                with self.assertRaises(CommitFailed):
                    b.commit(inbound(offset=3))
                self.assertEqual(b._consumer.committed, [])

    def test_commit_flushes_the_producer_first(self):
        b = bus("risk")
        b.commit(inbound(offset=1))
        self.assertEqual(b._producer.flushed, 1)

    def test_commit_refuses_when_messages_remain_queued(self):
        b = bus("risk")
        b._producer.flush = lambda timeout=None: 2
        with self.assertRaises(DeliveryFailed):
            b.commit(inbound(offset=1))
        self.assertEqual(b._consumer.committed, [])


class EnsureDeliveredTest(unittest.TestCase):
    def test_flushes_and_returns_when_everything_was_delivered(self):
        b = bus("risk")
        b.tombstone_card("risk")
        b.ensure_delivered(2.0)
        self.assertEqual(b._producer.flushed, 1)

    def test_raises_on_a_delivery_error_and_clears_it(self):
        b = bus("risk")
        b._producer.fail_next_delivery = True
        b.tombstone_card("risk")
        with self.assertRaises(DeliveryFailed):
            b.ensure_delivered(2.0)
        b.commit(inbound(offset=1))
        self.assertEqual(b._consumer.committed[-1], ("agent.tasks.risk", 0, 2))

    def test_raises_when_messages_remain_queued(self):
        b = bus("risk")
        b._producer.flush = lambda timeout=None: 1
        with self.assertRaises(DeliveryFailed):
            b.ensure_delivered(0.5)


class ReconfigureTest(unittest.TestCase):
    def test_applies_a_policy_commands_change_in_place(self):
        b = bus("risk")
        b.reconfigure(BusConfig(policy_commands="disabled"))
        self.assertEqual(b.config.policy_commands, "disabled")
        b.produce_avro("device.policy-commands", CMD, "dev-0001")
        self.assertEqual(b._producer.records, [])

    def test_refuses_any_other_change(self):
        for name, changed in (("topicPrefix", BusConfig(topic_prefix="x.")),
                              ("mode", BusConfig(mode="replay")),
                              ("allowedConsume", BusConfig(allowed_consume=("agent.cards",))),
                              ("startPosition", BusConfig(start_position="earliest", mode="replay",
                                                          replay_run_id="r1")),
                              ("replayRunId", BusConfig(replay_run_id="r1"))):
            with self.subTest(name):
                b = bus("risk")
                with self.assertRaises(ValueError):
                    b.reconfigure(changed)
                self.assertEqual(b.config, BusConfig())

    def test_refuses_a_combined_change_without_applying_either(self):
        b = bus("risk")
        with self.assertRaises(ValueError):
            b.reconfigure(BusConfig(policy_commands="disabled", topic_prefix="x."))
        self.assertEqual(b.config, BusConfig())


class PollTest(unittest.TestCase):
    def test_poll_decodes_json_and_tombstones(self):
        b = bus("compliance")
        b._consumer.queue([msg("agent.task-events", value=dumps(STATUS)), msg("agent.cards", value=None)])
        got = b.poll(0.1)
        self.assertEqual(got[0].value, STATUS)
        self.assertIsNone(got[1].value)

    def test_poll_maps_message_fields(self):
        b = bus("risk")
        b._consumer.queue([msg("agent.tasks.risk", value=dumps(REQ), key="dev-0001",
                               headers={"a2a-reply-topic": "agent.task-events"}, partition=2, offset=9,
                               timestamp_ms=123)])
        self.assertEqual(b.poll(0.1), [Inbound("agent.tasks.risk", 2, 9, "dev-0001",
                                               {"a2a-reply-topic": "agent.task-events"}, REQ, 123)])

    def test_poll_without_key_or_headers(self):
        b = bus("risk")
        b._consumer.queue([msg("agent.cards", value=None)])
        got = b.poll(0.1)[0]
        self.assertEqual((got.key, got.headers), (None, {}))

    def test_poll_skips_errored_messages(self):
        b = bus("risk")
        b._consumer.queue([msg("agent.cards", value=None, error="boom"), msg("agent.cards", value=None, offset=1)])
        with self.assertLogs("dtm.common.bus", "WARNING"):
            self.assertEqual([i.offset for i in b.poll(0.1)], [1])

    def test_poll_skips_undecodable_values(self):
        b = bus("risk")
        b._consumer.queue([msg("agent.cards", value=b"not json"), msg("agent.cards", value=None, offset=1)])
        with self.assertLogs("dtm.common.bus", "WARNING"):
            self.assertEqual([i.offset for i in b.poll(0.1)], [1])

    def test_poll_batch_defaults_to_ten_and_is_configurable(self):
        b = bus("risk")
        b._consumer.queue([msg("agent.cards", value=None, offset=i) for i in range(25)])
        self.assertEqual(len(b.poll(0.1)), 10)
        self.assertEqual(len(b.poll(0.1, max_records=4)), 4)

    def test_poll_survives_malformed_records_in_the_batch(self):
        bad_values = [b"[1]", b"5", b'"s"', b'{"params":"x"}', b'{"result":{"task":5}}', b'{"result":"x"}',
                      b'{"params":{"message":{"metadata":{"urn:device-trust-mesh:envelope:v1":{"eventTime":"x"}}}}}',
                      b'{"params":{"message":{"metadata":{"urn:device-trust-mesh:envelope:v1":{"eventTime":true}}}}}',
                      b'{"params":{"message":{"metadata":[]}}}', b"\xff\xfe"]
        for value in bad_values:
            with self.subTest(value=value):
                b = bus("hop-bad")
                b._consumer.queue([msg("agent.task-events", value=value),
                                   msg("agent.task-events", value=dumps(STATUS), offset=1)])
                with self.assertLogs("dtm.common.bus", "WARNING") if value in _SKIPPED else self.assertNoLogs(
                        "dtm.common.bus", "WARNING"):
                    got = b.poll(0.1)
                self.assertEqual(got[-1].value, STATUS)
                self.assertEqual(len(got), 1 if value in _SKIPPED else 2)

    def test_poll_skips_records_with_undecodable_key_or_headers(self):
        b = bus("risk")
        bad_key = msg("agent.cards", value=None, key=b"\xff", offset=0)
        bad_header = msg("agent.cards", value=None, offset=1)
        bad_header._headers = [("h", b"\xff")]
        b._consumer.queue([bad_key, bad_header, msg("agent.cards", value=None, offset=2)])
        with self.assertLogs("dtm.common.bus", "WARNING"):
            self.assertEqual([i.offset for i in b.poll(0.1)], [2])

    def test_poll_drops_none_valued_headers(self):
        b = bus("risk")
        m = msg("agent.cards", value=None)
        m._headers = [("keep", b"v"), ("null", None)]
        b._consumer.queue([m])
        self.assertEqual(b.poll(0.1)[0].headers, {"keep": "v"})

    def test_poll_observes_hop_latency_for_requests_and_events(self):
        b = bus("hop-agent")
        b._consumer.queue([msg("agent.tasks.hop-agent", value=dumps(REQ)),
                           msg("agent.task-events", value=dumps(STATUS)),
                           msg("agent.cards", value=None)])
        b.poll(0.1)
        self.assertEqual(hop_latency_sum("hop-agent", "agent.tasks.hop-agent"), 40.0)
        self.assertEqual(hop_latency_sum("hop-agent", "agent.task-events"), 40.0)
        self.assertEqual(hop_latency_sum("hop-agent", "agent.cards"), 0.0)

    def test_hop_latency_label_is_the_logical_topic(self):
        b = bus("prefix-agent", BusConfig(topic_prefix="replay.r1."))
        b._consumer.queue([msg("replay.r1.agent.task-events", value=dumps(STATUS))])
        b.poll(0.1)
        self.assertEqual(hop_latency_sum("prefix-agent", "agent.task-events"), 40.0)


class LifecycleTest(unittest.TestCase):
    def test_pause_and_resume_act_on_the_assignment(self):
        b = bus("risk")
        parts = b._consumer.trigger_assign([("agent.tasks.risk", 0)])
        b.pause()
        self.assertEqual(b._consumer.paused, parts)
        b.resume()
        self.assertEqual(b._consumer.paused, [])

    def test_pause_survives_a_rebalance_until_resume(self):
        b = bus("risk")
        b.subscribe(["agent.tasks.risk"])
        b.pause()
        parts = b._consumer.trigger_assign([("agent.tasks.risk", 0), ("agent.tasks.risk", 1)])
        self.assertEqual(b._consumer.paused, parts)
        b.resume()
        self.assertEqual(b._consumer.paused, [])
        b._consumer.trigger_assign([("agent.tasks.risk", 2)])
        self.assertEqual(b._consumer.paused, [])

    def test_paused_property_follows_pause_and_resume(self):
        b = bus("risk")
        self.assertFalse(b.paused)
        b.pause()
        self.assertTrue(b.paused)
        b.resume()
        self.assertFalse(b.paused)

    def test_rewind_seeks_to_the_inbound_offset(self):
        b = bus("risk", BusConfig(topic_prefix="replay.r1."))
        b.rewind(Inbound("replay.r1.agent.tasks.risk", 3, 42, "dev-0001", {}, None, 0))
        self.assertEqual(b._consumer.seeks, [("replay.r1.agent.tasks.risk", 3, 42)])

    def test_close_flushes_and_closes(self):
        b = bus("risk")
        b.close()
        self.assertTrue(b._consumer.closed)
        self.assertEqual(b._producer.flushed, 1)


if __name__ == "__main__":
    unittest.main()
