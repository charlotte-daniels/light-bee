"""Window slicing: sized from a recent probe, split on caps and failures, never partial."""

from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from lightbee.udl.client import UdlAuthError, UdlError, UdlSliceTooLargeError
from lightbee.udl.windows import (
    CAUTIOUS_SLICE,
    MAX_SLICE,
    MIN_SLICE,
    PAGE_CAP,
    PROBE_PIECES,
    divide,
    format_time,
    halve,
    read_window,
    slice_length,
    time_filter,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _read(fetch: Any, start: datetime, end: datetime, **kwargs: Any) -> list[dict[str, Any]]:
    chunks = read_window(fetch, start, end, lambda records: records, **kwargs)
    return [record for chunk in chunks for record in chunk]


def test_timestamps_and_filters_use_the_udl_form() -> None:
    moment = datetime(2026, 11, 10, 9, 5, 7, 123456, tzinfo=UTC)
    assert format_time(moment) == "2026-11-10T09:05:07.123Z"
    assert time_filter(START, START + timedelta(days=1)) == (
        "2026-01-01T00:00:00.000Z..2026-01-02T00:00:00.000Z"
    )
    with pytest.raises(ValueError, match="end after"):
        time_filter(START, START)


def test_slice_length_targets_seven_thousand_records_within_bounds() -> None:
    assert slice_length(7_000, timedelta(days=7)) == timedelta(days=7)
    assert slice_length(70_000, timedelta(days=7)) == timedelta(days=0.7)
    assert slice_length(0, timedelta(days=7)) == CAUTIOUS_SLICE
    assert slice_length(10**9, timedelta(days=7)) == MIN_SLICE
    assert slice_length(1, timedelta(days=7)) == MAX_SLICE


def test_a_short_read_is_cut_finer_to_use_every_worker() -> None:
    week = timedelta(days=7)
    # 7,000 a week, 8 more days to read: about 8,000 records, so 24 workers get
    # slices of the 1,000-record floor rather than one 7,000-record slice.
    short = slice_length(7_000, week, remaining=timedelta(days=8), workers=24)
    assert short == timedelta(days=1)
    # A long read keeps the measured 7,000-record target.
    long = slice_length(7_000, week, remaining=timedelta(days=700), workers=24)
    assert long == week


def test_divide_covers_the_window_exactly() -> None:
    windows = divide(START, START + timedelta(days=10), timedelta(days=3))
    assert windows[0][0] == START
    assert windows[-1][1] == START + timedelta(days=10)
    assert all(a[1] == b[0] for a, b in itertools.pairwise(windows))
    assert divide(START, START, timedelta(days=1)) == []


def test_halve_refuses_below_the_minimum_slice() -> None:
    first, second = halve((START, START + timedelta(hours=2)))
    assert first[1] == second[0] == START + timedelta(hours=1)
    with pytest.raises(UdlError, match="divided"):
        halve((START, START + MIN_SLICE))


def _records_per_minute(start: datetime, end: datetime, per_minute: int) -> list[dict[str, Any]]:
    minutes = int((end - start).total_seconds() // 60)
    return [
        {"t": (start + timedelta(minutes=m)).isoformat()}
        for m in range(minutes)
        for _ in range(per_minute)
    ]


def test_a_read_covers_the_whole_window_with_no_gaps_or_duplicates() -> None:
    calls: list[tuple[datetime, datetime]] = []
    lock = threading.Lock()

    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        with lock:
            calls.append((start, end))
        return _records_per_minute(start, end, 1)

    progress: list[tuple[int, int, int]] = []
    end = START + timedelta(days=60)
    records = _read(fetch, START, end, workers=4, on_progress=lambda *a: progress.append(a))
    assert len(records) == 60 * 24 * 60
    covered = sorted(calls)
    assert covered[0][0] == START
    assert covered[-1][1] == end
    assert all(a[1] == b[0] for a, b in itertools.pairwise(covered))
    assert progress
    assert progress[-1][2] == 0


def test_a_slice_at_the_page_cap_is_split_and_reread() -> None:
    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        records = _records_per_minute(start, end, 20)
        return records[:PAGE_CAP]

    records = _read(fetch, START, START + timedelta(days=2), workers=4)
    assert len(records) == 2 * 24 * 60 * 20


def test_a_slice_the_service_cannot_answer_is_split() -> None:
    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        if end - start > timedelta(days=1):
            raise UdlSliceTooLargeError("too big")
        return _records_per_minute(start, end, 1)

    records = _read(fetch, START, START + timedelta(days=20), workers=4)
    assert len(records) == 20 * 24 * 60


def test_a_window_that_cannot_be_split_far_enough_fails_the_read() -> None:
    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise UdlSliceTooLargeError("always too big")

    with pytest.raises(UdlError, match="divided"):
        _read(fetch, START, START + timedelta(days=3), workers=2)


def test_any_other_failure_stops_the_read() -> None:
    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        if start < START + timedelta(days=10):
            raise UdlAuthError("refused")
        return _records_per_minute(start, end, 1)

    with pytest.raises(UdlAuthError):
        _read(fetch, START, START + timedelta(days=40), workers=2)


def test_the_probe_reads_the_recent_end_first_in_parallel_pieces() -> None:
    calls: list[tuple[datetime, datetime]] = []

    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        calls.append((start, end))
        return []

    end = START + timedelta(days=30)
    _read(fetch, START, end, workers=1)
    probe = sorted(calls[:PROBE_PIECES])
    assert probe[0][0] == end - timedelta(days=7)
    assert probe[-1][1] == end
    assert all(a[1] == b[0] for a, b in pairwise(probe))
    assert all(stop - start == timedelta(days=7) / PROBE_PIECES for start, stop in probe)


def test_each_slice_is_transformed_as_it_lands() -> None:
    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        return _records_per_minute(start, end, 1)

    counts = read_window(fetch, START, START + timedelta(days=10), len, workers=3)
    assert sum(counts) == 10 * 24 * 60
    assert len(counts) > 1


def test_a_non_udl_failure_also_stops_the_queue_quickly() -> None:
    calls = 0
    lock = threading.Lock()

    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        nonlocal calls
        with lock:
            calls += 1
        if start < START + timedelta(days=30):
            raise RuntimeError("unexpected")
        return []

    with pytest.raises(RuntimeError):
        _read(fetch, START, START + timedelta(days=200), workers=2)
    assert calls < 20


def test_progress_includes_the_probe() -> None:
    seen: list[int] = []

    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        return _records_per_minute(start, end, 1)

    _read(
        fetch,
        START,
        START + timedelta(days=3),
        workers=2,
        on_progress=lambda r, d, p: seen.append(r),
    )
    assert seen[-1] == 3 * 24 * 60
