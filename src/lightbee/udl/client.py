"""The read-only UDL client.

Structural guarantees, each asserted by a test:

- Every request is a GET with no body, issued from one private method.
- Every path is on a fixed allowlist of read endpoints, every parameter name on
  a fixed allowlist, and values are percent-encoded by the HTTP library.
- Every request goes to the configured HTTPS base URL; redirects are refused,
  never followed, and proxy or certificate settings are not taken from the
  environment, so nothing outside the validated settings can redirect traffic.
- A response body is streamed against a byte cap and a page may not exceed the
  record limit asked for; an oversized answer asks the caller to divide.
- Credentials never appear in an exception, its chain, a log line or a repr.

Service behaviour carried from the predecessor's live measurements:

- 429 means slow down: honour Retry-After (capped), retry, and halve the
  requests in flight once per concurrency level, so a burst of simultaneous
  429s halves the limit once rather than collapsing it to one.
- 500 to 504 on an archive read usually means the slice was too large; after
  the retries the caller is asked to divide the window. So is a timeout.
"""

from __future__ import annotations

import base64
import html
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import unquote

import httpx2

from lightbee.config import Settings

# Every endpoint Light Bee reads. Nothing else can be addressed.
ALLOWED_PATHS: Final = frozenset(
    {
        "/udl/eoobservation/tuple",  # live store, selected columns
        "/udl/eoobservation/history",  # archive, selected columns via `columns`
        "/udl/elset/tuple",  # elset history, selected columns
        # Satellite names for display (1.2). Both candidates are allowed until
        # the live check shows which one answers; the other is then removed.
        "/udl/onorbit/tuple",
        "/udl/onorbit",
    }
)
_ALLOWED_PARAMS: Final = frozenset(
    {"columns", "obTime", "epoch", "satNo", "dataMode", "source", "maxResults"}
)

PAGE_CAP: Final = 25_000
# A trimmed 25,000-record page is about 8 MB; anything past this is not a normal answer.
MAX_BODY_BYTES: Final = 16 * 1024 * 1024
MAX_BODY_CEILING: Final = 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_MAX_IN_FLIGHT: Final = 24
SERVER_ERROR_ATTEMPTS: Final = 3
RATE_LIMIT_ATTEMPTS: Final = 6
BACKOFF_SECONDS: Final = (1.0, 3.0, 5.0)
MAX_RETRY_AFTER_SECONDS: Final = 30.0
_OVERSIZED: Final = frozenset({500, 502, 503, 504})


class UdlError(Exception):
    """A UDL read failed. Messages never carry credentials or response bodies."""


class UdlAuthError(UdlError):
    """The UDL refused the analyst's credentials or their access to the data."""


class UdlRequestError(UdlError):
    """The request itself was refused (a client-side contract breach).

    `detail` holds a short, sanitised excerpt of the UDL's own explanation for
    a rejection, for diagnosis by developer tools. It is never part of the
    message, so it is never shown to an analyst or written to a log by default.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class UdlSliceTooLargeError(UdlError):
    """The window was too large to answer; read it again in smaller pieces."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """One analyst's UDL username and password, held in memory only."""

    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.username or not self.password:
            raise ValueError("a UDL username and password are both required")
        if ":" in self.username:
            raise ValueError("a UDL username cannot contain a colon")

    def authorization(self) -> str:
        """The HTTP Basic Authorization header value."""
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return f"Basic {token}"


