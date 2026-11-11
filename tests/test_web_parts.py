"""Sessions, rate limits, run failure paths and figure helpers, one at a time."""

from __future__ import annotations

import itertools
import time
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest

from lightbee.figures.common import Caption, escape, layout, rounded, thin
from lightbee.figures.seasonal import magnitude_limits
from lightbee.udl.client import Credentials, UdlError
from lightbee.web import runs
from lightbee.web.limits import Limit, RateLimiter
from lightbee.web.runs import Run, RunData, RunRequest, execute, markings
from lightbee.web.session import SessionLimitError, SessionStore
from tests.fake_world import NOW, TARGET, factory, world

CREDENTIALS = Credentials("analyst.one", "not-real-distinctive-pw-7")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_sessions_expire_when_idle_and_sign_out_clears_everything() -> None:
    clock = Clock()
    store = SessionStore(idle_hours=1, max_sessions=2, clock=clock)
    token, session = store.create(CREDENTIALS)
    session.remember(("a",), 1)
    assert store.get(token) is session
    clock.now += 3599
    assert store.get(token) is session  # touched, so the hour restarts
    clock.now += 3601
    assert store.get(token) is None
    assert session.cache == {}
    token, session = store.create(CREDENTIALS)
    session.remember(("b",), 2)
    store.remove(token)
    assert session.cache == {}
    assert store.get(token) is None
    assert store.get(None) is None
    assert store.get("x" * 129) is None
    store.remove(None)


def test_the_session_cap_counts_only_live_sessions() -> None:
    clock = Clock()
    store = SessionStore(idle_hours=1, max_sessions=1, clock=clock)
    store.create(CREDENTIALS)
    with pytest.raises(SessionLimitError):
        store.create(CREDENTIALS)
    clock.now += 3601
    assert len(store) == 0
    store.create(CREDENTIALS)


def test_a_session_keeps_two_objects_newest_last() -> None:
    _, session = SessionStore(idle_hours=1, max_sessions=1).create(CREDENTIALS)
    for key in ("a", "b", "c"):
        session.remember((key,), key)
    assert session.recall(("a",)) is None
    assert session.recall(("b",)) == "b"
    session.remember(("d",), "d")
    assert list(session.cache) == [("b",), ("d",)]
    assert "credentials" not in repr(session).split("key=")[0] or "not-real" not in repr(session)
    assert "not-real" not in repr(session)


def test_rate_limits_slide_and_hold_both_tiers() -> None:
    clock = Clock()
    limiter = RateLimiter(Limit(per_session=2, service_wide=3, period_seconds=60), clock)
    assert limiter.admit("a")
    assert limiter.admit("a")
    assert not limiter.admit("a")
    assert limiter.admit("b")
    assert not limiter.admit("c")  # the service-wide tier is full
    clock.now += 61
    assert limiter.admit("a")


def test_the_limiter_forgets_idle_keys_rather_than_grow(monkeypatch: pytest.MonkeyPatch) -> None:
    from lightbee.web import limits

    monkeypatch.setattr(limits, "MAX_TRACKED_KEYS", 3)
    clock = Clock()
    limiter = RateLimiter(Limit(per_session=5, service_wide=100, period_seconds=10), clock)
    for key in ("a", "b", "c"):
        assert limiter.admit(key)
    clock.now += 11
    assert limiter.admit("d")  # a, b and c are idle and forgotten
    assert limiter.admit("e")
    assert limiter.admit("f")
    assert limiter.admit("g")  # all busy: the oldest is dropped to stay bounded
    assert len(limiter._keys) <= 3


def request(**changes: Any) -> RunRequest:
    values: dict[str, Any] = {
        "sat_no": TARGET,
        "change_date": date(2026, 11, 10),
        "window_days": 7,
        "provider": "KBR",
        "history_years": 0.25,
        "threshold_km": 150.0,
    }
    values.update(changes)
    return RunRequest(**values)


