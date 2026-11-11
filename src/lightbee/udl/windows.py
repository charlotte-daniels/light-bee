"""Reading a long UDL time window as many parallel slices, never a partial answer.

The UDL has no paging and no offset, so a window is divided rather than paged.
Carried from the predecessor's live measurements:

- The service is bound by records, not requests: about 7,000 records a slice
  with up to 24 in flight was the measured best, so the slice length is sized
  to that target from a probe of the most recent week.
- The probe samples the RECENT end, because coverage of an object usually
  starts part-way into a long history; an empty probe gets a cautious slice.
  It is itself read as PROBE_PIECES parallel slices: the UDL returns a few
  hundred records a second per request, so a dense week read as one request
  took over a minute in the owner's service check (11 November 2026).
- A slice that comes back at the page cap, or that times out or errors as too
  large, is split in two and both halves re-read at once. A slice that cannot
  be split further fails the read: a silently truncated history would draw a
  confident, wrong picture.

Each slice is handed to a transform as soon as it lands, in the worker that
read it, so raw records never accumulate: only the transformed chunks do.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Generic, TypeVar

from lightbee.udl.client import PAGE_CAP, UdlError, UdlSliceTooLargeError

TARGET_RECORDS_PER_SLICE: Final = 7_000
# A short read is cut finer, down to this many records a slice, so it still
# uses every request in flight: a request's time grows with its records.
MIN_RECORDS_PER_SLICE: Final = 1_000
PROBE_SPAN: Final = timedelta(days=7)
MIN_SLICE: Final = timedelta(minutes=30)
MAX_SLICE: Final = timedelta(days=30)
CAUTIOUS_SLICE: Final = timedelta(days=7)
PROBE_PIECES: Final = 8

T = TypeVar("T")
Window = tuple[datetime, datetime]
Fetch = Callable[[datetime, datetime], list[dict[str, Any]]]
Progress = Callable[[int, int, int], None]


def format_time(moment: datetime) -> str:
    """UDL's timestamp form: UTC, millisecond precision, Z suffix."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def time_filter(start: datetime, end: datetime) -> str:
    """A bounded UDL time filter. `start..end` is the only form the service accepts."""
    if end <= start:
        raise ValueError("a time window must end after it starts")
    return f"{format_time(start)}..{format_time(end)}"


def slice_length(
    records: int, span: timedelta, *, remaining: timedelta | None = None, workers: int = 24
) -> timedelta:
    """The slice length that should hold about the target number of records.

    With `remaining` (the time still to read), a read too short to fill every
    worker at the target is cut into smaller slices, so it finishes sooner.
    """
    if records == 0:
        return CAUTIOUS_SLICE
    per_second = records / span.total_seconds()
    target = float(TARGET_RECORDS_PER_SLICE)
    if remaining is not None:
        expected = per_second * remaining.total_seconds()
        target = min(target, max(MIN_RECORDS_PER_SLICE, expected / max(1, workers)))
    length = timedelta(seconds=target / per_second)
    return max(MIN_SLICE, min(MAX_SLICE, length))


def divide(start: datetime, end: datetime, length: timedelta) -> list[Window]:
    """Consecutive windows of at most `length` covering start to end."""
    windows: list[Window] = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + length)
        windows.append((cursor, stop))
        cursor = stop
    return windows


def halve(window: Window) -> list[Window]:
    """Split a window in two, or fail if it is already at the minimum slice."""
    start, end = window
    if end - start <= MIN_SLICE:
        raise UdlError("a UDL window could not be divided small enough to read in full")
    middle = start + (end - start) / 2
    return [(start, middle), (middle, end)]


@dataclass(frozen=True, slots=True)
class _Outcome(Generic[T]):
    chunk: T | None
    count: int
    split: Sequence[Window]


@dataclass(frozen=True, slots=True)
class _Reader(Generic[T]):
    fetch: Fetch
    transform: Callable[[list[dict[str, Any]]], T]

    def attempt(self, window: Window) -> _Outcome[T]:
        try:
            records = self.fetch(*window)
        except UdlSliceTooLargeError:
            return _Outcome(None, 0, halve(window))
        if len(records) >= PAGE_CAP:
            return _Outcome(None, 0, halve(window))
        return _Outcome(self.transform(records), len(records), ())


def read_window(
    fetch: Fetch,
    start: datetime,
    end: datetime,
    transform: Callable[[list[dict[str, Any]]], T],
    *,
    workers: int = 24,
    on_progress: Progress | None = None,
) -> list[T]:
    """Every slice of [start, end), transformed, read in parallel and sized from a recent probe."""
    reader = _Reader(fetch, transform)
    probe_start = max(start, end - PROBE_SPAN)
    tracker = _Tracker(on_progress)
    pieces = divide(probe_start, end, max(MIN_SLICE, (end - probe_start) / PROBE_PIECES))
    probe = _drain(reader, pieces, workers, tracker)
    length = slice_length(
        tracker.records, end - probe_start, remaining=probe_start - start, workers=workers
    )
    return probe + _drain(reader, divide(start, probe_start, length), workers, tracker)


class _Tracker:
    """Running totals across the probe and the rest, reported after every slice."""

    def __init__(self, on_progress: Progress | None) -> None:
        self.records = 0
        self.done = 0
        self._on_progress = on_progress

    def add(self, outcome: _Outcome[Any], pending: int) -> None:
        self.records += outcome.count
        self.done += 0 if outcome.split else 1
        if self._on_progress is not None:
            self._on_progress(self.records, self.done, pending)


def _drain(reader: _Reader[T], windows: list[Window], workers: int, tracker: _Tracker) -> list[T]:
    chunks: list[T] = []
    if not windows:
        return chunks
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        pending: set[Future[_Outcome[T]]] = {pool.submit(reader.attempt, w) for w in windows}
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                outcome = future.result()
                if outcome.chunk is not None:
                    chunks.append(outcome.chunk)
                pending |= {pool.submit(reader.attempt, w) for w in outcome.split}
                tracker.add(outcome, len(pending))
    finally:
        # On any failure, queued slices are cancelled and the error surfaces at
        # once; requests already in flight finish in the background unread.
        pool.shutdown(wait=False, cancel_futures=True)
    return chunks
