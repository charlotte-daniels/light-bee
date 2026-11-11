"""Two-tier rate limits: per session and across the whole service.

Sliding windows held in memory. A request is admitted only if both the
session's own limit and the service-wide limit have room, so one analyst cannot
starve another and the UDL is never hit harder than the service as a whole
allows.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Limit:
    per_session: int
    service_wide: int
    period_seconds: float


RUNS: Final = Limit(per_session=20, service_wide=60, period_seconds=3600.0)
SCANS: Final = Limit(per_session=120, service_wide=600, period_seconds=3600.0)
# Failed sign-ins, keyed by a hash of the username. Only failures count, and a
# username that has signed in successfully is not held back by the shared tier,
# so random usernames cannot lock returning analysts out.
SIGN_IN_FAILURES: Final = Limit(per_session=10, service_wide=100, period_seconds=900.0)
# Every request from a live session, keyed by that session. Requests without a
# live session are cheap (a 401 or a sign-in, which has its own limit) and are
# not counted, so anonymous traffic can never use up signed-in analysts' room.
REQUESTS: Final = Limit(per_session=600, service_wide=3000, period_seconds=60.0)
# Bounds the memory the limiter itself can hold for per-session windows.
MAX_TRACKED_KEYS: Final = 256


class RateLimiter:
    """Sliding-window counters for one kind of request."""

    def __init__(self, limit: Limit, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._service: deque[float] = deque()
        self._keys: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def admit(self, key: str) -> bool:
        """Record and admit one request for `key`, or refuse it without recording."""
        with self._lock:
            now = self._clock()
            if self._full(key, now, shared=True):
                return False
            self._add(key, now)
            return True

    def blocked(self, key: str, *, shared: bool = True) -> bool:
        """Whether `key` has used its room (and, if `shared`, the service's)."""
        with self._lock:
            return self._full(key, self._clock(), shared=shared)

    def record(self, key: str) -> None:
        """Count one event for `key` without asking whether there was room."""
        with self._lock:
            now = self._clock()
            self._full(key, now, shared=False)  # trims and bounds the windows
            self._add(key, now)

    def _full(self, key: str, now: float, *, shared: bool) -> bool:
        horizon = now - self._limit.period_seconds
        _trim(self._service, horizon)
        mine = self._keys.get(key)
        if mine is None:
            if len(self._keys) >= MAX_TRACKED_KEYS:
                self._forget_idle(horizon)
            return shared and len(self._service) >= self._limit.service_wide
        _trim(mine, horizon)
        return len(mine) >= self._limit.per_session or (
            shared and len(self._service) >= self._limit.service_wide
        )

    def _add(self, key: str, now: float) -> None:
        self._keys.setdefault(key, deque()).append(now)
        self._service.append(now)

    def _forget_idle(self, horizon: float) -> None:
        for key in [k for k, d in self._keys.items() if not d or d[-1] <= horizon]:
            del self._keys[key]
        while len(self._keys) >= MAX_TRACKED_KEYS:
            self._keys.pop(next(iter(self._keys)))


def _trim(window: deque[float], horizon: float) -> None:
    while window and window[0] <= horizon:
        window.popleft()
