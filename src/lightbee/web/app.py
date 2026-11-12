"""The application factory.

`create_app(settings)` wires routes and middleware and returns the app without
listening, so every route is testable in-process. `src/asgi.py` is the only
place the real settings are loaded.

Three platform rules, each of which cost a sister application a cycle:

- `/`, `/healthz`, `/readyz`, `/livez` and `/ping` answer 200 unauthenticated
  and touch nothing. Readiness reports its state in the body and keeps the 200,
  so a fault stays visible instead of taking the pod off the balancer silently.
- Static assets are selected by dictionary key, never by a path built from a
  request string, so no request can name a file outside the allowlist.
- No interactive API documentation is served; the surface is only what the
  interface uses.
"""

from __future__ import annotations

import contextvars
import logging
import resource
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from lightbee import __version__
from lightbee.config import Settings
from lightbee.logs import RequestContext, configure_logging
from lightbee.udl.client import UdlReadClient
from lightbee.web.api import Services, Submit, install
from lightbee.web.guards import BodyLimit, RequestLimit
from lightbee.web.runs import ClientFactory
from lightbee.web.session import SessionStore

_LOG: Final = logging.getLogger("lightbee.web")

# When this module was first imported: the process start, near enough.
PROCESS_STARTED: Final = time.time()


def peak_memory_mb() -> float:
    """The process's peak resident memory in MiB (Linux reports kibibytes; MB in the name)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


# Background runs at once. Two analysts a day; four leaves room without letting
# runs crowd out the event loop's thread pool.
RUN_WORKERS: Final = 4

# Scripts and styles are same-origin only, with no inline style or eval
# allowance: Plotly runs under this policy (browser-tested, 11 November 2026)
# given a same-origin stylesheet to write its rules into and SVG traces only
# (its WebGL traces need eval and are not used). Images allow data: and blob:
# because the browser-side PNG export produces them.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

_SECURITY_HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000",
}


@dataclass(frozen=True, slots=True)
class StaticAsset:
    """One file the interface may request, and how to serve it."""

    filename: str
    media_type: str


_CSS: Final = "text/css; charset=utf-8"
_JAVASCRIPT: Final = "text/javascript; charset=utf-8"

STATIC_ASSETS: Final = {
    "app.css": StaticAsset("app.css", _CSS),
    "app.js": StaticAsset("app.js", _JAVASCRIPT),
    "logic.mjs": StaticAsset("logic.mjs", _JAVASCRIPT),
    "plotly-rules.css": StaticAsset("plotly-rules.css", _CSS),
    "plotly.min.js": StaticAsset("vendor/plotly.min.js", _JAVASCRIPT),
}


def _read_static(filename: str) -> bytes:
    return resources.files("lightbee.web").joinpath("static", filename).read_bytes()


def _with_headers(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _add_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            response = await call_next(request)
        # A last-resort handler, so it catches everything (ruff BLE001): the
        # detail stays in the server log; the client gets a generic message
        # that still carries every security header.
        except Exception:  # noqa: BLE001
            _LOG.exception("unhandled error while serving a request")
            response = PlainTextResponse("Internal Server Error", status_code=500)
        return _with_headers(response)


def _add_health_routes(app: FastAPI) -> None:
    # Async, so the probes are answered on the event loop and can never queue
    # behind blocking work in the threadpool (a hung probe is a silent kill).
    @app.get("/healthz", include_in_schema=False)
    @app.get("/livez", include_in_schema=False)
    @app.get("/ping", include_in_schema=False)
    async def alive() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> JSONResponse:
        # Numbers only: a later start time, or a peak that has dropped, shows
        # over HTTP that the container restarted.
        return JSONResponse(
            {
                "status": "ready",
                "version": __version__,
                "started_unix": int(PROCESS_STARTED),
                "uptime_seconds": int(time.time() - PROCESS_STARTED),
                "peak_memory_mb": peak_memory_mb(),
            }
        )


def _add_static_routes(app: FastAPI) -> None:
    index = _read_static("index.html")
    bodies = {key: _read_static(asset.filename) for key, asset in STATIC_ASSETS.items()}

    # HEAD as well as GET: a probe or uptime monitor may ask for headers only.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def root() -> Response:
        return Response(index, media_type="text/html; charset=utf-8")

    @app.get("/static/{key}", include_in_schema=False)
    def static(key: str) -> Response:
        asset = STATIC_ASSETS.get(key)
        if asset is None:
            return PlainTextResponse("not found", status_code=404)
        return Response(
            bodies[key],
            media_type=asset.media_type,
            headers={"Cache-Control": "public, max-age=300"},
        )


def _default_submit() -> Submit:
    pool = ThreadPoolExecutor(max_workers=RUN_WORKERS, thread_name_prefix="lightbee-run")

    def submit(job: Callable[[], None]) -> None:
        # The job runs in the starting request's context, so its log lines
        # carry that request's ID.
        pool.submit(contextvars.copy_context().run, job)

    return submit


def create_app(
    settings: Settings,
    *,
    client_factory: ClientFactory | None = None,
    submit: Submit | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the Light Bee application without starting a server.

    The UDL client factory, the background-job runner and the clock can be
    replaced, so tests drive the whole service against a fake UDL in-process.
    """
    app = FastAPI(
        title="Light Bee",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # A trailing-slash redirect would reflect the Host header into a
        # Location and downgrade it to http behind the TLS ingress.
        redirect_slashes=False,
    )
    configure_logging()
    app.state.settings = settings
    app.state.sessions = SessionStore(
        idle_hours=settings.session_hours, max_sessions=settings.max_sessions
    )
    _add_security_headers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Outermost last: oversized bodies and excess requests are refused first.
    app.add_middleware(RequestLimit, headers=_SECURITY_HEADERS, store=app.state.sessions)
    app.add_middleware(BodyLimit, headers=_SECURITY_HEADERS)
    # Outermost of all, so even a refused request's lines carry its ID.
    app.add_middleware(RequestContext)
    _add_health_routes(app)
    _add_static_routes(app)
    install(
        app,
        Services(
            store=app.state.sessions,
            make_client=client_factory or (lambda creds: UdlReadClient(settings, creds)),
            submit=submit or _default_submit(),
            clock=clock or (lambda: datetime.now(UTC)),
        ),
    )
    return app
