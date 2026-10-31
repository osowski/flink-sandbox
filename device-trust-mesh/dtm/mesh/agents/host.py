"""Runs one agent: its poll loop on a daemon thread, and its Ray Serve deployment.

``AgentHost`` wraps an ``AgentRuntime`` around an ``Agent``. An agent that reads context starts consuming
only once context-mcp answers a probe, so no request is failed while context-mcp is still catching up.
``agent_deployment`` builds the Serve deployment for an agent: the replica starts a host from the mounted
configuration, reports unhealthy when the poll loop thread dies, and stops the host when it is torn down.
"""
import logging
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from ray import serve

from dtm.common.bus import AgentBus, Inbound
from dtm.common.config import CONFIG_ROOT, AgentSettings, FileConfigSource
from dtm.common.kafka import bootstrap_servers
from dtm.mesh.agents.context import SyncContext
from dtm.mesh.agents.reasoning import AgentLlm
from dtm.mesh.runtime import AgentRuntime

log = logging.getLogger(__name__)

# A probe that hangs holds the poll thread at most this long plus the context call grace, well within the
# time ``stop`` waits for the thread.
_PROBE_TIMEOUT_S = 2.0
_PROBE_BACKOFF_S = 0.5
_PROBE_BACKOFF_MAX_S = 15.0


class Agent(Protocol):
    """What the host needs of an agent.

    ``topics`` are the logical topics it consumes (None for its own task topic); ``request_topic`` is the
    topic its Agent Card advertises (``SELF_TASK_TOPIC`` for an agent that takes requests, None for an
    observer of ``agent.task-events``). ``reads_context`` is optional and False when absent; an agent that
    sets it True calls context-mcp from its handler, and its host waits for context-mcp before consuming.
    """
    name: str
    topics: list[str] | None
    request_topic: str | None
    reads_context: bool

    def describe(self, settings: AgentSettings) -> tuple[str, list[dict]]: ...

    def __call__(self, inbound: Inbound, settings: AgentSettings) -> None: ...


class AgentHost:
    """Owns the bus, and any ``closeables`` handed in, from ``start`` until ``stop``.

    ``context`` is the context-mcp client the host probes before consuming, required for an agent that
    reads context and unused otherwise.
    """

    def __init__(self, agent: Agent, *, bus: AgentBus, config_source: FileConfigSource, bootstrap: str,
                 context: SyncContext | None = None, closeables: Sequence = ()):
        reads_context = getattr(agent, "reads_context", False)
        if reads_context and context is None:
            raise ValueError(f"agent {agent.name} reads context, so its host needs a context to probe")
        self._name = agent.name
        self._context = context if reads_context else None
        self._bus = bus
        self._closeables = tuple(closeables)
        self._runtime = AgentRuntime(agent.name, bus, config_source, agent.describe, agent,
                                     bootstrap=bootstrap, topics=agent.topics, request_topic=agent.request_topic)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False

    @classmethod
    def from_environment(cls, make_agent: Callable[[AgentBus, SyncContext, AgentLlm], Agent], name: str, *,
                         config_root: str = CONFIG_ROOT, bus_factory=AgentBus.connect,
                         context_factory=SyncContext) -> "AgentHost":
        source = FileConfigSource(config_root)
        config = source.load()
        bus = bus_factory(name, config.bus)
        context = None
        try:
            context = context_factory(config.context["mcpUrl"])
            agent = make_agent(bus, context, AgentLlm(name))
        except BaseException:
            if context is not None:
                context.close()
            bus.close()
            raise
        return cls(agent, bus=bus, config_source=source, bootstrap=bootstrap_servers(), context=context,
                   closeables=(context,))

    def start(self) -> None:
        self._runtime.start()
        self._thread = threading.Thread(target=self._run, name=f"dtm-agent-{self._name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if self._context is not None and not self._await_context():
            return
        self._runtime.run(self._stop)

    def _await_context(self) -> bool:
        """Probe context-mcp with capped backoff until it answers; return False when the host stops first."""
        delay = _PROBE_BACKOFF_S
        while not self._stop.is_set():
            try:
                self._context.probe(_PROBE_TIMEOUT_S)
            except Exception as e:
                log.warning("%s: waiting for context-mcp before consuming, retrying in %gs: %r",
                            self._name, delay, e)
                if self._stop.wait(delay):
                    return False
                delay = min(delay * 2, _PROBE_BACKOFF_MAX_S)
            else:
                log.info("%s: context-mcp answered, consuming", self._name)
                return True
        return False

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop the poll loop, join its thread, then close the bus and the owned resources; safe to call again."""
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                log.warning("%s: poll loop did not stop within %ss; leaving its bus open", self._name, timeout_s)
                return
        for resource in (self._bus, *self._closeables):
            try:
                resource.close()
            except Exception:
                log.exception("%s: closing %r failed", self._name, resource)

    def check_alive(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError(f"agent {self._name} poll loop is not running")


def build_agent_api(name: str) -> FastAPI:
    api = FastAPI()

    @api.get("/healthz")
    async def healthz():
        return {"agent": name}

    @api.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return api


def agent_deployment(deployment_name: str, name: str, make_agent) -> serve.Deployment:
    """Return the Serve deployment that runs the agent ``name``, built by ``make_agent(bus, context, llm)``."""

    def builder() -> FastAPI:
        # Built inside the replica, like the context-mcp app.
        return build_agent_api(name)

    # Each agent replica mostly waits on Kafka and context-mcp, so it reserves a fraction of a CPU; the
    # RayService's serveConfigV2 can override it per deployment.
    @serve.deployment(name=deployment_name, ray_actor_options={"num_cpus": 0.1})
    @serve.ingress(builder)
    class AgentDeployment:
        def __init__(self):
            self._host = AgentHost.from_environment(make_agent, name)
            self._host.start()

        def check_health(self) -> None:
            self._host.check_alive()

        def __del__(self):
            host = getattr(self, "_host", None)
            if host is not None:
                host.stop()

    return AgentDeployment
