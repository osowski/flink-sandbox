"""Ray Serve entry point for context-mcp: ``dtm.mesh.context_mcp:app``."""
import os
import time

from fastapi import FastAPI
from ray import serve

from dtm.mesh.context.server import MAX_CONCURRENT_LOOKUPS, build_api
from dtm.mesh.context.state import ContextState, get_state

_DEFAULT_READY_TIMEOUT_S = 300.0
_POLL_S = 0.25


def _build() -> FastAPI:
    # Built inside the replica: the MCP app holds objects Ray cannot pickle.
    return build_api(get_state())


def wait_until_ready(state: ContextState, timeout_s: float) -> None:
    """Block until the materializer has caught up; raise on timeout or when its thread dies."""
    deadline = time.monotonic() + timeout_s
    while not state.materializer.ready.wait(min(_POLL_S, max(deadline - time.monotonic(), 0))):
        state.check_alive()
        if time.monotonic() >= deadline:
            raise RuntimeError(f"context tables not caught up within {timeout_s:g}s")


@serve.deployment(max_ongoing_requests=MAX_CONCURRENT_LOOKUPS)
@serve.ingress(_build)
class ContextMcp:
    def __init__(self):
        wait_until_ready(get_state(), float(os.environ.get("DTM_CONTEXT_READY_TIMEOUT_S",
                                                           _DEFAULT_READY_TIMEOUT_S)))

    def check_health(self) -> None:
        get_state().check_alive()


app = ContextMcp.bind()
