import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent_fakes import make_bus, request_inbound
from dtm.common.a2a import SELF_TASK_TOPIC, dumps
from dtm.common.config import FileConfigSource
from dtm.mesh.agents import host as host_module
from dtm.mesh.agents.host import AgentHost, agent_deployment, build_agent_api
from dtm.mesh.agents.reasoning import AgentLlm
from fakes import msg

FIXTURE = Path(__file__).parent / "fixtures" / "etc-dtm"
BOOTSTRAP = "kafka.test:9071"
ENV = {"LLM_ENDPOINT": "http://h:11434", "KAFKA_BOOTSTRAP_SERVERS": BOOTSTRAP}
SKILLS = [{"id": "assess", "name": "Assess", "description": "d", "tags": ["t"]}]


class RecordingAgent:
    name = "risk"
    topics = None
    request_topic = SELF_TASK_TOPIC

    def __init__(self):
        self.calls: list[tuple[int, str]] = []
        self.handled = threading.Event()

    def describe(self, settings):
        return "risk agent", SKILLS

    def __call__(self, inbound, settings):
        self.calls.append((inbound.offset, settings.config_version))
        self.handled.set()


class ContextReadingAgent(RecordingAgent):
    reads_context = True

    def __init__(self, timeline=None):
        super().__init__()
        self.timeline = timeline if timeline is not None else []

    def __call__(self, inbound, settings):
        self.timeline.append("handled")
        super().__call__(inbound, settings)


class StubContext:
    """A context whose probe fails ``down`` times (forever when None) before it answers."""

    def __init__(self, target=None, *, down=0, timeline=None):
        self.target = target
        self.closed = 0
        self.down = down
        self.probes = 0
        self.probed = threading.Event()
        self.timeline = timeline if timeline is not None else []

    def probe(self, timeout_s):
        self.probes += 1
        self.probed.set()
        if self.down is None or self.probes <= self.down:
            self.timeline.append("down")
            raise ConnectionError("context-mcp not serving")
        self.timeline.append("up")

    def close(self):
        self.closed += 1


class AgentHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = Path(self.tmp) / "etc-dtm"
        shutil.copytree(FIXTURE, self.root)
        self.agent = RecordingAgent()
        self.bus = make_bus("risk")
        self.host = AgentHost(self.agent, bus=self.bus, config_source=FileConfigSource(str(self.root), ENV),
                              bootstrap=BOOTSTRAP)
        self.addCleanup(self.host.stop)

    def queue_request(self, offset=0):
        inbound = request_inbound("agent.tasks.risk", {"deviceId": "dev-0001"}, offset=offset)
        self.bus._consumer.queue([msg("agent.tasks.risk", dumps(inbound.value), key="dev-0001",
                                      offset=offset)])

    def test_start_handles_queued_request_and_stop_joins(self):
        self.queue_request()
        self.host.start()
        self.assertTrue(self.agent.handled.wait(5))
        thread = next(t for t in threading.enumerate() if t.name == "dtm-agent-risk")
        self.assertTrue(thread.daemon)
        self.host.check_alive()
        self.host.stop()
        self.assertFalse(thread.is_alive())
        self.assertEqual([offset for offset, _ in self.agent.calls], [0])
        self.assertEqual(self.bus._consumer.committed, [("agent.tasks.risk", 0, 1)])

    def test_start_publishes_the_card_with_the_bootstrap(self):
        self.host.start()
        card = [r for r in self.bus._producer.records if r.topic == "agent.cards"][-1]
        self.assertIn(f"kafka://{BOOTSTRAP}/agent.tasks.risk", card.value.decode())

    def test_check_alive_raises_before_start_and_after_stop(self):
        with self.assertRaisesRegex(RuntimeError, "risk"):
            self.host.check_alive()
        self.host.start()
        self.host.check_alive()
        self.host.stop()
        with self.assertRaisesRegex(RuntimeError, "risk"):
            self.host.check_alive()

    def test_stop_is_idempotent_and_closes_the_bus_and_owned_resources(self):
        context = StubContext("x")
        host = AgentHost(self.agent, bus=self.bus, config_source=FileConfigSource(str(self.root), ENV),
                         bootstrap=BOOTSTRAP, closeables=(context,))
        host.start()
        host.stop()
        host.stop()
        self.assertEqual(context.closed, 1)
        self.assertTrue(self.bus._consumer.closed)

    def context_host(self, agent, context):
        host = AgentHost(agent, bus=self.bus, config_source=FileConfigSource(str(self.root), ENV),
                         bootstrap=BOOTSTRAP, context=context)
        self.addCleanup(host.stop)
        return host

    def test_context_reader_consumes_only_once_context_mcp_answers(self):
        timeline = []
        agent = ContextReadingAgent(timeline)
        context = StubContext(down=2, timeline=timeline)
        host = self.context_host(agent, context)
        self.queue_request()
        with mock.patch.object(host_module, "_PROBE_BACKOFF_S", 0.01), \
                self.assertLogs(host_module.log, "WARNING") as logs:
            host.start()
            self.assertTrue(agent.handled.wait(5))
        self.assertEqual(timeline, ["down", "down", "up", "handled"])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("waiting for context-mcp", logs.output[0])
        host.check_alive()

    def test_probe_backoff_doubles_up_to_its_cap(self):
        delays = []
        agent = ContextReadingAgent()
        host = self.context_host(agent, StubContext(down=5))
        host._stop.wait = lambda delay: delays.append(delay) or False
        with mock.patch.object(host_module, "_PROBE_BACKOFF_S", 1.0), \
                mock.patch.object(host_module, "_PROBE_BACKOFF_MAX_S", 4.0), \
                self.assertLogs(host_module.log, "WARNING"):
            self.assertTrue(host._await_context())
        self.assertEqual(delays, [1.0, 2.0, 4.0, 4.0, 4.0])

    def test_stop_interrupts_the_wait_for_context_mcp(self):
        agent = ContextReadingAgent()
        context = StubContext(down=None)
        host = self.context_host(agent, context)
        self.queue_request()
        with mock.patch.object(host_module, "_PROBE_BACKOFF_S", 30.0), \
                self.assertLogs(host_module.log, "WARNING"):
            host.start()
            self.assertTrue(context.probed.wait(5))
            started = time.monotonic()
            host.stop()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(any(t.name == "dtm-agent-risk" for t in threading.enumerate()))
        self.assertEqual(agent.calls, [])
        self.assertTrue(self.bus._consumer.closed)

    def test_agent_that_reads_no_context_is_not_gated(self):
        context = StubContext(down=None)
        host = self.context_host(self.agent, context)
        self.queue_request()
        host.start()
        self.assertTrue(self.agent.handled.wait(5))
        self.assertEqual(context.probes, 0)

    def test_context_reader_needs_a_context_to_probe(self):
        with self.assertRaisesRegex(ValueError, "risk"):
            AgentHost(ContextReadingAgent(), bus=self.bus, config_source=FileConfigSource(str(self.root), ENV),
                      bootstrap=BOOTSTRAP)

    def test_only_the_agents_that_read_context_are_gated(self):
        from dtm.mesh.agents.compliance import ComplianceAgent
        from dtm.mesh.agents.enrolment import EnrolmentAgent
        from dtm.mesh.agents.revocation import RevocationAgent
        from dtm.mesh.agents.risk import RiskAgent
        from dtm.mesh.agents.step_up import StepUpAgent
        self.assertEqual({cls.name: cls.reads_context for cls in (RiskAgent, EnrolmentAgent, StepUpAgent,
                                                                  ComplianceAgent, RevocationAgent)},
                         {"risk": True, "enrolment": True, "step-up": True, "compliance": False,
                          "revocation": False})

    def test_from_environment_probes_the_agents_context(self):
        agent = ContextReadingAgent()
        made = {}

        def context_factory(target):
            made["context"] = StubContext(target)
            return made["context"]

        with mock.patch.dict(os.environ, ENV):
            host = AgentHost.from_environment(lambda bus, ctx, llm: agent, "risk", config_root=str(self.root),
                                              bus_factory=lambda name, config: self.bus,
                                              context_factory=context_factory)
            self.addCleanup(host.stop)
            self.queue_request()
            host.start()
        self.assertTrue(agent.handled.wait(5))
        self.assertEqual(made["context"].probes, 1)

    def test_from_environment_uses_mcp_url_and_bus_config(self):
        seen = {}
        bus = make_bus("risk")
        context = {}

        def bus_factory(name, config):
            seen["bus"] = (name, config)
            return bus

        def context_factory(target):
            context["stub"] = StubContext(target)
            return context["stub"]

        def make_agent(agent_bus, ctx, llm):
            seen["make"] = (agent_bus, ctx, llm)
            return self.agent

        with mock.patch.dict(os.environ, ENV):
            expected = FileConfigSource(str(self.root)).load()
            host = AgentHost.from_environment(make_agent, "risk", config_root=str(self.root),
                                              bus_factory=bus_factory, context_factory=context_factory)
            self.addCleanup(host.stop)
            host.start()
        self.assertEqual(seen["bus"], ("risk", expected.bus))
        self.assertEqual(context["stub"].target, expected.context["mcpUrl"])
        self.assertIs(seen["make"][0], bus)
        self.assertIs(seen["make"][1], context["stub"])
        self.assertIsInstance(seen["make"][2], AgentLlm)
        card = [r for r in bus._producer.records if r.topic == "agent.cards"][-1]
        self.assertIn(f"kafka://{BOOTSTRAP}/", card.value.decode())
        host.stop()
        self.assertEqual(context["stub"].closed, 1)
        self.assertTrue(bus._consumer.closed)

    def test_from_environment_releases_the_bus_when_construction_fails(self):
        bus = make_bus("risk")

        def make_agent(agent_bus, ctx, llm):
            raise RuntimeError("no agent")

        with mock.patch.dict(os.environ, ENV), self.assertRaisesRegex(RuntimeError, "no agent"):
            AgentHost.from_environment(make_agent, "risk", config_root=str(self.root),
                                       bus_factory=lambda name, config: bus, context_factory=StubContext)
        self.assertTrue(bus._consumer.closed)


class AgentDeploymentTest(unittest.TestCase):
    def test_deployment_name(self):
        self.assertEqual(agent_deployment("RiskAgent", "risk", lambda bus, ctx, llm: RecordingAgent()).name,
                         "RiskAgent")

    def test_replica_reserves_a_fraction_of_a_cpu(self):
        deployment = agent_deployment("RiskAgent", "risk", lambda bus, ctx, llm: RecordingAgent())
        self.assertEqual(deployment.ray_actor_options["num_cpus"], 0.1)

    def test_api_serves_health_and_metrics(self):
        client = TestClient(build_agent_api("risk"))
        health = client.get("/healthz")
        self.assertEqual((health.status_code, health.json()), (200, {"agent": "risk"}))
        metrics = client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertTrue(metrics.headers["content-type"].startswith("text/plain"))
        self.assertIn("dtm_agent_processing_latency_ms", metrics.text)


if __name__ == "__main__":
    unittest.main()