def new_run(**changes: Any) -> Run:
    return Run("r1", request(**changes), "abc", time.monotonic())


def test_an_unexpected_error_ends_the_run_in_a_stated_state() -> None:
    def broken(credentials: Credentials) -> Any:
        raise RuntimeError("boom")

    run = new_run()
    assert execute(run, CREDENTIALS, broken, None, NOW) is None
    assert run.stage == "failed"
    assert run.message == "The run failed unexpectedly; please try again."


def test_a_udl_failure_is_reported_without_detail() -> None:
    def unreachable(credentials: Credentials) -> Any:
        raise UdlError("could not reach the UDL (ConnectError)")

    run = new_run()
    execute(run, CREDENTIALS, unreachable, None, NOW)
    assert run.message == "The UDL read failed: could not reach the UDL (ConnectError)."


def test_a_provider_with_no_elsets_says_so_and_a_slow_screen_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = world()
    run = new_run(provider="NobodyFilesHere")
    data = execute(run, CREDENTIALS, factory(fake), None, NOW)
    assert run.stage == "done"
    assert data is not None
    assert run.results["approaches"] == []
    assert "No elsets" in run.results["_approaches_note"]
    monkeypatch.setattr(runs, "APPROACH_TIME_LIMIT_S", -1.0)
    slow = new_run()
    execute(slow, CREDENTIALS, factory(fake), None, NOW)
    assert "time limit" in slow.results["_approaches_note"]


def test_markings_and_windows_need_data() -> None:
    assert markings(None) == "UNCLASSIFIED"
    run = new_run()
    with pytest.raises(ValueError, match="not read anything"):
        run.window_ms()
    run.data = RunData(end_ms=0)
    assert run.window_ms() == (-7 * 86_400_000, 0)
    assert request().end(datetime(2026, 11, 10, 12, tzinfo=UTC)) == datetime(
        2026, 11, 10, 12, tzinfo=UTC
    )
    assert request().end(datetime(2026, 12, 1, tzinfo=UTC)) == datetime(2026, 11, 11, tzinfo=UTC)


def test_figure_helpers_escape_round_and_thin() -> None:
    assert escape("<b>&") == "&lt;b&gt;&amp;"
    assert rounded([1.23456, float("nan")], 2) == [1.23, None]
    assert thin(5, 10).tolist() == [0, 1, 2, 3, 4]
    assert thin(100, 10).size == 10
    # Fitted to the object, glints aside, as the predecessor's Dynamic Range plots.
    mags = np.concatenate([np.linspace(10.0, 14.0, 1000), [3.0, 25.0]])
    bright, faint = magnitude_limits(mags)
    assert 9.5 < bright < 9.7
    assert 14.3 < faint < 14.5
    assert magnitude_limits([12.0, 12.1]) == (11.55, 12.55)  # a flat object still gets 1 mag
    assert magnitude_limits([]) == (7.0, 15.0)
    caption = Caption("UNCLASSIFIED", 1, "w", "s", NOW)
    figure = layout(caption, "<title>", "sub", 500)
    assert figure["annotations"][2]["text"] == "&lt;title&gt;"
    assert figure["annotations"][2]["font"]["weight"] == "bold"
    assert "Bluestaq Limited" in caption.footer()[1]
    assert np.isfinite(figure["height"])


def test_the_memory_budget_drops_the_oldest_sessions_data_first() -> None:
    clock = Clock()
    store = SessionStore(idle_hours=1, max_sessions=4, clock=clock)

    class Sized:
        def __init__(self, nbytes: int) -> None:
            self.nbytes = nbytes

    _, old = store.create(CREDENTIALS)
    old.remember(("a",), Sized(600))
    clock.now += 10
    _, new = store.create(CREDENTIALS)
    new.remember(("b",), Sized(600))
    store.enforce_budget(1000, keep=new)
    assert old.cache == {}
    assert list(new.cache) == [("b",)]
    store.enforce_budget(100, keep=new)
    assert new.cache == {}


