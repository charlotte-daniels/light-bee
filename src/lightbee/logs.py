"""Every log line as one JSON object on standard output: the App Store standard.

The platform's logging check (blocking from 1 December 2026) reads standard
output only and expects each line to be one JSON object with these top-level
fields and no others: `timestamp` (RFC 3339 UTC, milliseconds),
`severity_text`, `severity_number` (the OpenTelemetry pairs TRACE 1, DEBUG 5,
INFO 9, WARN 13, ERROR 17, FATAL 21), `body` (a string) and `attributes` (a
flat object with OpenTelemetry keys). `trace_id` and `span_id` are optional
and not used here.

Lines written while a request is handled carry its `X-Request-Id` and the
platform gateway's `X-Auth-Request-Email` as attributes, through context
variables set by `RequestContext`. A background run copies the context of the
request that started it, so its lines carry the same request ID.

What never reaches a line: header values other than those two, cookies,
tokens, UDL credentials, query strings. Nothing names the service, tenant or
app: the platform adds those.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, TextIO

REQUEST_ID_ATTRIBUTE: Final = "http.request.header.x-request-id"
USER_EMAIL_ATTRIBUTE: Final = "user.email"
EXCEPTION_TYPE: Final = "exception.type"
EXCEPTION_MESSAGE: Final = "exception.message"
EXCEPTION_STACKTRACE: Final = "exception.stacktrace"

REQUEST_ID: Final[contextvars.ContextVar[str | None]] = contextvars.ContextVar(
    "lightbee_request_id", default=None
)
USER_EMAIL: Final[contextvars.ContextVar[str | None]] = contextvars.ContextVar(
    "lightbee_user_email", default=None
)

# Python level floor to the OpenTelemetry pair; a level between two takes the lower.
_SEVERITIES: Final = (
    (logging.CRITICAL, "FATAL", 21),
    (logging.ERROR, "ERROR", 17),
    (logging.WARNING, "WARN", 13),
    (logging.INFO, "INFO", 9),
    (logging.DEBUG, "DEBUG", 5),
)
_TRACE: Final = ("TRACE", 1)

# A request ID is an opaque correlation label: short, printable, no spaces.
_REQUEST_ID_SHAPE: Final = re.compile(r"[A-Za-z0-9._:/+=-]{1,128}")
# The gateway's email header, bounded and plain; anything else is not logged.
_EMAIL_SHAPE: Final = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,189}")


def severity(levelno: int) -> tuple[str, int]:
    """The OpenTelemetry severity text and number for a Python level."""
    for floor, text, number in _SEVERITIES:
        if levelno >= floor:
            return text, number
    return _TRACE


def _scalar(value: object) -> str | int | float | bool:
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


def flatten(values: Mapping[str, object], prefix: str = "") -> dict[str, str | int | float | bool]:
    """A nested mapping as one level of dotted keys with scalar values."""
    flat: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, f"{name}."))
        else:
            flat[name] = _scalar(value)
    return flat


class JsonFormatter(logging.Formatter):
    """One log record as one line of the App Store's JSON shape."""

    def format(self, record: logging.LogRecord) -> str:
        text, number = severity(record.levelno)
        attributes: dict[str, str | int | float | bool] = {
            "code.namespace": record.name,
            "code.function": record.funcName,
        }
        extra = getattr(record, "attributes", None)
        if isinstance(extra, Mapping):
            attributes.update(flatten(extra))
        request_id = REQUEST_ID.get()
        if request_id is not None:
            attributes[REQUEST_ID_ATTRIBUTE] = request_id
        email = USER_EMAIL.get()
        if email is not None:
            attributes[USER_EMAIL_ATTRIBUTE] = email
        if record.exc_info and record.exc_info[0] is not None:
            kind, error, trace = record.exc_info
            attributes[EXCEPTION_TYPE] = kind.__qualname__
            attributes[EXCEPTION_MESSAGE] = str(error)
            attributes[EXCEPTION_STACKTRACE] = "".join(
                traceback.format_exception(kind, error, trace)
            )
        stamp = datetime.fromtimestamp(record.created, UTC)
        line = {
            "timestamp": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "severity_text": text,
            "severity_number": number,
            "body": record.getMessage(),
            "attributes": attributes,
        }
        return json.dumps(line, ensure_ascii=True, separators=(",", ":"))


class StdoutHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever standard output is now (tests and servers replace it)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        # setStream would flush the old stream, which may already be closed.
        self.stream = sys.stdout
        super().emit(record)


# Loggers whose INFO and DEBUG lines carry full request URLs: uvicorn's access
# log (which uvicorn enables whenever a log configuration is passed) and the
# HTTP client libraries.
QUIET_LIBRARIES: Final = ("httpx2", "httpcore2", "httpx", "httpcore")
ACCESS_LOGGER: Final = "uvicorn.access"

