"""Request guards that run before any route: a body size cap and a broad rate limit.

Both are plain ASGI middleware, so an oversized or excess request is refused
before routing, validation or JSON parsing spend any memory on it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any, Final

from starlette.requests import cookie_parser

from lightbee.web import limits
from lightbee.web.session import COOKIE_NAME, SessionStore

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

# The largest valid body (50,000 selected times) is about 750 KB.
MAX_BODY_BYTES: Final = 1024 * 1024
# The page and its assets are cheap and must always answer, as must the probes.
UNLIMITED_PATHS: Final = frozenset({"/", "/healthz", "/livez", "/readyz", "/ping"})
UNLIMITED_PREFIX: Final = "/static/"


async def _refuse(send: Send, status: int, message: str, headers: Mapping[str, str]) -> None:
    body = json.dumps({"error": message}).encode()
    pairs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    pairs += [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    await send({"type": "http.response.start", "status": status, "headers": pairs})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Refuse a request body over the cap, whether declared or streamed."""

    def __init__(
        self, app: App, headers: Mapping[str, str], max_bytes: int = MAX_BODY_BYTES
    ) -> None:
        self.app = app
        self.headers = headers
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await _refuse(send, 413, "The request is too large.", self.headers)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await _refuse(send, 413, "The request is too large.", self.headers)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replayed() -> Message:
            return replay.pop() if replay else await receive()

        await self.app(scope, replayed, send)


class RequestLimit:
    """A broad limit on every request from a live session, per session and in total.

    Only cookies naming a session the store holds are counted, so random
    cookie values cannot open fresh allowances or use up the analysts' shared
    room. A request carrying more than one session cookie is refused, so the
    cookie counted here is always the one the routes use.
    """

    def __init__(self, app: App, headers: Mapping[str, str], store: SessionStore) -> None:
        self.app = app
        self.headers = headers
        self.store = store
        self.limiter = limits.RateLimiter(limits.REQUESTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path in UNLIMITED_PATHS or path.startswith(UNLIMITED_PREFIX):
            await self.app(scope, receive, send)
            return
        tokens = session_tokens(scope)
        if len(tokens) > 1:
            await _refuse(send, 400, "Request refused.", self.headers)
            return
        session = self.store.get(tokens[0]) if tokens else None
        if session is not None and not self.limiter.admit(session.key):
            await _refuse(send, 429, "Too many requests; slow down.", self.headers)
            return
        await self.app(scope, receive, send)


def session_tokens(scope: Scope) -> list[str]:
    """Every session cookie value the request carries, across all Cookie headers.

    Each part is read by Starlette's own parser, so every spelling the routes
    would accept (spaces around the name, a quoted value) is seen and counted
    here too.
    """
    found: list[str] = []
    for name, value in scope.get("headers", []):
        if name != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            found += [v for k, v in cookie_parser(part).items() if k == COOKIE_NAME]
    return found
