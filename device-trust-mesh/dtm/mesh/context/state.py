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
    materializer_thread: threading.Thread | None = None

    def ready(self) -> bool:
        return self.materializer is None or self.materializer.ready.is_set()

    def check_alive(self) -> None:
        """Raise when the materializer thread has stopped, since the table would no longer advance."""
        if self.materializer_thread is not None and not self.materializer_thread.is_alive():
            raise RuntimeError("context materializer thread is no longer running")

    @classmethod
    def from_env(cls) -> "ContextState":
        """Build the table from ``DEVICE_CAP`` and start the materializer that fills it."""
        raw_cap = os.environ.get("DEVICE_CAP")
        try:
            device_cap = int(raw_cap)
        except (TypeError, ValueError):
            raise RuntimeError(f"DEVICE_CAP must be set to an integer, got {raw_cap!r}") from None
        table = ContextTable(device_cap)
        wait_timeout_ms = int(os.environ.get("DTM_CONTEXT_WAIT_TIMEOUT_MS", _DEFAULT_WAIT_TIMEOUT_MS))
        materializer = Materializer.connect(table)
        thread = materializer.start()
        return cls(table, materializer, wait_timeout_ms, thread)


_state: ContextState | None = None
_lock = threading.Lock()


def get_state() -> ContextState:
    """Return this process's state, creating it on first use."""
    global _state
    with _lock:
        if _state is None:
            _state = ContextState.from_env()
        return _state
