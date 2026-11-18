"""Test helper: capture records on a logger without changing its level, as ``assertLogs`` would."""
import logging
import unittest

from dtm.mesh import trail


class Capture(logging.Handler):
    def __init__(self, with_scope_filter: bool = True):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        if with_scope_filter:
            self.addFilter(trail.ScopeFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def steps(self, min_level: int = logging.DEBUG) -> list[str]:
        """The ``dtm.step`` of each captured record at or above ``min_level`` that has one."""
        return [r.dtm["step"] for r in self.records
                if r.levelno >= min_level and "step" in getattr(r, "dtm", {})]

    def step_records(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "dtm", {}).get("step") == name]


def capture(test: unittest.TestCase, logger: str = trail.LOGGER, level: int = logging.DEBUG,
            with_scope_filter: bool = True) -> Capture:
    """Attach a ``Capture`` to ``logger`` at ``level`` for the rest of ``test``, restoring the logger after."""
    target = logging.getLogger(logger)
    handler = Capture(with_scope_filter)
    old_level = target.level
    target.addHandler(handler)
    target.setLevel(level)

    def restore() -> None:
        target.removeHandler(handler)
        target.setLevel(old_level)

    test.addCleanup(restore)
    return handler
