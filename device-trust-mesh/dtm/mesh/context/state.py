"""Per-process context state: the table and the materializer that fills it."""
import os
import threading
from dataclasses import dataclass

from dtm.mesh.context.materializer import Materializer
from dtm.mesh.context.table import ContextTable

_DEFAULT_WAIT_TIMEOUT_MS = 2000


@dataclass
class ContextState:
    table: ContextTable
    materializer: Materializer | None
    wait_timeout_ms: int

    def ready(self) -> bool:
        return self.materializer is None or self.materializer.ready.is_set()

    @classmethod
    def from_env(cls) -> "ContextState":
        """Build the table from ``DEVICE_CAP`` and start the materializer that fills it."""
        table = ContextTable(int(os.environ["DEVICE_CAP"]))
        wait_timeout_ms = int(os.environ.get("DTM_CONTEXT_WAIT_TIMEOUT_MS", _DEFAULT_WAIT_TIMEOUT_MS))
        materializer = Materializer.connect(table)
        materializer.start()
        return cls(table, materializer, wait_timeout_ms)


_state: ContextState | None = None
_lock = threading.Lock()


def get_state() -> ContextState:
    """Return this process's state, creating it on first use."""
    global _state
    with _lock:
        if _state is None:
            _state = ContextState.from_env()
        return _state