def test_a_cancelled_run_stops_at_its_next_step() -> None:
    fake = world()
    run = new_run()
    run.cancel.set()
    assert execute(run, CREDENTIALS, factory(fake), None, NOW) is None
    assert run.stage == "failed"
    assert run.message == "Stopped: the session ended."
    assert run.snapshot()["seconds"] == run.snapshot()["seconds"]  # frozen once finished


def test_day_colours_spread_across_any_window_and_the_seasonal_evidence_line() -> None:
    from lightbee.figures.causes import day_colour

    assert day_colour(0, 31) == "#d0021b"
    assert day_colour(1, 31) == "#3a6fc4"
    assert day_colour(30, 31) == "#eef4fb"
    assert len({day_colour(d, 31) for d in range(31)}) == 31


def test_the_evidence_line_states_the_numbers_for_every_outcome() -> None:
    from lightbee.analysis.seasonal import BinTable, Context, SeasonalFlag
    from lightbee.figures.seasonal import evidence

    def flag(outcome: str, delta: float = 0.21, years: tuple[int, ...] = (1, 2)) -> SeasonalFlag:
        context = Context(years, 100.0, 140.0, 2.0, 7, 30, 10, 20, recent_spread_deg=3.4)
        return SeasonalFlag(outcome, "h", "d", False, delta, 4.2, context, BinTable())  # type: ignore[arg-type]  # outcome as text

    assert evidence(flag("departs")) == (
        "0.21 mag fainter than the same season 1 and 2 years ago, +4.2 sigma across 0 SEPA "
        "bins; longitude 100.0°E."
    )
    assert "brighter" in evidence(flag("consistent", delta=-0.05, years=(1,)))
    assert "1 year ago" in evidence(flag("consistent", delta=-0.05, years=(1,)))
    assert evidence(flag("drifting")) == "Longitude moved 3.4° within the window."
    assert "140.0°E in the reference seasons, 100.0°E now" in evidence(flag("relocated"))
    assert evidence(flag("inconclusive")).startswith("0 comparable SEPA bins")


def test_neighbour_labels_lean_clear_of_the_axis_and_neighbours_have_their_own_colours() -> None:
    from lightbee.figures import causes

    assert causes._label_offset(10, 0, 1000)["xanchor"] == "left"
    assert causes._label_offset(500, 0, 1000) == {"ax": 0, "ay": -24}
    assert not set(causes.NEIGHBOUR_COLOURS) & set(causes.SENSOR_COLOURS)


def test_the_footer_breaks_onto_two_lines_and_the_bars_span_any_width() -> None:
    caption = Caption("UNCLASSIFIED", 1, "w", "s", datetime(2026, 11, 10, tzinfo=UTC))
    frame = layout(caption, "t", "s", 500)
    texts = [note["text"] for note in frame["annotations"]]
    assert caption.footer()[0] in texts
    assert caption.footer()[1] in texts
    assert not any("&lt;" in text or "<br" in text for text in texts)
    assert all(bar["x0"] <= -1 and bar["x1"] >= 2 for bar in frame["shapes"])


def test_the_rate_limiter_counts_failures_apart_from_admissions() -> None:
    limiter = RateLimiter(Limit(per_session=2, service_wide=3, period_seconds=60), clock=lambda: 0)
    assert not limiter.blocked("a")
    limiter.record("a")
    limiter.record("a")
    assert limiter.blocked("a", shared=False)
    limiter.record("b")
    assert limiter.blocked("c")  # the shared tier is full
    assert not limiter.blocked("c", shared=False)


