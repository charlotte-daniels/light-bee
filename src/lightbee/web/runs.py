"""Runs: one analyst's read and analysis of one object, in the background.

A run reads in stages so the analyst sees something early: the recent weeks
first (the top panel and the light curve), then the same season in each
earlier year (all the seasonal flag needs, so it is published at once), then
the target's elsets and a GEO snapshot, screened at once with the close
approaches named (Other Causes and the scan are usable from here), and last
the rest of two years of history (the heatmap). The history was 73 to 82% of
each run measured on the owner's known events, so it goes last. The browser
polls the run's state.

Each run has its own UDL client and throttle, so a failed run's requests still
in flight can never take slots from the next. A session runs one job at a time,
and a run stops at its next step if the analyst signs out or the session ends.

What was read is kept in the session (two objects at most) and reused part by
part: the photometry when it ends at the same moment (or, for today's date,
within the warm tolerance), the elsets and snapshot only when they cover the
new window. Anything not covered is read again, so a warm run is never served
data that does not fit the question asked.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Final, Literal

import numpy as np

from lightbee.analysis.screen import Approach, ScreenError, approaches
from lightbee.analysis.seasonal import (
    SeasonalFlag,
    SepaGrid,
    build_grid,
    season_windows,
    seasonal_flag,
)
from lightbee.analysis.series import DAY_MS, Series
from lightbee.analysis.triage import Board, Inputs, board
from lightbee.udl.client import Credentials, UdlAuthError, UdlError, UdlReadClient
from lightbee.udl.photometry import Photometry, merge
from lightbee.udl.queries import (
    ElsetRead,
    read_elsets,
    read_geo_catalogue,
    read_names,
    read_observations,
)
from lightbee.udl.records import Elset, Tally

AUDIT: Final = logging.getLogger("lightbee.audit")
_LOG: Final = logging.getLogger("lightbee.runs")

RECENT_DAYS: Final = 15
# Elsets for the target are read from this long before the window, so the
# orbital panels open with a state and the screen has an elset near the start.
ELSET_LEAD: Final = timedelta(days=3)
# With a change time the elsets reach back far enough for the board's baseline:
# 30 days before the 24 hours before the time.
BOARD_ELSET_LEAD: Final = timedelta(days=31)
BOARD_AFTER: Final = timedelta(hours=48)
APPROACH_TIME_LIMIT_S: Final = 30.0
# Names are read for the approaches the Other Causes panel draws (its MAX_APPROACH_TRACES).
NAMED_APPROACHES: Final = 20
# A run for today's date reuses photometry read up to this long ago; the
# figures state the data's end time, so the analyst sees how fresh it is.
WARM_TOLERANCE_MS: Final = 30 * 60_000
# Rough bytes held per observation once a run has its series and grid built.
BYTES_PER_OBSERVATION: Final = 120

Stage = Literal[
    "queued",
    "recent",
    "seasons",
    "elsets",
    "catalogue",
    "screening",
    "names",
    "history",
    "done",
    "failed",
]
Basis = Literal["auto", "normalised", "raw"]
ClientFactory = Callable[[Credentials], UdlReadClient]


class RunCancelledError(Exception):
    """The analyst signed out, or the session ended, while the run was going."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What the analyst asked for, already validated at the boundary."""

    sat_no: int
    change_date: date
    window_days: int
    provider: str
    history_years: float
    threshold_km: float
    # The analyst's approximate change time on the change date (1.4); None: no board.
    change_time: dt_time | None = None

    @property
    def anchor(self) -> datetime | None:
        if self.change_time is None:
            return None
        return datetime.combine(self.change_date, self.change_time, UTC)

    def end(self, now: datetime) -> datetime:
        """The end of the change date (UTC), or now if that is still to come.

        With a change time, the window instead ends 48 hours after it, so the
        board sees two days after the change.
        """
        anchor = self.anchor
        if anchor is not None:
            return min(anchor + BOARD_AFTER, now)
        day_end = datetime.combine(self.change_date + timedelta(days=1), datetime.min.time(), UTC)
        return min(day_end, now)

    @property
    def cache_key(self) -> tuple[Any, ...]:
        return (self.sat_no, self.provider, self.change_date, self.history_years, self.change_time)


