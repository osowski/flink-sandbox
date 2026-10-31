"""Ray Serve entry point for context-mcp: ``dtm.mesh.context_mcp:app``."""
import os

from fastapi import FastAPI
from ray import serve

from dtm.mesh.context.server import build_api
from dtm.mesh.context.state import get_state

_DEFAULT_READY_TIMEOUT_S = 300.0


def _build() -> FastAPI:
    # Built inside the replica: the MCP app holds objects Ray cannot pickle.
    return build_api(get_state())


@serve.deployment
@serve.ingress(_build)
class ContextMcp:
    def __init__(self):
        timeout = float(os.environ.get("DTM_CONTEXT_READY_TIMEOUT_S", _DEFAULT_READY_TIMEOUT_S))
        if not get_state().materializer.ready.wait(timeout):
            raise RuntimeError(f"context tables not caught up within {timeout:g}s")


app = ContextMcp.bind()