# The uvicorn options entry: every logger, uvicorn's included, through one
# stdout handler and the JSON formatter. uvicorn applies this before it
# imports the app, so its start-up lines are JSON too.
LOG_CONFIG: Final[dict[str, Any]] = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"stdout": {"()": "lightbee.logs.StdoutHandler"}},
    "loggers": {
        "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
        **{name: {"level": "WARNING"} for name in QUIET_LIBRARIES},
        # Off outright: uvicorn decides to write access lines when this logger
        # has any handler, its own or a parent's through propagation.
        ACCESS_LOGGER: {"handlers": [], "level": "WARNING", "propagate": False},
    },
    "root": {"handlers": ["stdout"], "level": "INFO"},
}


def _log_uncaught(
    kind: type[BaseException], error: BaseException, trace: TracebackType | None
) -> None:
    logging.getLogger("lightbee").critical("uncaught exception", exc_info=(kind, error, trace))


def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None:
        _log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def _writes_to_a_standard_stream(handler: logging.Handler) -> bool:
    """A handler other than ours that prints to stdout or stderr (uvicorn's, basicConfig's)."""
    if isinstance(handler, StdoutHandler) or not isinstance(handler, logging.StreamHandler):
        return False
    stream = handler.stream
    return stream in (sys.stderr, sys.stdout, sys.__stderr__, sys.__stdout__)


def configure_logging() -> None:
    """Route every logger to one JSON stdout handler. Safe to call twice.

    The root logger gets the one handler; every other handler on the root or
    on uvicorn's loggers is removed, so nothing writes to standard error and
    nothing is printed twice. Warnings and uncaught exceptions are logged.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if _writes_to_a_standard_stream(handler):
            root.removeHandler(handler)
    if not any(isinstance(handler, StdoutHandler) for handler in root.handlers):
        root.addHandler(StdoutHandler())
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", ACCESS_LOGGER, "lightbee"):
        named = logging.getLogger(name)
        for handler in list(named.handlers):
            if _writes_to_a_standard_stream(handler):
                named.removeHandler(handler)
        named.propagate = name != ACCESS_LOGGER
    logging.getLogger("lightbee").setLevel(logging.INFO)
    # uvicorn's access lines and the HTTP client's request lines carry full
    # URLs, query strings included, at INFO; uvicorn's are off outright and
    # the client's held to warnings. `RequestContext` writes the request line.
    for name in (*QUIET_LIBRARIES, ACCESS_LOGGER):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught


def clean_request_id(raw: str | None) -> str:
    """The request's own ID when it is a plain label, otherwise a fresh one."""
    if raw is not None and _REQUEST_ID_SHAPE.fullmatch(raw):
        return raw
    return uuid.uuid4().hex


def clean_email(raw: str | None) -> str | None:
    """The gateway's email when it looks like one; nothing otherwise."""
    if raw is not None and _EMAIL_SHAPE.fullmatch(raw.strip()):
        return raw.strip()
    return None


# The five probe paths and static files are not logged: probes arrive every
# few seconds and touch nothing, and the page and its assets say nothing an
# operator needs.
QUIET_PATHS: Final = frozenset({"/", "/healthz", "/livez", "/readyz", "/ping"})
QUIET_PREFIX: Final = "/static/"
MAX_PATH_CHARS: Final = 256
_HTTP: Final = logging.getLogger("lightbee.http")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


def _single_header(scope: Scope, name: bytes) -> str | None:
    """A header sent exactly once; repeated, it is ignored (fail closed).

    Whether the gateway's value would come first or last is not known, so a
    request carrying two cannot choose which one is logged.
    """
    values = [value for key, value in scope.get("headers", []) if key == name]
    return values[0].decode("latin-1") if len(values) == 1 else None


class RequestContext:
    """Outermost ASGI middleware: request ID and email in context, one line per request.

    The line names the method, the path (never the query string), the status
    and the time taken. Probes and static files are not logged.
    """

    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _single_header(scope, b"x-request-id")
        email = _single_header(scope, b"x-auth-request-email")
        id_token = REQUEST_ID.set(clean_request_id(request_id))
        email_token = USER_EMAIL.set(clean_email(email))
        path = str(scope.get("path", ""))
        quiet = path in QUIET_PATHS or path.startswith(QUIET_PREFIX)
        status = 0
        started = time.perf_counter()

        async def observed(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observed)
        finally:
            if not quiet:
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                _HTTP.info(
                    "request",
                    extra={
                        "attributes": {
                            "http.request.method": str(scope.get("method", ""))[:16],
                            "url.path": path[:MAX_PATH_CHARS],
                            "http.response.status_code": status,
                            "lightbee.duration_ms": elapsed,
                        }
                    },
                )
            USER_EMAIL.reset(email_token)
            REQUEST_ID.reset(id_token)