@dataclass
class RunData:
    """Everything read from the UDL for one object, reusable part by part."""

    end_ms: int
    photometry: Photometry | None = None
    # The recent weeks and the same season of each earlier year are all read:
    # enough for the seasonal flag, before the rest of the history arrives.
    seasons_complete: bool = False
    history_complete: bool = False
    # The automatic magnitude basis, fixed at the first analysis (the flag's),
    # so the early flag and the full analysis can never differ in basis.
    auto_basis: str | None = None
    elsets: ElsetRead | None = None
    elset_span: tuple[int, int] | None = None
    catalogue: dict[int, Elset] = field(default_factory=dict)
    catalogue_tally: Tally = field(default_factory=Tally)
    catalogue_span: tuple[int, int] | None = None
    approaches: dict[tuple[float, int, int], tuple[list[Approach], str]] = field(
        default_factory=dict
    )
    # Common names for the target and the GEO snapshot (1.2), display only.
    names: dict[int, str] = field(default_factory=dict)
    names_tally: Tally = field(default_factory=Tally)

    @property
    def nbytes(self) -> int:
        """An estimate of the memory this data and its analyses hold."""
        return len(self.photometry) * BYTES_PER_OBSERVATION if self.photometry else 0

    def photometry_fits(self, end_ms: int) -> bool:
        return (
            self.photometry is not None
            and self.history_complete
            and 0 <= end_ms - self.end_ms <= WARM_TOLERANCE_MS
        )


class RunReleasedError(Exception):
    """The run's data was released to free memory after the request began."""


RELEASED_MESSAGE: Final = (
    "This run's data was released to free memory for other analysts; run it again."
)


@dataclass(frozen=True)
class Analysis:
    """The seasonal products for one magnitude basis, published together."""

    series: Series
    grid: SepaGrid
    flag: SeasonalFlag


@dataclass
class Run:
    """The state the browser polls, and the results the figures are built from."""

    id: str
    request: RunRequest
    session_audit_id: str
    started: float
    stage: Stage = "queued"
    records: int = 0
    message: str = ""
    warm: bool = False
    finished_at: float | None = None
    released: bool = False
    data: RunData | None = None
    results: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    compute: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Seconds spent in each stage, and from the start to the flag, for the audit
    # line: where a slow run's time went, read from the platform's log.
    stage_seconds: dict[str, float] = field(default_factory=dict)
    stage_started: float | None = None
    flag_seconds: float | None = None

    @property
    def finished(self) -> bool:
        return self.stage in ("done", "failed")

    def set(self, **changes: Any) -> None:
        with self.lock:
            now = time.monotonic()
            if "stage" in changes and changes["stage"] != self.stage:
                began = self.started if self.stage_started is None else self.stage_started
                spent = self.stage_seconds.get(self.stage, 0.0) + (now - began)
                self.stage_seconds[self.stage] = round(spent, 1)
                self.stage_started = now
            for name, value in changes.items():
                setattr(self, name, value)
            if self.stage in ("done", "failed") and self.finished_at is None:
                self.finished_at = now

    def flag_ready(self) -> None:
        """Note the moment the flag was first published. Call with the lock held."""
        if self.flag_seconds is None:
            self.flag_seconds = round(time.monotonic() - self.started, 1)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            until = self.finished_at if self.finished_at is not None else time.monotonic()
            return {
                "id": self.id,
                "stage": self.stage,
                "records": self.records,
                "message": self.message,
                "warm": self.warm,
                "ready": sorted(name for name in self.results if not name.startswith("_")),
                "seconds": round(until - self.started, 1),
            }

    def window_ms(self) -> tuple[int, int]:
        data = self.data  # read once: a release may clear it at any moment
        if data is None:
            if self.released:
                raise RunReleasedError
            raise ScreenError("the run has not read anything yet")
        end = data.end_ms
        return end - self.request.window_days * DAY_MS, end

    def release(self) -> None:
        """Drop a finished run's data to free memory; the analyst is told to re-run."""
        with self.lock:
            self.released = True
            self.data = None
            self.results.clear()
            self.message = RELEASED_MESSAGE

    def check(self) -> None:
        if self.cancel.is_set():
            raise RunCancelledError


