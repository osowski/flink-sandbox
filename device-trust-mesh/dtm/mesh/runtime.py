"""AgentRuntime: the poll loop around an agent's handler, with hot reload and the Agent Card lifecycle.

Configuration is re-read before every message, so a handler always sees the settings current at its
message. A disabled agent, or one removed from ``agents.yaml``, has no card on ``agent.cards`` and its
consumer is paused; records fetched before the pause and not yet handled are rewound so the paused
consumer delivers them again after re-enabling.

Card writes are confirmed as soon as they are made. An unconfirmed card operation, whether it failed
delivery or raised, is retried at the start of every ``step`` and never reaches a message commit. A
pause or resume that follows a lifecycle change is applied even when the card write raises. Records of
a batch that were fetched but not handled are rewound when anything unexpected escapes ``step``. Of the bus settings only ``policyCommands``
is applied on reload; a config that changes any other bus setting is refused and the agent keeps its
previous settings.
"""
import logging
import threading
import time
from collections.abc import Callable

from confluent_kafka import KafkaException

from dtm.common.a2a import SELF_TASK_TOPIC, agent_card, task_topic
from dtm.common.bus import AgentBus, CommitFailed, DeliveryFailed, Inbound
from dtm.common.config import AgentSettings, ConfigError, FileConfigSource
from dtm.common.metrics import HANDLER_ERRORS, LOOP_ERRORS, PROCESSING_LATENCY_MS

log = logging.getLogger(__name__)

_CARD_DELIVERY_TIMEOUT_S = 10.0
_LOOP_BACKOFF_S = 1.0


class AgentRuntime:
    def __init__(self, name: str, bus: AgentBus, config_source: FileConfigSource,
                 describe: Callable[[AgentSettings], tuple[str, list[dict]]],
                 handler: Callable[[Inbound, AgentSettings], None], *, bootstrap: str,
                 topics: list[str] | None = None, request_topic: str | None = SELF_TASK_TOPIC):
        """``topics`` are the logical topics the agent consumes, by default its own task topic.

        ``request_topic`` is the topic its Agent Card advertises, by default its own task topic; an agent
        that takes no requests (an observer of ``agent.task-events``) passes None and its card lists no
        interfaces.
        """
        self.name = name
        self.bus = bus
        self.handler = handler
        self._topics = list(topics) if topics is not None else [task_topic(name)]
        self._request_topic = request_topic
        self._config_source = config_source
        self._describe = describe
        self._bootstrap = bootstrap
        self._settings: AgentSettings | None = None
        self._active = False
        self._card_confirmed = True

    @property
    def settings(self) -> AgentSettings | None:
        """The settings in force, or None before ``start``."""
        return self._settings

    def start(self) -> None:
        config = self._config_source.load()
        if self.name not in config.agents:
            raise ConfigError(f"agent {self.name!r} is not configured")
        self._settings = config.agents[self.name]
        self._active = self._settings.enabled
        self.bus.subscribe(self._topics)
        self._sync_card()
        if not self._active:
            self.bus.pause()

    def step(self, timeout: float = 0.1) -> int:
        """Process one poll batch and return how many messages were handled and committed."""
        if not self._card_confirmed:
            self._sync_card()
        batch = self.bus.poll(timeout)
        if not batch:
            self._reload()
            return 0
        handled = 0
        i = 0
        try:
            for i, inbound in enumerate(batch):
                self._reload()
                if not self._active:
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
        except Exception:
            self._rewind(batch[i:])
            raise
        return handled

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                LOOP_ERRORS.labels(self.name).inc()
                log.exception("%s: poll loop iteration failed, retrying in %ss", self.name, _LOOP_BACKOFF_S)
                stop.wait(_LOOP_BACKOFF_S)

    def _handle(self, inbound: Inbound) -> None:
        started = time.monotonic()
        try:
            self.handler(inbound, self._settings)
        except Exception:
            HANDLER_ERRORS.labels(self.name).inc()
            log.exception("%s: handler failed on %s[%s]@%s", self.name, inbound.topic, inbound.partition,
                          inbound.offset)
        finally:
            PROCESSING_LATENCY_MS.labels(self.name).observe((time.monotonic() - started) * 1000)

    def _rewind(self, unhandled: list[Inbound]) -> None:
        """Seek each partition back to its first unhandled record so the consumer delivers them again."""
        first: dict[tuple[str, int], Inbound] = {}
        for inbound in unhandled:
            first.setdefault((inbound.topic, inbound.partition), inbound)
        for inbound in first.values():
            try:
                self.bus.rewind(inbound)
            except KafkaException as e:
                log.warning("%s: cannot rewind %s[%s]@%s, the partition is no longer assigned: %s",
                            self.name, inbound.topic, inbound.partition, inbound.offset, e)

    def _reload(self) -> None:
        config = self._config_source.refresh()
        if config is None:
            return
        if config.bus != self.bus.config:
            try:
                self.bus.reconfigure(config.bus)
            except ValueError as e:
                log.error("%s: config reload refused, keeping previous settings: %s", self.name, e)
                return
        new = config.agents.get(self.name)
        if new is None:
            log.warning("%s: agent absent from reloaded config, treating it as disabled", self.name)
        was_active, old = self._active, self._settings
        self._active = new is not None and new.enabled
        if new is not None:
            self._settings = new
        if was_active and not self._active:
            try:
                self._sync_card()
            finally:
                self.bus.pause()
        elif self._active and not was_active:
            try:
                self._sync_card()
            finally:
                self.bus.resume()
        elif self._active and new.config_version != old.config_version:
            self._sync_card()

    def _sync_card(self) -> None:
        """Make the card topic reflect the agent's state: the current card when active, a tombstone otherwise."""
        try:
            if self._active:
                description, skills = self._describe(self._settings)
                self.bus.publish_card(agent_card(self.name, description, skills, self._settings.config_version,
                                                 self._bootstrap, request_topic=self._request_topic))
            else:
                self.bus.tombstone_card(self.name)
            self.bus.ensure_delivered(_CARD_DELIVERY_TIMEOUT_S)
        except DeliveryFailed as e:
            self._card_confirmed = False
            log.error("%s: agent card not delivered, retrying on the next step: %s", self.name, e)
        except BaseException:
            self._card_confirmed = False
            raise
        else:
            self._card_confirmed = True