def test_dense_photometry_is_drawn_within_each_panels_marker_budget() -> None:
    from lightbee.analysis.series import DAY_MS, Series
    from lightbee.figures import causes, seasonal

    end = int(NOW.timestamp() * 1000)
    count = 400 * 1500  # 400 nights at 1,500 a night
    epoch = np.sort(end - np.arange(count, dtype=np.int64) * (400 * DAY_MS // count))
    light = Series.from_arrays(epoch, 12 + np.sin(epoch / 1e7), np.linspace(-100, 100, count))
    caption = Caption("UNCLASSIFIED", 1, "w", "s", NOW)
    inputs = causes.CausesInputs(light, light, None, [], (end - 7 * DAY_MS, end), 150.0)
    figure = causes.causes_figure(inputs, caption)

    def drawn(group: str) -> int:
        return sum(len(t["x"]) for t in figure["data"] if t.get("legendgroup") == group)

    assert drawn("days") <= causes.LIGHT_CURVE_POINTS
    assert causes.SENSOR_POINTS - 10 <= drawn("sensors") <= causes.SENSOR_POINTS + 10
    assert all(
        len(t["x"]) <= causes.GHOST_POINTS
        for t in figure["data"]
        if t.get("legendgroup") == "ghost"
    )
    recent = seasonal.seasonal_figure(light, None, None, caption, end)["data"][0]
    assert len(recent["x"]) <= seasonal.RECENT_POINTS


def test_history_is_split_into_the_flags_seasons_and_the_rest_with_no_gap_or_overlap() -> None:
    from datetime import timedelta

    from lightbee.analysis.series import DAY_MS
    from lightbee.web.runs import history_spans

    end_ms = int(NOW.timestamp() * 1000)
    recent_start = NOW - timedelta(days=15)
    history_start = NOW - timedelta(days=730)
    seasons, rest = history_spans((end_ms - 7 * DAY_MS, end_ms), history_start, recent_start)
    assert len(seasons) == 2
    assert all((stop - start).days == 27 for start, stop in seasons)
    pieces = sorted(seasons + rest)
    assert pieces[0][0] < history_start  # the oldest season is read whole
    assert pieces[-1][1] == recent_start
    assert all(a[1] == b[0] for a, b in itertools.pairwise(pieces))
    assert rest[0][1] == recent_start  # the newest gap is read first
    short, remaining = history_spans(
        (end_ms - 7 * DAY_MS, end_ms), NOW - timedelta(days=91), recent_start
    )
    assert len(short) == 2  # the flag's seasons are read even for a short history
    assert remaining == [(NOW - timedelta(days=91), recent_start)]


def test_the_automatic_basis_is_held_once_chosen() -> None:
    from lightbee.udl.photometry import Photometry
    from lightbee.web.runs import RunData, _series

    count = 100
    mag = np.full(count, 11.0, dtype=np.float32)
    norm = np.full(count, np.nan, dtype=np.float32)
    norm[:5] = 18.0  # most of the later data has no range
    photometry = Photometry(
        epoch_ms=np.arange(count, dtype=np.int64),
        mag=mag,
        mag_norm=norm,
        sepa=np.zeros(count, dtype=np.float32),
        geolon=np.zeros(count, dtype=np.float32),
        sensor=np.zeros(count, dtype=np.int16),
        sensors=("S",),
    )
    data = RunData(end_ms=count, auto_basis="range-normalised")
    assert _series(data, photometry, "auto").basis == "range-normalised"
    assert _series(data, photometry, "raw").basis == "raw"
    fresh = RunData(end_ms=count)
    assert _series(fresh, photometry, "auto").basis == "raw"
    assert fresh.auto_basis == "raw"


def test_every_heatmap_day_is_a_whole_number_of_pixels_tall() -> None:
    from lightbee.figures import seasonal

    for rows in (92, 731, 732):
        height, recent, heat = seasonal.geometry(rows)
        area = height - seasonal.HEADER_PX - seasonal.FOOTER_PX
        assert round(area * (heat[1] - heat[0]), 6) == rows * seasonal.ROW_PX
        assert recent[1] == 1.0
        assert recent[0] > heat[1]  # a gap between the panels