def execute(
    run: Run,
    credentials: Credentials,
    make_client: ClientFactory,
    cached: RunData | None,
    now: datetime,
) -> RunData | None:
    """Carry a run through every stage. Returns the data worth caching, if complete."""
    try:
        data = _execute(run, credentials, make_client, cached, now)
    except RunCancelledError:
        run.set(stage="failed", message="Stopped: the session ended.")
        _audit(run, "cancelled")
        return None
    except UdlAuthError:
        run.set(stage="failed", message="The UDL did not accept your credentials for this data.")
        _audit(run, "failed")
        return None
    except UdlError as error:
        # The client's messages never carry credentials or response bodies.
        run.set(stage="failed", message=f"The UDL read failed: {error}.")
        _audit(run, "failed")
        return None
    # A background job must always end in a stated state, so it catches
    # everything (ruff BLE001).
    except Exception:  # noqa: BLE001
        _LOG.exception("run %s failed unexpectedly", run.id)
        run.set(stage="failed", message="The run failed unexpectedly; please try again.")
        _audit(run, "failed")
        return None
    run.set(stage="done")
    _audit(run, "done")
    return data if data.history_complete else None


def _execute(
    run: Run,
    credentials: Credentials,
    make_client: ClientFactory,
    cached: RunData | None,
    now: datetime,
) -> RunData:
    run.check()  # a run queued behind a sign-out never touches the UDL
    request = run.request
    end_ms = round(request.end(now).timestamp() * 1000)
    reuse = cached is not None and cached.photometry_fits(end_ms)
    data = _carry_over(cached) if reuse and cached is not None else RunData(end_ms=end_ms)
    run.set(data=data)
    window_start, end = run.window_ms()
    elset_span = (window_start - round(ELSET_LEAD.total_seconds() * 1000), end)
    anchor = request.anchor
    if anchor is not None:
        reach_back = round((anchor - BOARD_ELSET_LEAD).timestamp() * 1000)
        elset_span = (min(elset_span[0], reach_back), end)
    need_elsets = not _covers(data.elset_span, elset_span)
    need_catalogue = not _covers(data.catalogue_span, (window_start, end))
    run.set(warm=reuse and not need_elsets and not need_catalogue)
    if not (reuse and not need_elsets and not need_catalogue):
        with make_client(credentials) as client:
            if not reuse:
                _read_early(run, client, data, now)
            _read_orbit(run, client, data, elset_span if need_elsets else None, need_catalogue)
            _screen(run, data, lambda: nullcontext(client))
            if not reuse:
                _read_history(run, client, data, now)
    else:
        _screen(run, data, lambda: make_client(credentials))
    # An object with no photometry still publishes its (empty) panel.
    if data.photometry is not None:
        _publish(run, "recent", _series(data, data.photometry, "auto"))
    analysis(run, "auto")
    return data


