"""The HTTP surface: the page and its assets, the SSE stream, and a liveness probe."""
import asyncio
import mimetypes
from contextlib import asynccontextmanager
from importlib import resources
from typing import Callable

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from dtm.ui.hub import Hub

_INDEX = "index.html"


def _static_file(name: str):
    """Return the packaged static file called ``name``, or None for anything that is not a plain file name."""
    if not name or name.startswith(".") or "/" in name or "\\" in name or "\0" in name:
        return None
    entry = resources.files("dtm.ui").joinpath("static", name)
    return entry if entry.is_file() else None


def _file_response(name: str) -> Response:
    entry = _static_file(name)
    if entry is None:
        return Response("not found", status_code=404, media_type="text/plain")
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(entry.read_bytes(), media_type=media_type)


def build_app(hub: Hub, health: Callable[[], dict]) -> Starlette:
    async def index(request: Request) -> Response:
        return _file_response(_INDEX)

    async def static(request: Request) -> Response:
        return _file_response(request.path_params["name"])

    async def events(request: Request) -> Response:
        return EventSourceResponse(hub.subscribe())

    async def healthz(request: Request) -> Response:
        return JSONResponse(health())

    @asynccontextmanager
    async def lifespan(app: Starlette):
        hub.bind(asyncio.get_running_loop())
        yield

    return Starlette(routes=[Route("/", index), Route("/static/{name}", static),
                             Route("/events", events), Route("/healthz", healthz)],
                     lifespan=lifespan)
