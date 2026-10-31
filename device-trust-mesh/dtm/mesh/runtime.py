"""AgentRuntime: the poll loop around an agent's handler, with hot reload and the Agent Card lifecycle.

Configuration is re-read before every message, so a handler always sees the settings current at its
message. A disabled agent has no card on ``agent.cards`` and its consumer is paused; records fetched
before the pause and not yet handled are rewound so the paused consumer delivers them again after
re-enabling.
"""
import logging
import threading
from collections.abc import Callable

from dtm.common.a2a import agent_card, task_topic
from dtm.common.bus import AgentBus, CommitFailed, DeliveryFailed, Inbound
from dtm.common.config import AgentSettings, ConfigError, FileConfigSource
from dtm.common.metrics import HANDLER_ERRORS

log = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(self, name: str, bus: AgentBus, config_source: FileConfigSource,
                 describe: Callable[[AgentSettings], tuple[str, list[dict]]],
                 handler: Callable[[Inbound, AgentSettings], None], *, bootstrap: str):
        self.name = name
        self.bus = bus
        self.handler = handler
        self._config_source = config_source
        self._describe = describe
        self._bootstrap = bootstrap
        self._settings: AgentSettings | None = None

    @property
    def settings(self) -> AgentSettings:
        return self._settings

    def start(self) -> None:
        config = self._config_source.load()
        if self.name not in config.agents:
            raise ConfigError(f"agent {self.name!r} is not configured")
        self._settings = config.agents[self.name]
        self.bus.subscribe([task_topic(self.name)])
        if self._settings.enabled:
            self._publish_card()
        else:
            self._withdraw()

    def step(self, timeout: float = 0.1) -> int:
        """Process one poll batch and return how many messages were handled and committed."""
        batch = self.bus.poll(timeout)
        if not batch:
            self._reload()
            return 0
        handled = 0
        for i, inbound in enumerate(batch):
            self._reload()
            if not self._settings.enabled:
                self._rewind(batch[i:])
                break
            self._handle(inbound)
            try:
                self.bus.commit(inbound)
            except (DeliveryFailed, CommitFailed) as e:
                log.error("%s: not committing %s[%s]@%s, it will be processed again: %s",
                          self.name, inbound.topic, inbound.partition, inbound.offset, e)
                self._rewind(batch[i:])
                break
            handled += 1
        return handled

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.step()

    def _handle(self, inbound: Inbound) -> None:
        try:
            self.handler(inbound, self._settings)
        except Exception:
            HANDLER_ERRORS.labels(self.name).inc()
            log.exception("%s: handler failed on %s[%s]@%s", self.name, inbound.topic, inbound.partition,
                          inbound.offset)

    def _rewind(self, unhandled: list[Inbound]) -> None:
        """Seek each partition back to its first unhandled record so the consumer delivers them again."""
        first: dict[tuple[str, int], Inbound] = {}
        for inbound in unhandled:
            first.setdefault((inbound.topic, inbound.partition), inbound)
        for inbound in first.values():
            self.bus.rewind(inbound)

    def _reload(self) -> None:
        config = self._config_source.refresh()
        if config is None:
            return
        new = config.agents.get(self.name)
        if new is None:
            log.warning("%s: agent absent from reloaded config, keeping current settings", self.name)
            return
        old, self._settings = self._settings, new
        if old.enabled and not new.enabled:
            self._withdraw()
        elif new.enabled and not old.enabled:
            self._publish_card()
            self.bus.resume()
        elif new.enabled and new.config_version != old.config_version:
            self._publish_card()

    def _publish_card(self) -> None:
        description, skills = self._describe(self._settings)
        self.bus.publish_card(agent_card(self.name, description, skills, self._settings.config_version,
                                         self._bootstrap))

    def _withdraw(self) -> None:
        self.bus.tombstone_card(self.name)
        self.bus.pause()
