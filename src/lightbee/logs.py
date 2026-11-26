"""Every log line as one JSON object on standard output: the App Store standard.

The platform's logging check (blocking from 1 December 2026) reads standard
output only and expects each line to be one JSON object with these top-level
fields and no others: `timestamp` (RFC 3339 UTC, milliseconds),
`severity_text`, `severity_number` (the OpenTelemetry pairs TRACE 1, DEBUG 5,
INFO 9, WARN 13, ERROR 17, FATAL 21), `body` (a string) and `attributes` (a
flat object with OpenTelemetry keys). `trace_id` and `span_id` are optional
and not used here.

Lines written while a request is handled carry its `X-Request-Id`, the
platform gateway's `X-Auth-Request-Email`, its path (never the query string),
method and `User-Agent` as attributes, through context variables set by
`RequestContext`. A background run copies the context of the request that
started it, so its lines carry the same request fields. One audit line per
completed request adds the status, `event.outcome` and, when a route matched,
`http.route` (the platform's requirement as reported on DRIFTER's 2.0.3
upload, 25 November 2026; ported from DRIFTER).

Every line stays under 64 KiB: long values are cut, the required fields never.

What never reaches a line: header values other than those three, cookies,
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
# Every line written while a request is handled names the request: its path
# (never the query string), method and user agent, as the standard requires.
REQUEST_FIELDS: Final[contextvars.ContextVar[dict[str, str] | None]] = contextvars.ContextVar(
    "lightbee_request_fields", default=None
)

# A line must stay under 64 KiB; long values are cut, required fields never.
# Each required field is bounded where it is cleaned (256 characters at most),
# so these caps never reach one. A value can grow up to sixfold when escaped
# (ensure_ascii writes a non-ASCII character as \uXXXX), so the encoded line
# is measured too and the longest cuttable value halved until it fits.
MAX_VALUE_CHARS: Final = 16_000
MAX_BODY_CHARS: Final = 8_000
MAX_LINE_BYTES: Final = 60 * 1024
REQUIRED_ATTRIBUTES: Final = frozenset(
    {
        REQUEST_ID_ATTRIBUTE,
        USER_EMAIL_ATTRIBUTE,
        "url.path",
        "http.request.method",
        "user_agent.original",
        "http.response.status_code",
        "event.outcome",
        "http.route",
    }
)
_CUT_MARK: Final = "...[cut]"

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
        fields = REQUEST_FIELDS.get()
        if fields is not None:
            # Assigned, not defaulted: a caller's `extra` never replaces a request field.
            attributes.update(fields)
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
            "body": _cut(record.getMessage(), MAX_BODY_CHARS),
            "attributes": {key: _bounded(key, value) for key, value in attributes.items()},
        }
        return _fit(line)


def _bounded(key: str, value: Any) -> Any:
    """A string attribute cut to its bound; a required key is bounded tighter (ours
    are at most 256 characters where cleaned, so this only touches one a caller put
    in `extra`). Anything else is kept as it is."""
    if not isinstance(value, str):
        return value
    limit = MAX_PATH_CHARS if key in REQUIRED_ATTRIBUTES else MAX_VALUE_CHARS
    return _cut(value, limit)


def _cut(value: str, limit: int) -> str:
    """The value, or its first characters and a mark when it is longer than the limit."""
    return value if len(value) <= limit else value[: max(limit - len(_CUT_MARK), 0)] + _CUT_MARK


def _encode(line: Mapping[str, object]) -> str:
    return json.dumps(line, ensure_ascii=True, separators=(",", ":"))


def _fit(line: dict[str, Any]) -> str:
    """The line as JSON, its longest cuttable value halved until it is under the cap.

    ensure_ascii makes the text pure ASCII, so its length is its size in bytes.
    The body and every attribute outside `REQUIRED_ATTRIBUTES` may be cut.
    """
    text = _encode(line)
    attributes: dict[str, Any] = line["attributes"]
    while len(text) >= MAX_LINE_BYTES:
        cuttable = [
            (len(value), key)
            for key, value in attributes.items()
            if isinstance(value, str) and key not in REQUIRED_ATTRIBUTES
        ]
        longest, key = max(cuttable, default=(0, ""))
        if len(line["body"]) >= longest and len(line["body"]) > len(_CUT_MARK):
            line["body"] = _cut(line["body"], len(line["body"]) // 2)
        elif longest > len(_CUT_MARK):
            attributes[key] = _cut(attributes[key], longest // 2)
        else:
            # Many short attributes and nothing long left to cut: keep only the
            # required fields. Each is at most 256 characters, so eight escaped
            # are under 13 KiB and this fits.
            line["attributes"] = {
                key: value for key, value in attributes.items() if key in REQUIRED_ATTRIBUTES
            }
            line["attributes"]["lightbee.attributes_dropped"] = True
            return _encode(line)
        text = _encode(line)
    return text


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


def _drop_printers(logger: logging.Logger) -> None:
    """Remove the logger's handlers that print to a standard stream.

    The handlers to drop are chosen first, so the list is never changed while
    it is being read.
    """
    printers = [handler for handler in logger.handlers if _writes_to_a_standard_stream(handler)]
    for handler in printers:
        logger.removeHandler(handler)


def configure_logging() -> None:
    """Route every logger to one JSON stdout handler. Safe to call twice.

    The root logger gets the one handler; every other handler on the root or
    on uvicorn's loggers is removed, so nothing writes to standard error and
    nothing is printed twice. Warnings and uncaught exceptions are logged.
    """
    root = logging.getLogger()
    _drop_printers(root)
    if not any(isinstance(handler, StdoutHandler) for handler in root.handlers):
        root.addHandler(StdoutHandler())
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", ACCESS_LOGGER, "lightbee"):
        named = logging.getLogger(name)
        _drop_printers(named)
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


_AGENT_SHAPE: Final = re.compile(r"[\x20-\x7e]{1,256}")


def clean_user_agent(raw: str | None) -> str:
    """The client's User-Agent, printable and bounded; "unknown" otherwise.

    Always present, because the standard requires it on every request line.
    """
    if raw is not None:
        value = raw.strip()[:256]
        if _AGENT_SHAPE.fullmatch(value):
            return value
    return "unknown"


_NOT_PRINTABLE: Final = re.compile(r"[^\x20-\x7e]")


def clean_path(raw: str) -> str:
    """The decoded path, bounded, with every character outside printable ASCII as `?`.

    The path is on every request line, so a control character or a
    bidirectional override in it must not reach a log viewer.
    """
    return _NOT_PRINTABLE.sub("?", raw[:MAX_PATH_CHARS])


def outcome(status: int) -> str:
    """The OpenTelemetry `event.outcome` for a response status (0: none was sent)."""
    if status == 0:
        return "unknown"
    return "success" if status < 400 else "failure"


# The five probe paths and static files are not logged when they arrive
# untagged: probes come every few seconds and touch nothing, and the page and
# its assets say nothing an operator needs. A request that carries an
# X-Request-Id is always logged, wherever it goes, because someone (the
# platform's logging check, an operator tracing a call) asked to follow it.
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

    Every line written while the request is handled names its method, path
    (never the query string) and user agent. The one audit line per request
    adds the status, `event.outcome`, the matched route and the time taken.
    Untagged probes and static files are not logged.
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
        fields_token = REQUEST_FIELDS.set(
            {
                "url.path": clean_path(path),
                "http.request.method": str(scope.get("method", ""))[:16],
                "user_agent.original": clean_user_agent(_single_header(scope, b"user-agent")),
            }
        )
        quiet = request_id is None and (path in QUIET_PATHS or path.startswith(QUIET_PREFIX))
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
                audit: dict[str, object] = {
                    "event.name": "http.request.completed",
                    "http.response.status_code": status,
                    "event.outcome": outcome(status),
                    "lightbee.duration_ms": elapsed,
                }
                route = getattr(scope.get("route"), "path", None)
                if isinstance(route, str):
                    audit["http.route"] = route[:MAX_PATH_CHARS]
                _HTTP.info("request", extra={"attributes": audit})
            REQUEST_FIELDS.reset(fields_token)
            USER_EMAIL.reset(email_token)
            REQUEST_ID.reset(id_token)