def _read_orbit(
    run: Run,
    client: UdlReadClient,
    data: RunData,
    elset_span: tuple[int, int] | None,
    need_catalogue: bool,
) -> None:
    """The target's elsets over this span, and the GEO snapshot if it is needed."""
    request = run.request
    run.check()
    if elset_span is not None:
        run.set(stage="elsets")
        data.elsets = read_elsets(
            client,
            request.sat_no,
            (_utc(elset_span[0]), _utc(elset_span[1])),
            source=request.provider,
        )
        data.elset_span = elset_span
    run.check()
    if need_catalogue:
        window_start, end = run.window_ms()
        run.set(stage="catalogue")
        # One elset per neighbour, the one nearest this moment: the change time
        # when one is given, so a neighbour that station-keeps is placed right at
        # the moment the board judges. Case 1 of the owner's events found no pass
        # with the window's middle, 37 hours before the encounter.
        anchor = request.anchor
        moment = _utc((window_start + end) // 2) if anchor is None else anchor.astimezone(UTC)
        data.catalogue, data.catalogue_tally = read_geo_catalogue(
            client, moment, source=request.provider
        )
        data.catalogue_span = (window_start, end)


def _screen(
    run: Run, data: RunData, open_client: Callable[[], AbstractContextManager[UdlReadClient]]
) -> None:
    """Close approaches, named, then Other Causes and the scan are ready."""
    run.check()
    run.set(stage="screening")
    _publish_orbit(run, data)
    _name_approaches(run, data, open_client)
    _publish(run, "causes", True)


def _name_approaches(
    run: Run, data: RunData, open_client: Callable[[], AbstractContextManager[UdlReadClient]]
) -> None:
    """Names for the target and its close approaches only: one small read, after screening.

    Names are display text, so only the objects on screen are named; a scan
    names its own list. Names already held (a warm run) are not read again.
    """
    with run.lock:
        found: list[Approach] = list(run.results.get("approaches", []))
    shown = found[:NAMED_APPROACHES]  # the ones the panel draws, so always one read
    wanted = {run.request.sat_no, *(a.sat_no for a in shown)} - data.names.keys()
    if not wanted:
        return
    run.set(stage="names")
    with open_client() as client:
        add_names(data, run, client, wanted, run.check)


def add_names(
    data: RunData,
    run: Run,
    client: UdlReadClient,
    sat_nos: set[int],
    check: Callable[[], None] = lambda: None,
) -> None:
    """Read and keep names for these numbers; a failed read leaves them blank.

    `check` is the run's cancel check while the run is going; a scan, made
    after the run has finished, passes none.
    """
    names, tally = _names(client, sat_nos, check)
    with run.lock:
        data.names.update(names)
        data.names_tally.add(tally)


def _names(
    client: UdlReadClient, sat_nos: set[int], check: Callable[[], None]
) -> tuple[dict[int, str], Tally]:
    """Names for display; a refused or failed read leaves them blank, never fails the run."""
    try:
        return read_names(client, sat_nos, check)
    except UdlError:
        _LOG.warning("satellite names could not be read; the run continues without them")
        return {}, Tally()


def _copied(tally: Tally) -> Tally:
    fresh = Tally()
    fresh.add(tally)
    return fresh


def _carry_over(cached: RunData) -> RunData:
    """The cached data, with the parts that depend on the window kept for reuse."""
    return RunData(
        end_ms=cached.end_ms,
        photometry=cached.photometry,
        seasons_complete=True,
        history_complete=True,
        auto_basis=cached.auto_basis,
        elsets=cached.elsets,
        elset_span=cached.elset_span,
        catalogue=cached.catalogue,
        catalogue_tally=cached.catalogue_tally,
        catalogue_span=cached.catalogue_span,
        approaches=cached.approaches,
        # Copies, so a later run adding names never changes the cached data.
        names=dict(cached.names),
        names_tally=_copied(cached.names_tally),
    )


def _covers(held: tuple[int, int] | None, wanted: tuple[int, int]) -> bool:
    return held is not None and held[0] <= wanted[0] and wanted[1] <= held[1]


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _photometry_spans(run: Run, data: RunData) -> tuple[datetime, datetime, datetime]:
    """The end, the start of the recent weeks and the start of the history."""
    request = run.request
    end = _utc(data.end_ms)
    recent_start = end - timedelta(days=max(RECENT_DAYS, request.window_days))
    history_start = end - timedelta(days=round(365.25 * request.history_years))
    return end, recent_start, history_start


def _read_early(run: Run, client: UdlReadClient, data: RunData, now: datetime) -> None:
    """The recent weeks, then the same season in earlier years: the flag."""
    end, recent_start, history_start = _photometry_spans(run, data)
    run.set(stage="recent")
    data.photometry = read_observations(
        client, run.request.sat_no, (recent_start, end), now=now, on_progress=_counter(run)
    )
    _publish(run, "recent", Series.of(data.photometry))
    run.check()
    run.set(stage="seasons")
    seasons, _ = history_spans(run.window_ms(), history_start, recent_start)
    _read_spans(run, client, data, seasons, now)
    data.seasons_complete = True
    run.check()
    flag_of(run, "auto")  # published now: the flag needs nothing more


def _read_history(run: Run, client: UdlReadClient, data: RunData, now: datetime) -> None:
    """The rest of two years, for the heatmap: read last, as it is most of a run."""
    run.check()
    _, recent_start, history_start = _photometry_spans(run, data)
    run.set(stage="history")
    _, rest = history_spans(run.window_ms(), history_start, recent_start)
    _read_spans(run, client, data, rest, now)
    data.history_complete = True
    run.check()
    analysis(run, "auto")


def _read_spans(
    run: Run, client: UdlReadClient, data: RunData, spans: list[Span], now: datetime
) -> None:
    """Read several spans at once and add them to the photometry.

    They share the client's throttle, so together they never exceed the
    requests in flight one read would make; reading them side by side lets
    each one's slow first requests overlap.
    """
    if not spans:
        return
    held = data.photometry
    base = len(held) if held is not None else 0
    counts = [0] * len(spans)
    lock = threading.Lock()
    stop = threading.Event()  # set when any span fails, so its siblings stop too

    def read(index: int, span: Span) -> Photometry:
        def progress(records: int, done: int, pending: int) -> None:
            run.check()
            if stop.is_set():
                raise RunCancelledError
            with lock:
                counts[index] = records
                total = base + sum(counts)
            run.set(records=total)

        return read_observations(client, run.request.sat_no, span, now=now, on_progress=progress)

    pool = ThreadPoolExecutor(max_workers=len(spans))
    # Each reader runs in this run's context, so its lines carry the request ID.
    futures = [
        pool.submit(contextvars.copy_context().run, read, index, span)
        for index, span in enumerate(spans)
    ]
    try:
        for future in as_completed(futures):
            future.result()  # the first failure surfaces here, at once
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    parts = [future.result() for future in futures]
    for part in parts:
        held = part if held is None else merge(held, part)
    data.photometry = held
    run.check()


Span = tuple[datetime, datetime]


def history_spans(
    window_ms: tuple[int, int], history_start: datetime, recent_start: datetime
) -> tuple[list[Span], list[Span]]:
    """The history before the recent read, split into the flag's seasons and the rest.

    Each season is read whole, even where it starts before the history (two
    years back, the season's first days fall just outside it), so the flag
    compares like with like; the heatmap still shows only the history. A
    history shorter than a year (possible through the API, not the page) still
    reads the seasons, since the flag needs them. Seasons
    never overlap the recent read, and together the two lists cover the
    history and the seasons exactly once.
    """
    low_ms, high_ms = (
        round(history_start.timestamp() * 1000),
        round(recent_start.timestamp() * 1000),
    )
    seasons: list[tuple[int, int]] = []
    for _, low, high in sorted(season_windows(*window_ms), key=lambda w: w[1]):
        start, stop = low, min(high, high_ms)
        if start < stop:
            if seasons and start <= seasons[-1][1]:
                seasons[-1] = (seasons[-1][0], max(stop, seasons[-1][1]))
            else:
                seasons.append((start, stop))
    rest: list[tuple[int, int]] = []
    cursor = low_ms
    for start, stop in seasons:
        if cursor < start:
            rest.append((cursor, start))
        cursor = max(cursor, stop)
    if cursor < high_ms:
        rest.append((cursor, high_ms))
    # The newest gap first, so the heatmap fills from the top.
    return [(_utc(a), _utc(b)) for a, b in seasons], [(_utc(a), _utc(b)) for a, b in rest][::-1]


def _counter(run: Run, base: int = 0) -> Callable[[int, int, int], None]:
    def progress(records: int, done: int, pending: int) -> None:
        run.check()
        run.set(records=base + records)

    return progress


def _publish(run: Run, name: str, value: Any) -> None:
    if value is not None:
        with run.lock:
            run.results[name] = value


def analysis(run: Run, basis: Basis) -> Analysis | None:
    """The seasonal products for a basis, computed once per run and basis.

    The automatic basis is chosen once per run, at the first analysis (the
    early flag's), and held, so the recent panel, the heatmap and the flag
    never mix two bases. The grid and the flag are published together.
    """
    data = run.data
    if data is None or data.photometry is None or not data.history_complete:
        return None
    key = f"_analysis:{basis}"
    with run.compute:
        with run.lock:
            held = run.results.get(key)
        if isinstance(held, Analysis):
            return held
        series = _series(data, data.photometry, basis)
        window_start, end = run.window_ms()
        days = round(365.25 * run.request.history_years) + 1
        made = Analysis(
            series,
            build_grid(series, end_ms=end, days=days),
            seasonal_flag(series, recent_start_ms=window_start, recent_end_ms=end),
        )
        with run.lock:
            run.results[key] = made
            if basis == "auto":
                run.results["seasonal"] = True
                run.results["flag"] = True
                run.flag_ready()
        return made


def _series(data: RunData, photometry: Photometry, basis: Basis) -> Series:
    """The photometry on a basis; "auto" is chosen once per run and then held."""
    if basis == "raw":
        return Series.of(photometry, normalised=False)
    if data.auto_basis is None:
        data.auto_basis = Series.of(photometry).basis
    if data.auto_basis == "raw":
        return Series.of(photometry, normalised=False)
    series = Series.of(photometry)
    if series.basis == data.auto_basis:
        return series
    # Range-normalised was chosen, but later data has less range coverage:
    # held to the chosen basis, where range is missing the value is missing.
    mag = np.asarray(photometry.mag_norm, dtype=np.float64)
    return Series(
        series.epoch_ms,
        mag,
        series.sepa,
        series.geolon,
        series.sensor,
        series.sensors,
        "range-normalised",
    )


def flag_of(run: Run, basis: Basis) -> SeasonalFlag | None:
    """The seasonal flag for a basis, as soon as the seasons it needs are read.

    It looks only at the recent window and the same season of earlier years,
    so it is the same flag the full analysis reaches once the history is in.
    """
    full = analysis(run, basis)
    if full is not None:
        return full.flag
    data = run.data
    if data is None or data.photometry is None or not data.seasons_complete:
        return None
    key = f"_flag:{basis}"
    with run.compute:
        with run.lock:
            held = run.results.get(key)
        if isinstance(held, SeasonalFlag):
            return held
        series = _series(data, data.photometry, basis)
        window_start, end = run.window_ms()
        made = seasonal_flag(series, recent_start_ms=window_start, recent_end_ms=end)
        with run.lock:
            run.results[key] = made
            if basis == "auto":
                run.results["flag"] = True
                run.flag_ready()
        return made


def board_of(run: Run) -> Board | None:
    """The triage board (1.4) for a run with a change time, once the screen is named.

    It reads only the recent weeks, the elsets, the close approaches and the
    flag, all held before the rest of the history, so it is made once and kept.
    """
    anchor = run.request.anchor
    data = run.data  # read once: a release may clear it at any moment
    if anchor is None or data is None:
        return None
    with run.lock:
        held = run.results.get("_board")
        ready = "causes" in run.results
        found = list(run.results.get("approaches", []))
        note = str(run.results.get("_approaches_note", ""))
        names = dict(data.names)
    if isinstance(held, Board):
        return held
    if not ready:
        return None
    flag = flag_of(run, "auto")  # before the compute lock: it takes that lock itself
    # One board at a time per run: requests arriving together wait for the first.
    with run.compute:
        with run.lock:
            held = run.results.get("_board")
        if isinstance(held, Board):
            return held
        photometry = data.photometry
        given = Inputs(
            _series(data, photometry, "auto") if photometry is not None else None,
            data.elsets.elsets if data.elsets is not None else [],
            run.request.provider,
            found,
            flag,
            run.request.threshold_km,
            names,
            note,
        )
        made = board(given, anchor_ms=round(anchor.timestamp() * 1000), data_end_ms=data.end_ms)
        with run.lock:
            run.results["_board"] = made
        return made


def _publish_orbit(run: Run, data: RunData) -> None:
    targets = data.elsets.elsets if data.elsets is not None else []
    start, end = run.window_ms()
    key = (run.request.threshold_km, start, end)
    found: list[Approach]
    note: str
    if not targets:
        found, note = [], "No elsets from this provider for the target in the window."
    elif key in data.approaches:
        found, note = data.approaches[key]
    else:
        try:
            found, note = (
                approaches(
                    start,
                    end,
                    targets,
                    data.catalogue,
                    threshold_km=run.request.threshold_km,
                    deadline=time.monotonic() + APPROACH_TIME_LIMIT_S,
                ),
                "",
            )
            data.approaches[key] = (found, note)
        except ScreenError as error:
            found, note = [], str(error)
    with run.lock:
        run.results["elsets"] = targets
        run.results["approaches"] = found
        run.results["_approaches_note"] = note


def markings(data: RunData | None) -> str:
    """The marking for products made from everything this run read."""
    tally = Tally()
    if data is not None:
        for part in (
            data.photometry.tally if data.photometry is not None else None,
            data.elsets.tally if data.elsets is not None else None,
            data.catalogue_tally,
            data.names_tally,
        ):
            if part is not None:
                tally.controls |= part.controls
    return tally.banner()


def _audit(run: Run, outcome: str) -> None:
    """One line per run: who (hashed), what, how much, how long. Never credentials."""
    data = run.data
    photometry = data.photometry.tally.as_dict() if data and data.photometry else {}
    AUDIT.info(
        f"run {outcome}",
        extra={
            "attributes": {
                "event.name": "lightbee.run",
                "lightbee.outcome": outcome,
                "lightbee.session": run.session_audit_id,
                "lightbee.sat_no": run.request.sat_no,
                "lightbee.change_date": run.request.change_date.isoformat(),
                "lightbee.window_days": run.request.window_days,
                "lightbee.provider": run.request.provider,
                "lightbee.warm": run.warm,
                "lightbee.photometry": photometry,
                "lightbee.elsets": data.elsets.tally.kept if data and data.elsets else 0,
                "lightbee.catalogue": len(data.catalogue) if data else 0,
                "lightbee.seconds": round(time.monotonic() - run.started, 1),
                # Where the time went: seconds per stage, and from the start to the flag.
                "lightbee.stage_seconds": dict(run.stage_seconds),
                # Absent when the run ended before a flag was published.
                **({} if run.flag_seconds is None else {"lightbee.flag_seconds": run.flag_seconds}),
            }
        },
    )


def audit_scan(
    session_audit_id: str, run: Run, times: int, neighbours: int, seconds: float
) -> None:
    AUDIT.info(
        "scan",
        extra={
            "attributes": {
                "event.name": "lightbee.scan",
                "lightbee.session": session_audit_id,
                "lightbee.sat_no": run.request.sat_no,
                "lightbee.times": times,
                "lightbee.neighbours": neighbours,
                "lightbee.seconds": round(seconds, 2),
            }
        },
    )