class Throttle:
    """A shared limit on requests in flight, halved once per level on 429, never raised."""

    def __init__(self, limit: int = DEFAULT_MAX_IN_FLIGHT) -> None:
        if limit < 1:
            raise ValueError("the in-flight limit must be at least 1")
        self._limit = limit
        self._active = 0
        self._condition = threading.Condition()

    @property
    def limit(self) -> int:
        with self._condition:
            return self._limit

    def acquire(self) -> int:
        """Wait for a slot; return the limit in force when it was granted."""
        with self._condition:
            self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
            return self._limit

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def back_off(self, observed: int) -> None:
        """Halve the limit, unless another request already halved it from this level."""
        with self._condition:
            if self._limit == observed:
                self._limit = max(1, self._limit // 2)


@dataclass
class _Sent:
    status: int
    body: bytes
    retry_after: float | None
    limit_at_send: int
    preview: str = ""


PREVIEW_BYTES: Final = 1024
PREVIEW_CHARACTERS: Final = 300
REDACTED: Final = "[REDACTED]"
# Any piece this long of a secret is redacted wherever it appears.
FRAGMENT: Final = 8
# A long run of token characters; redacted when it mixes letters and digits
# or carries base64 or percent-encoding characters.
_OPAQUE: Final = re.compile(r"[A-Za-z0-9+/_%=-]{16,}")
_UNICODE_ESCAPE: Final = re.compile(r"\\u([0-9a-fA-F]{4})")
_BACKSLASH_ESCAPE: Final = re.compile(r"\\(.)")
# Credential-shaped text a server or gateway might echo back: an auth scheme
# and its token, or a credential-named field and its value.
_CREDENTIAL_TEXT: Final = (
    re.compile(r"(?i)\b(basic|bearer|digest)\s+[^\s,;\"']+"),
    re.compile(
        r"(?i)(authorization|password|passwd|pwd|secret|token|api[-_]?key|credential)"
        r"[^\s:=]{0,20}[\"']?\s*[:=]\s*[\"']?[^\s,;\"'&]+"
    ),
)


class UdlReadClient:
    """A GET-only client for the allowlisted UDL read endpoints."""

    def __init__(
        self,
        settings: Settings,
        credentials: Credentials,
        *,
        transport: httpx2.BaseTransport | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_body_bytes: int = MAX_BODY_BYTES,
        trust_env: bool = False,
    ) -> None:
        self.throttle = throttle or Throttle()
        self.rate_limited = 0
        if not 1 <= max_body_bytes <= MAX_BODY_CEILING:
            raise ValueError("max_body_bytes must be between 1 byte and 1 GiB")
        self._sleep = sleep
        self._max_body_bytes = max_body_bytes
        # What must never appear in a rejection excerpt, longest first so a
        # shorter secret inside a longer one cannot leave part of it behind.
        self._secrets = _secrets_of(credentials)
        self._counter = threading.Lock()
        self._http = httpx2.Client(
            base_url=settings.udl_base_url,
            headers={"Authorization": credentials.authorization(), "Accept": "application/json"},
            timeout=httpx2.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=15.0),
            follow_redirects=False,
            # Off for the app: proxy and certificate settings come only from
            # validated configuration. The live check may opt in on a laptop.
            trust_env=trust_env,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection pool."""
        self._http.close()

    def read(self, path: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        """GET an allowlisted endpoint and return its records, retrying politely."""
        _check_request(path, params)
        limit = _page_limit(params)
        status = 0
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            sent = self._send(path, params)
            status = sent.status
            outcome = _classify(status)
            if outcome == "ok":
                return _records(sent.body, limit)
            if outcome == "rejected":
                raise UdlRequestError(f"the UDL rejected the request (HTTP {status})", sent.preview)
            if outcome == "oversized" and attempt + 1 >= SERVER_ERROR_ATTEMPTS:
                raise UdlSliceTooLargeError(f"the UDL could not answer this window (HTTP {status})")
            if outcome == "slow-down":
                self._note_rate_limit(sent.limit_at_send)
            self._sleep(_wait(attempt, sent.retry_after))
        raise UdlError(f"the UDL kept refusing the request (HTTP {status})")

    def _note_rate_limit(self, observed: int) -> None:
        with self._counter:
            self.rate_limited += 1
        self.throttle.back_off(observed)

    def _send(self, path: str, params: Mapping[str, str]) -> _Sent:
        """The one place a request is sent. Always GET, never a body."""
        limit = self.throttle.acquire()
        failure = ""
        try:
            with self._http.stream("GET", path, params=dict(params)) as response:
                retry_after = _retry_after(response.headers.get("Retry-After"))
                status = response.status_code
                ok = 200 <= status < 300
                body = _bounded_body(response, self._max_body_bytes) if ok else b""
                rejected = _classify(status) == "rejected"
                preview = _preview(response, self._secrets) if rejected else ""
                return _Sent(status, body, retry_after, limit, preview)
        except httpx2.TimeoutException:
            failure = "timeout"
        except httpx2.HTTPError as exc:
            failure = type(exc).__name__
        finally:
            self.throttle.release()
        # Raised outside the except blocks so no reference to the library's
        # exception, which holds the request and its Authorization header,
        # survives in __context__.
        if failure == "timeout":
            raise UdlSliceTooLargeError("the UDL did not answer in time")
        raise UdlError(f"could not reach the UDL ({failure})")


def _bounded_body(response: httpx2.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise UdlSliceTooLargeError("the UDL response was larger than the safe limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_after(value: str | None) -> float | None:
    text = (value or "").strip()
    if not (text.isascii() and text.isdigit()):
        return None
    return min(float(text), MAX_RETRY_AFTER_SECONDS)


def _wait(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def _page_limit(params: Mapping[str, str]) -> int:
    raw = params.get("maxResults", str(PAGE_CAP))
    if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) <= PAGE_CAP:
        raise UdlRequestError(f"maxResults must be between 1 and {PAGE_CAP}")
    return int(raw)


def _check_request(path: str, params: Mapping[str, str]) -> None:
    if path not in ALLOWED_PATHS:
        raise UdlRequestError("that UDL endpoint is not on the read allowlist")
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UdlRequestError(f"unsupported UDL query parameter(s): {sorted(unknown)}")


def _classify(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        raise UdlRequestError("the UDL answered with a redirect, which is refused")
    if status in (401, 403):
        raise UdlAuthError("the UDL did not accept these credentials for this data")
    if status == 429:
        return "slow-down"
    if status in _OVERSIZED:
        return "oversized"
    return "rejected"


def _secrets_of(credentials: Credentials) -> tuple[str, ...]:
    header = credentials.authorization()
    pair = f"{credentials.username}:{credentials.password}"
    candidates = {
        header,
        header.removeprefix("Basic "),
        pair,
        credentials.username,
        credentials.password,
    }
    return tuple(sorted((c for c in candidates if len(c) >= 3), key=len, reverse=True))


def _decoded(text: str) -> str:
    """The text with URL, HTML and JSON escaping undone, twice for double encoding."""
    for _ in range(2):
        text = html.unescape(unquote(text))
        text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
        text = _BACKSLASH_ESCAPE.sub(r"\1", text).replace("\xa0", " ")
    return text


def _secret_spans(text: str, secrets: tuple[str, ...]) -> list[tuple[int, int]]:
    """Where any secret, or any FRAGMENT-character piece of a long one, appears."""
    spans = []
    for secret in secrets:
        width = min(len(secret), FRAGMENT)
        for piece in {secret[i : i + width] for i in range(len(secret) - width + 1)}:
            start = text.find(piece)
            while start != -1:
                spans.append((start, start + width))
                start = text.find(piece, start + 1)
    return spans


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials from text an upstream sent back.

    The text is decoded first, so an echo that is URL-encoded, HTML-escaped or
    JSON-escaped is seen as it really is. Then any secret, any 8-character
    fragment of one (so a token with its padding stripped cannot pass), any
    credential-shaped field, and any long opaque run that mixes letters and
    digits (which a rejection message never needs) is replaced.
    """
    text = _decoded(text)
    spans = sorted(_secret_spans(text, secrets))
    merged: list[list[int]] = []
    for low, high in spans:
        if merged and low <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    for low, high in reversed(merged):
        text = text[:low] + REDACTED + text[high:]
    for pattern in _CREDENTIAL_TEXT:
        text = pattern.sub(REDACTED, text)
    return _OPAQUE.sub(lambda m: REDACTED if _looks_opaque(m.group()) else m.group(), text)


def _looks_opaque(run: str) -> bool:
    has_letter = any(c.isalpha() for c in run)
    has_digit = any(c.isdigit() for c in run)
    return (has_letter and has_digit) or any(c in "+/=%" for c in run)


def _preview(response: httpx2.Response, secrets: tuple[str, ...] = ()) -> str:
    """The start of a rejection's body, credentials removed, as printable ASCII.

    For diagnosis only. A body cut short at the byte limit loses its last
    word first, so a secret split by the cut cannot survive in part.
    """
    collected = b""
    for chunk in response.iter_bytes():
        collected += chunk
        if len(collected) > PREVIEW_BYTES:
            break
    cut = len(collected) > PREVIEW_BYTES
    text = collected[:PREVIEW_BYTES].decode("utf-8", errors="replace")
    if cut:
        text = text[: max(text.rfind(" "), 0)]
    printable = "".join(c if " " <= c <= "~" else " " for c in redact(text, secrets))
    return " ".join(printable.split())[:PREVIEW_CHARACTERS]


def _records(body: bytes, limit: int) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        raise UdlError("the UDL returned something that is not usable JSON") from None
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise UdlError("the UDL returned JSON that is not a list of records")
    if len(parsed) > limit:
        raise UdlError("the UDL returned more records than were asked for")
    return parsed
