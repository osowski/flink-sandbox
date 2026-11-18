"""The per-task log trail: a scope for the record an agent is handling, and one log line per step.

``AgentRuntime`` opens a ``message_scope`` for each inbound record. Once the handler knows its task it calls
``establish``, which binds the agent, task, context and device ids into the scope and logs ``received``.
Every later ``step`` line carries those ids, and ``ScopeFilter`` adds them to any other record logged while
the scope is open.

All trail fields go in the record's ``dtm`` dict, never as top-level record attributes: Ray Serve deletes a
top-level ``task_id`` from its replica logs. A record is handled on one thread from poll to commit, so a
context variable holds the scope.
"""
import contextlib
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

LOGGER = "dtm"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_log = logging.getLogger(f"{LOGGER}.trail")


@dataclass(frozen=True)
class _Scope:
    position: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)
    established: bool = False


_EMPTY = _Scope()
_scope: ContextVar[_Scope] = ContextVar("dtm_trail_scope", default=_EMPTY)


def _compact(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if value is not None}


@contextlib.contextmanager
def message_scope(topic: str, partition: int, offset: int) -> Iterator[None]:
    """Open a fresh scope for one inbound record; it replaces any open scope and is reset on exit."""
    token = _scope.set(_Scope(position={"topic": topic, "partition": partition, "offset": offset}))
    try:
        yield
    finally:
        _scope.reset(token)


def establish(agent: str, task_id: str, context_id: str, device_id: str | None) -> None:
    """Bind the task's ids into the open scope, mark it established and log ``received``.

    Outside a scope it still logs ``received``, without a position, and binds nothing.
    """
    scope = _scope.get()
    bound = _compact({"agent": agent, "task_id": task_id, "context_id": context_id, "device_id": device_id})
    if scope is not _EMPTY:
        _scope.set(replace(scope, ids=bound, established=True))
    _log.info("received", extra={"dtm": {**bound, "step": "received", **scope.position}})


def established() -> bool:
    return _scope.get().established


def ids() -> dict:
    """The ids bound in the open scope: ``agent``, ``task_id``, ``context_id`` and ``device_id`` when set."""
    return dict(_scope.get().ids)


def position() -> dict:
    """The open scope's ``topic``, ``partition`` and ``offset``, or an empty dict outside a scope."""
    return dict(_scope.get().position)


def step(name: str, level: int = logging.INFO, **fields) -> None:
    """Log the step ``name`` with the scope's ids and ``fields`` (``None`` values dropped) under ``dtm``."""
    _log.log(level, name, extra={"dtm": {**ids(), "step": name, **_compact(fields)}})


class ScopeFilter(logging.Filter):
    """Add the open scope's ids to every record's ``dtm`` dict, keeping any key the record already set."""

    def filter(self, record: logging.LogRecord) -> bool:
        bound = _scope.get().ids
        if bound:
            own = getattr(record, "dtm", None)
            record.dtm = {**bound, **own} if isinstance(own, dict) else dict(bound)
        return True


def parse_level(value: str | None) -> int:
    """Return the logging level ``DTM_LOG_LEVEL`` names: ``INFO`` when unset, else a level name in any case."""
    if not value:
        return logging.INFO
    name = value.strip().upper()
    if name not in LEVELS:
        raise ValueError(f"DTM_LOG_LEVEL must be one of {', '.join(LEVELS)}, not {value!r}")
    return logging.getLevelName(name)
