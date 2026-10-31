import dataclasses
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import yaml
from prometheus_client import REGISTRY

from dtm.common.a2a import dumps, send_message_request
from dtm.common.bus import AgentBus, BusConfig
from dtm.common.config import ConfigError, FileConfigSource
from dtm.common.envelope import Envelope
from dtm.mesh.runtime import AgentRuntime
from fakes import FakeConsumer, FakeProducer, msg

FIXTURE = Path(__file__).parent / "fixtures" / "etc-dtm"
BOOTSTRAP = "kafka.test:9071"
SKILLS = [{"id": "assess", "name": "Assess", "description": "d", "tags": ["t"]}]
ENV = Envelope("m-0", "c-1", None, "agent:risk", "dev", "abc123", 1793323937770, "dev-0001", "cust-001", 5, 5)


def describe(settings):
    return f"{settings.name} agent", SKILLS


def req(n, agent="risk", partition=0):
    request = send_message_request(dataclasses.replace(ENV, message_id=f"m-{n}"),
                                   {"taskType": "DECIDE_ENROLMENT", "deviceId": "dev-0001"}, "Decide")
    return msg(f"agent.tasks.{agent}", dumps(request), key="dev-0001", partition=partition, offset=n)


def raising(inbound, settings):
    raise RuntimeError("boom")


