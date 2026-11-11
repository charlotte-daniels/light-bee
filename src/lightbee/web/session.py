"""Analyst sessions, held in this process's memory only.

A session holds one analyst's UDL credentials for the length of their working
day and nothing else of theirs outlives it. The browser holds a random 256-bit
token in a `__Host-` cookie (Secure, HttpOnly, SameSite=Strict, path /, no
domain); the server keys sessions by the token's SHA-256, so the token itself
is never stored. A session ends on sign-out, after the configured idle time,
or when the process restarts (an accepted risk: the analyst signs in again).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from lightbee.udl.client import Credentials

if TYPE_CHECKING:
    from lightbee.web.runs import Run

COOKIE_NAME: Final = "__Host-lightbee"
TOKEN_BYTES: Final = 32
# Objects whose data a session keeps for warm re-runs; the oldest is dropped first.
CACHED_OBJECTS: Final = 2


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", errors="replace")).hexdigest()


@dataclass
class Session:
    """One signed-in analyst. Credentials never leave this object except to a UDL client."""

    key: str  # SHA-256 of the cookie token
    credentials: Credentials = field(repr=False)
    last_seen: float
    run: Run | None = None  # the analyst's current or last run
    ended: bool = False
    scanning: bool = False
    cache: OrderedDict[tuple[Any, ...], Any] = field(default_factory=OrderedDict, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def audit_id(self) -> str:
        """A short, non-reversible label for audit lines."""
        return self.key[:12]

    def remember(self, cache_key: tuple[Any, ...], data: Any) -> None:
        with self.lock:
            if self.ended:
                return  # a run finishing after sign-out must not refill the session
            self.cache[cache_key] = data
            self.cache.move_to_end(cache_key)
            while len(self.cache) > CACHED_OBJECTS:
                self.cache.popitem(last=False)

    def recall(self, cache_key: tuple[Any, ...]) -> Any:
        with self.lock:
            data = self.cache.get(cache_key)
            if data is not None:
                self.cache.move_to_end(cache_key)
            return data


class SessionLimitError(Exception):
    """Every session slot is in use."""


class SessionStore:
    """All live sessions, with idle expiry and a hard cap."""

    def __init__(
        self, *, idle_hours: int, max_sessions: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._idle_seconds = idle_hours * 3600.0
        self._max = max_sessions
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, credentials: Credentials) -> tuple[str, Session]:
        """A new session and the token for its cookie."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = self._clock()
        with self._lock:
            self._sweep(now)
            if len(self._sessions) >= self._max:
                raise SessionLimitError
            session = Session(token_hash(token), credentials, now)
            self._sessions[session.key] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        """The live session for a cookie token, refreshed; None if absent or expired."""
        if not token or len(token) > 128:
            return None
        now = self._clock()
        key = token_hash(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if now - session.last_seen > self._idle_seconds:
                self._end(key)
                return None
            session.last_seen = now
            return session

    def remove(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._end(token_hash(token))

    def __len__(self) -> int:
        with self._lock:
            self._sweep(self._clock())
            return len(self._sessions)

    def _sweep(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if now - s.last_seen > self._idle_seconds]:
            self._end(key)

    def _end(self, key: str) -> None:
        """Drop a session and everything it held, so nothing of it outlives sign-out."""
        session = self._sessions.pop(key, None)
        if session is not None:
            with session.lock:
                session.ended = True
                session.cache.clear()
                if session.run is not None:
                    session.run.cancel.set()
                session.run = None

    def enforce_budget(self, budget_bytes: int, keep: Session) -> None:
        """Drop the oldest sessions' cached data and finished runs' data until under budget.

        `keep` is the session whose run just finished; its own data goes last.
        """
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda s: (s is keep, s.last_seen))
        total = sum(_footprint(s) for s in sessions)
        for session in sessions:
            if total <= budget_bytes:
                return
            with session.lock:
                before = _footprint_locked(session)
                session.cache.clear()
                if session.run is not None and session.run.finished:
                    # Kept, so the analyst's next request explains what happened.
                    session.run.release()
                total -= before - _footprint_locked(session)


def _footprint(session: Session) -> int:
    with session.lock:
        return _footprint_locked(session)


def _footprint_locked(session: Session) -> int:
    held = {id(data): data for data in session.cache.values()}
    if session.run is not None and session.run.data is not None:
        held[id(session.run.data)] = session.run.data
    return sum(getattr(data, "nbytes", 0) for data in held.values())
