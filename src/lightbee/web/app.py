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

import logging
import sys
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Final, TextIO

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from lightbee import __version__
from lightbee.config import Settings
from lightbee.udl.client import UdlReadClient
from lightbee.web.api import Services, Submit, install
from lightbee.web.guards import BodyLimit, RequestLimit
from lightbee.web.runs import ClientFactory
from lightbee.web.session import SessionStore

_LOG: Final = logging.getLogger("lightbee.web")


class AuditHandler(logging.StreamHandler[TextIO]):
    """Writes audit lines to the process's current standard output, message only."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        # Whatever stdout is now (setStream would flush the old one, which may be closed).
        self.stream = sys.stdout
        super().emit(record)


def configure_audit_log() -> None:
    """Make the audit lines reach the platform's log, whatever the server set up.

    uvicorn configures only its own loggers, so without this the root's default
    WARNING level would silently drop every INFO audit line. Safe to call twice.
    """
    audit = logging.getLogger("lightbee.audit")
    audit.setLevel(logging.INFO)
    if not any(isinstance(handler, AuditHandler) for handler in audit.handlers):
        audit.addHandler(AuditHandler())
    # Kept for tests and any root handler a developer adds; uvicorn adds none.
    audit.propagate = True


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


STATIC_ASSETS: Final = {
    "app.css": StaticAsset("app.css", "text/css; charset=utf-8"),
    "app.js": StaticAsset("app.js", "text/javascript; charset=utf-8"),
    "logic.mjs": StaticAsset("logic.mjs", "text/javascript; charset=utf-8"),
    "plotly-rules.css": StaticAsset("plotly-rules.css", "text/css; charset=utf-8"),
    "plotly.min.js": StaticAsset("vendor/plotly.min.js", "text/javascript; charset=utf-8"),
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
        except Exception:  # noqa: BLE001 - last-resort handler; detail logged, client told nothing
            # The detail stays in the server log; the client gets a generic
            # message that still carries every security header.
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
        return JSONResponse({"status": "ready", "version": __version__})


def _add_static_routes(app: FastAPI) -> None:
    index = _read_static("index.html")
    bodies = {key: _read_static(asset.filename) for key, asset in STATIC_ASSETS.items()}

    @app.get("/", include_in_schema=False)
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
        pool.submit(job)

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
    configure_audit_log()
    app.state.settings = settings
    app.state.sessions = SessionStore(
        idle_hours=settings.session_hours, max_sessions=settings.max_sessions
    )
    _add_security_headers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Outermost last: oversized bodies and excess requests are refused first.
    app.add_middleware(RequestLimit, headers=_SECURITY_HEADERS, store=app.state.sessions)
    app.add_middleware(BodyLimit, headers=_SECURITY_HEADERS)
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