def handler_errors(agent):
    return REGISTRY.get_sample_value("dtm_handler_errors_total", {"agent": agent}) or 0.0


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = Path(self.tmp) / "etc-dtm"
        shutil.copytree(FIXTURE, self.root)
        self.doc = yaml.safe_load((FIXTURE / "agents.yaml").read_text())
        self.handled: list[tuple[int, str]] = []

    def runtime(self, name, handler=None):
        bus = AgentBus(name, BusConfig(), producer=FakeProducer(), consumer=FakeConsumer(),
                       avro_serialize=lambda topic, value: json.dumps(value).encode())
        source = FileConfigSource(str(self.root), {"LLM_ENDPOINT": "http://h:11434"})
        return AgentRuntime(name, bus, source, describe, handler or self.recording_handler(),
                            bootstrap=BOOTSTRAP)

    def recording_handler(self, on_first=None):
        def handle(inbound, settings):
            self.handled.append((inbound.offset, settings.llm["model"]))
            if on_first is not None and len(self.handled) == 1:
                on_first()
        return handle

    def write(self):
        (self.root / "agents.yaml").write_text(yaml.safe_dump(self.doc))

    def set_enabled(self, name, enabled):
        self.doc["agents"][name]["enabled"] = enabled
        self.write()

    def set_model(self, model):
        self.doc["llm"]["model"] = model
        self.write()

    @staticmethod
    def last_card(rt):
        return rt.bus._producer.records[-1].value

    def test_start_enabled_publishes_card_with_config_version(self):
        rt = self.runtime("risk")
        rt.start()
        record = rt.bus._producer.records[-1]
        card = json.loads(record.value)
        self.assertEqual((record.topic, record.key), ("agent.cards", b"risk"))
        self.assertEqual(card["version"], rt.settings.config_version)
        self.assertEqual(card["description"], "risk agent")
        self.assertEqual(card["supportedInterfaces"][0]["url"], f"kafka://{BOOTSTRAP}/agent.tasks.risk")
        self.assertFalse(rt.bus.paused)

    def test_start_subscribes_to_the_agents_task_topic(self):
        rt = self.runtime("risk")
        rt.start()
        self.assertEqual(rt.bus._consumer.subscribed, ["agent.tasks.risk"])

    def test_start_disabled_tombstones_and_pauses(self):
        rt = self.runtime("revocation")
        rt.start()
        record = rt.bus._producer.records[-1]
        self.assertEqual((record.topic, record.key, record.value), ("agent.cards", b"revocation", None))
        self.assertTrue(rt.bus.paused)

    def test_start_fails_when_the_agent_is_not_configured(self):
        del self.doc["agents"]["risk"]
        self.write()
        with self.assertRaises(ConfigError):
            self.runtime("risk").start()

    def test_step_handles_and_commits_each_message(self):
        rt = self.runtime("risk")
        rt.start()
        rt.bus._consumer.queue([req(1), req(2)])
        self.assertEqual(rt.step(), 2)
        self.assertEqual([o for o, _ in self.handled], [1, 2])
        self.assertEqual(rt.bus._consumer.committed, [("agent.tasks.risk", 0, 2), ("agent.tasks.risk", 0, 3)])

    def test_idle_step_returns_zero(self):
        rt = self.runtime("risk")
        rt.start()
        self.assertEqual(rt.step(), 0)

    def test_disable_at_message_boundary(self):
        rt = self.runtime("risk")
        rt.start()
        rt.bus._consumer.queue([req(1), req(2)])
        rt.handler = self.recording_handler(on_first=lambda: self.set_enabled("risk", False))
        self.assertEqual(rt.step(), 1)
        self.assertEqual([o for o, _ in self.handled], [1])
        self.assertTrue(rt.bus.paused)
        self.assertIsNone(self.last_card(rt))
        self.assertEqual(rt.bus._consumer.committed[-1][2], 2)
        self.assertEqual(rt.bus._consumer.seeks, [("agent.tasks.risk", 0, 2)])

    def test_disable_rewinds_the_first_unhandled_record_of_each_partition(self):
        rt = self.runtime("risk")
        rt.start()
        rt.bus._consumer.queue([req(1), req(2), req(5, partition=1), req(6, partition=1)])
        rt.handler = self.recording_handler(on_first=lambda: self.set_enabled("risk", False))
        self.assertEqual(rt.step(), 1)
        self.assertEqual(sorted(rt.bus._consumer.seeks), [("agent.tasks.risk", 0, 2), ("agent.tasks.risk", 1, 5)])

    def test_disabled_agent_handles_nothing_and_rewinds_a_polled_batch(self):
        rt = self.runtime("revocation")
        rt.start()
        rt.bus._consumer.queue([req(1, "revocation")])
        self.assertEqual(rt.step(), 0)
        self.assertEqual(self.handled, [])
        self.assertEqual(rt.bus._consumer.committed, [])
        self.assertEqual(rt.bus._consumer.seeks, [("agent.tasks.revocation", 0, 1)])

    def test_reenable_resumes_and_republishes(self):
        rt = self.runtime("risk")
        rt.start()
        self.set_enabled("risk", False)
        rt.step()
        self.assertTrue(rt.bus.paused)
        self.assertIsNone(self.last_card(rt))
        self.set_enabled("risk", True)
        rt.step()
        self.assertFalse(rt.bus.paused)
        self.assertEqual(json.loads(self.last_card(rt))["version"], rt.settings.config_version)

    def test_idle_step_applies_a_disable(self):
        rt = self.runtime("risk")
        rt.start()
        self.set_enabled("risk", False)
        self.assertEqual(rt.step(), 0)
        self.assertTrue(rt.bus.paused)
        self.assertIsNone(self.last_card(rt))

    def test_model_change_republishes_card_with_new_version(self):
        rt = self.runtime("risk")
        rt.start()
        v1 = rt.settings.config_version
        self.set_model("qwen3:8b")
        rt.bus._consumer.queue([req(1)])
        rt.step()
        self.assertNotEqual(rt.settings.config_version, v1)
        self.assertEqual(json.loads(self.last_card(rt))["version"], rt.settings.config_version)

    def test_unchanged_config_does_not_republish(self):
        rt = self.runtime("risk")
        rt.start()
        published = len(rt.bus._producer.records)
        rt.bus._consumer.queue([req(1)])
        rt.step()
        self.assertEqual(len(rt.bus._producer.records), published)

    def test_another_agents_change_keeps_this_agents_card(self):
        rt = self.runtime("risk")
        rt.start()
        published = len(rt.bus._producer.records)
        self.set_enabled("enrolment", False)
        rt.step()
        self.assertEqual(len(rt.bus._producer.records), published)
        self.assertFalse(rt.bus.paused)

    def test_agent_missing_from_a_reloaded_config_keeps_last_settings(self):
        rt = self.runtime("risk")
        rt.start()
        before = rt.settings
        del self.doc["agents"]["risk"]
        self.write()
        rt.bus._consumer.queue([req(1)])
        with self.assertLogs("dtm.mesh.runtime", "WARNING"):
            self.assertEqual(rt.step(), 1)
        self.assertEqual(rt.settings, before)

    def test_handler_sees_settings_current_at_its_message(self):
        rt = self.runtime("risk")
        rt.start()
        rt.handler = self.recording_handler(on_first=lambda: self.set_model("qwen3:8b"))
        rt.bus._consumer.queue([req(1), req(2)])
        rt.step()
        self.assertEqual(self.handled, [(1, "qwen3:4b"), (2, "qwen3:8b")])

    def test_handler_error_counted_and_committed(self):
        rt = self.runtime("risk", handler=raising)
        rt.start()
        before = handler_errors("risk")
        rt.bus._consumer.queue([req(1)])
        with self.assertLogs("dtm.mesh.runtime", "ERROR"):
            self.assertEqual(rt.step(), 1)
        self.assertEqual(rt.bus._consumer.committed[-1][2], 2)
        self.assertEqual(handler_errors("risk") - before, 1)

    def test_delivery_failure_rewinds_without_committing_or_counting(self):
        def produce_and_fail(inbound, settings):
            rt.bus._producer.fail_next_delivery = True
            rt.bus.tombstone_card("other")
        rt = self.runtime("risk", handler=produce_and_fail)
        rt.start()
        rt.bus._consumer.queue([req(1), req(2), req(5, partition=1)])
        with self.assertLogs("dtm.mesh.runtime", "ERROR"):
            self.assertEqual(rt.step(), 0)
        self.assertEqual(rt.bus._consumer.committed, [])
        self.assertEqual(sorted(rt.bus._consumer.seeks), [("agent.tasks.risk", 0, 1), ("agent.tasks.risk", 1, 5)])

    def test_redelivered_record_is_handled_after_a_delivery_failure(self):
        failures = iter([True, False])
        def flaky(inbound, settings):
            rt.bus._producer.fail_next_delivery = next(failures)
            rt.bus.tombstone_card("other")
        rt = self.runtime("risk", handler=flaky)
        rt.start()
        rt.bus._consumer.queue([req(1)])
        with self.assertLogs("dtm.mesh.runtime", "ERROR"):
            rt.step()
        rt.bus._consumer.queue([req(1)])
        self.assertEqual(rt.step(), 1)
        self.assertEqual(rt.bus._consumer.committed, [("agent.tasks.risk", 0, 2)])

    def test_commit_rejection_rewinds_without_counting(self):
        rt = self.runtime("risk")
        rt.start()
        rt.bus._consumer.commit_error = "broker said no"
        rt.bus._consumer.queue([req(1), req(2)])
        with self.assertLogs("dtm.mesh.runtime", "ERROR"):
            self.assertEqual(rt.step(), 0)
        self.assertEqual(rt.bus._consumer.seeks, [("agent.tasks.risk", 0, 1)])
        self.assertEqual([o for o, _ in self.handled], [1])

    def test_run_loops_until_stopped(self):
        stop = threading.Event()
        rt = self.runtime("risk")
        rt.start()
        rt.handler = self.recording_handler(on_first=stop.set)
        rt.bus._consumer.queue([req(1)])
        rt.run(stop)
        self.assertEqual([o for o, _ in self.handled], [1])


if __name__ == "__main__":
    unittest.main()
