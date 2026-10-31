"""Runs one agent: its poll loop on a daemon thread, and its Ray Serve deployment.

``AgentHost`` wraps an ``AgentRuntime`` around an ``Agent``. ``agent_deployment`` builds the Serve
deployment for an agent: the replica starts a host from the mounted configuration, reports unhealthy when
the poll loop thread dies, and stops the host when it is torn down.
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


class Agent(Protocol):
    """What the host needs of an agent.

    ``topics`` are the logical topics it consumes (None for its own task topic); ``request_topic`` is the
    topic its Agent Card advertises (``SELF_TASK_TOPIC`` for an agent that takes requests, None for an
    observer of ``agent.task-events``).
    """
    name: str
    topics: list[str] | None
    request_topic: str | None

    def describe(self, settings: AgentSettings) -> tuple[str, list[dict]]: ...

    def __call__(self, inbound: Inbound, settings: AgentSettings) -> None: ...


class AgentHost:
    """Owns the bus, and any ``closeables`` handed in, from ``start`` until ``stop``."""

    def __init__(self, agent: Agent, *, bus: AgentBus, config_source: FileConfigSource, bootstrap: str,
                 closeables: Sequence = ()):
        self._name = agent.name
        self._bus = bus
        self._closeables = tuple(closeables)
        self._runtime = AgentRuntime(agent.name, bus, config_source, agent.describe, agent,
                                     bootstrap=bootstrap, topics=agent.topics, request_topic=agent.request_topic)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False

    @classmethod
    def from_environment(cls, make_agent: Callable[[SyncContext, AgentLlm], Agent], name: str, *,
                         config_root: str = CONFIG_ROOT, bus_factory=AgentBus.connect,
                         context_factory=SyncContext) -> "AgentHost":
        source = FileConfigSource(config_root)
        config = source.load()
        bus = bus_factory(name, config.bus)
        context = None
        try:
            context = context_factory(config.context["mcpUrl"])
            agent = make_agent(context, AgentLlm(name))
        except BaseException:
            if context is not None:
                context.close()
            bus.close()
            raise
        return cls(agent, bus=bus, config_source=source, bootstrap=bootstrap_servers(), closeables=(context,))

    def start(self) -> None:
        self._runtime.start()
        self._thread = threading.Thread(target=self._runtime.run, args=(self._stop,),
                                        name=f"dtm-agent-{self._name}", daemon=True)
        self._thread.start()

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
    """Return the Serve deployment that runs the agent ``name``, built by ``make_agent(context, llm)``."""

    def builder() -> FastAPI:
        # Built inside the replica, like the context-mcp app.
        return build_agent_api(name)

    @serve.deployment(name=deployment_name)
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
