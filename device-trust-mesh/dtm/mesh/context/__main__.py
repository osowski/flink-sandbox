import os
from collections.abc import Mapping

import uvicorn

from dtm.mesh.context.server import build_api
from dtm.mesh.context.state import ContextState, get_state

DEFAULT_PORT = "8080"


def _port(env: Mapping[str, str]) -> int:
    raw = env.get("DTM_CONTEXT_PORT", DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"DTM_CONTEXT_PORT must be an integer from 1 to 65535, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"DTM_CONTEXT_PORT must be an integer from 1 to 65535, got {raw!r}")
    return port


def server_config(state: ContextState, env: Mapping[str, str]) -> uvicorn.Config:
    return uvicorn.Config(build_api(state), host="0.0.0.0", port=_port(env), access_log=False,
                          workers=1, timeout_graceful_shutdown=10, log_level="info")


def main() -> None:
    uvicorn.Server(server_config(get_state(), os.environ)).run()


if __name__ == "__main__":
    main()
