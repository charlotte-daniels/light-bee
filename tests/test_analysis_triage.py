"""The anchored triage board on synthetic objects whose answers are known."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import numpy as np
import pytest

from lightbee.analysis.screen import Approach
from lightbee.analysis.seasonal import SeasonalFlag
from lightbee.analysis.series import Series
from lightbee.analysis.triage import (
    HOUR_MS,
    HOURS,
    STEP_FITS_TIMES,
    Inputs,
    board,
    elset_steps,
    passes_in_strip,
    sensor_rows,
    strip,
)
from lightbee.udl.records import Elset, elset
from tests.fake_udl import elset_record

ANCHOR = datetime(2026, 11, 19, 4, 30, tzinfo=UTC)
A = int(ANCHOR.timestamp() * 1000)
SENSORS = ("EXO1612", "EXO1834", "EXO1840")


def photometry(
    events: dict[str, tuple[float, float, float]] | None = None, *, seed: int = 3
) -> Series:
    """Three sensors every 10 minutes through the strip, each with its own offset.

    Brightness follows SEPA through the day; `events` adds mag to one sensor
    between two times, in hours from the anchor.
    """
    rng = np.random.default_rng(seed)
    start, end = strip(A)
    times, mags, sepas, sensor = [], [], [], []
    for index, name in enumerate(SENSORS):
        t = np.arange(start + index * 120_000, end, 600_000, dtype=np.int64)
        sepa = ((t % 86_400_000) / 86_400_000) * 160.0 - 80.0
        mag = 11.0 + 0.02 * np.abs(sepa) + 0.25 * index + rng.normal(0.0, 0.04, t.size)
        for who, (lo, hi, by) in (events or {}).items():
            if who == name:
                mag[(t >= A + lo * HOUR_MS) & (t < A + hi * HOUR_MS)] += by
        times.append(t)
        mags.append(mag)
        sepas.append(sepa)
        sensor.append(np.full(t.size, index, dtype=np.int16))
    order = np.argsort(np.concatenate(times), kind="stable")
    return Series(
        np.concatenate(times)[order],
        np.concatenate(mags)[order],
        np.concatenate(sepas)[order],
        np.full(order.size, np.nan),
        np.concatenate(sensor)[order],
        SENSORS,
        "range-normalised",
    )


def elsets(step_at_hours: float | None = None, step: float = 0.1) -> list[Elset]:
    """KBR elsets every 3 hours for 35 days, period noise 0.001 min, one step if asked."""
    rng = np.random.default_rng(5)
    out = []
    first = ANCHOR - timedelta(days=35)
    period = 1436.1
    for k in range(36 * 8):
        when = first + timedelta(hours=3 * k)
        if when > ANCHOR + timedelta(hours=48):
            break
        moved = step_at_hours is not None and when >= ANCHOR + timedelta(hours=step_at_hours)
        p = period + (step if moved else 0.0) + float(rng.normal(0.0, 0.001))
        item = elset(elset_record(43874, when, period=p, meanMotion=1440.0 / p))
        assert item is not None
        out.append(item)
    return out


@dataclass(frozen=True)
class Flag:
    outcome: str
    headline: str


def flag(outcome: str = "consistent", headline: str = "Consistent with earlier years") -> Any:
    return cast(SeasonalFlag, Flag(outcome, headline))


def approach(sat_no: int, passes: list[tuple[int, float]]) -> Approach:
    tca, km = min(passes, key=lambda p: p[1])
    track = np.array([tca], dtype=np.int64)
    return Approach(sat_no, tca, km, True, False, track, np.array([km]), tuple(passes))


def made(
    series: Series | None = None,
    found_elsets: list[Elset] | None = None,
    found: list[Approach] | None = None,
    the_flag: Any = None,
    *,
    names: dict[int, str] | None = None,
    after_hours: float = 48.0,
) -> Any:
    given = Inputs(
        series,
        found_elsets or [],
        "KBR",
        found or [],
        flag() if the_flag is None else the_flag,
        150.0,
        names or {},
    )
    return board(given, anchor_ms=A, data_end_ms=A + round(after_hours * HOUR_MS))


def card(result: Any, key: str) -> Any:
    return next(c for c in result.cards if c.key == key)


def test_a_steady_object_departs_nowhere_whatever_its_sensors_offsets() -> None:
    rows = sensor_rows(photometry(), A)
    assert len(rows) == 3
    for row in rows:
        assert row.scatter_mag is not None
        assert row.scatter_mag < 0.08
        assert row.shift_mag is not None
        assert abs(row.shift_mag) < 0.03
        assert row.hour_median.size == HOURS
        assert not row.departs, row.spans


def test_a_three_hour_fade_in_every_sensor_is_dated_to_the_hour() -> None:
    fade = dict.fromkeys(SENSORS, (1.0, 4.0, 0.3))
    rows = sensor_rows(photometry(fade), A)
    assert all(r.departs for r in rows)
    for row in rows:
        (span,) = [s for s in row.spans if s.end_ms > A]
        assert span.start_ms == A + HOUR_MS
        assert span.end_ms == A + 4 * HOUR_MS
        assert span.median_mag == pytest.approx(0.3, abs=0.06)
    result = made(photometry(fade))
    assert result.headline.startswith(
        "3 of 3 sensors depart from their own day before, from 19 Nov 05:30Z"
    )
    assert card(result, "sensor").state == "against"  # all three together: not one sensor
    assert card(result, "season").state == "against"  # hours, not seasons


def test_one_sensor_alone_departing_reads_as_that_sensors_artefact() -> None:
    result = made(photometry({"EXO1834": (0.0, 48.0, 0.4)}))
    assert card(result, "sensor").state == "fits"
    assert "Only EXO1834 departs" in card(result, "sensor").body
    assert "Fits the timing: Sensor." in result.headline


def test_a_period_step_is_measured_against_the_thirty_days_before() -> None:
    steps, typical, baseline = elset_steps(elsets(step_at_hours=3.0), "KBR", A)
    assert typical is not None
    assert baseline >= 200
    big = max(steps, key=lambda s: abs(s.step_min))
    assert big.step_min == pytest.approx(0.1, abs=0.005)
    assert big.times_typical is not None
    assert big.times_typical > STEP_FITS_TIMES
    assert big.before_ms < A + 3 * HOUR_MS <= big.after_ms
    manoeuvre = card(made(found_elsets=elsets(step_at_hours=3.0)), "manoeuvre")
    assert manoeuvre.state == "fits"
    assert "period changed by +0.10" in manoeuvre.body
    # No step: the elements say no manoeuvre.
    assert card(made(found_elsets=elsets()), "manoeuvre").state == "against"


def test_a_manoeuvre_cannot_be_judged_without_a_baseline() -> None:
    recent = [e for e in elsets(step_at_hours=3.0) if e.epoch >= ANCHOR - timedelta(days=2)]
    manoeuvre = card(made(found_elsets=recent), "manoeuvre")
    assert manoeuvre.state == "cannot"
    assert "typical step" in manoeuvre.body


def test_every_pass_in_the_strip_is_listed_and_a_routine_neighbour_says_so() -> None:
    """Case 2's shape: TJS-23 passes twice a day; one pass is 1.2 hours before the time."""
    twice_daily = [
        (A - 72 * 60_000 + k * 12 * HOUR_MS, 74.0 if k % 2 == 0 else 32.0) for k in range(-3, 5)
    ]
    found = [approach(67226, twice_daily)]
    listed = passes_in_strip(found, A)
    start, end = strip(A)
    assert all(start <= p.tca_ms < end for p in listed)
    assert len(listed) == 6
    neighbour = card(made(found_elsets=elsets(), found=found, names={67226: "TJS-23"}), "neighbour")
    assert neighbour.state == "fits"
    assert (
        "TJS-23 (67226) passed at 74 km on 19 Nov 03:18Z, 1.2 hours before your time"
        in neighbour.body
    )
    assert "passes 6 times in these 72 hours" in neighbour.body


def test_a_pass_far_from_the_time_does_not_fit_and_none_at_all_says_so() -> None:
    far = [approach(45985, [(A + 30 * HOUR_MS, 104.0)])]
    neighbour = card(made(found_elsets=elsets(), found=far), "neighbour")
    assert neighbour.state == "against"
    assert "Nearest in time" in neighbour.body
    nobody = card(made(found_elsets=elsets()), "neighbour")
    assert "No GEO object came within 150 km" in nobody.body


def test_the_season_card_cannot_judge_when_the_flag_cannot() -> None:
    drifting = flag("drifting", "Drifting: cannot compare")
    season = card(made(photometry(), the_flag=drifting), "season")
    assert season.state == "cannot"
    assert "Drifting: cannot compare" in season.body
    steady = made(photometry())
    assert card(steady, "season").state == "fits"
    assert steady.headline.startswith("None of 3 sensors departs")


def test_too_little_data_after_the_time_is_said_and_judged_honestly() -> None:
    cut = photometry().between(*strip(A))
    early = cut.take(cut.epoch_ms < A + HOUR_MS)  # an hour after the time, then nothing
    given = Inputs(early, [], "KBR", [], None, 150.0, {})
    result = board(given, anchor_ms=A, data_end_ms=A + HOUR_MS)
    assert "Observations after your time run to 19 Nov 05:30Z only." in result.lead
    assert card(result, "sensor").state == "cannot"
    assert card(result, "season").state == "cannot"
    assert card(result, "manoeuvre").state == "cannot"


def test_a_neighbour_within_reach_all_along_is_listed_but_not_judged_on_timing() -> None:
    """A neighbour sharing the slot has no distinct pass in the strip, but is never far."""
    start, end = strip(A)
    track = np.arange(start - 3 * 86_400_000, end, 300_000, dtype=np.int64)
    km = np.full(track.size, 100.0)
    km[track == A + 5 * HOUR_MS] = 99.0
    slot = Approach(40001, int(track[0]), 99.0, True, False, track, km, ((int(track[0]), 99.0),))
    listed = passes_in_strip([slot], A, reach_km=237.0)
    assert len(listed) == 1
    assert listed[0].held
    assert listed[0].tca_ms == A + 5 * HOUR_MS
    assert passes_in_strip([slot], A, reach_km=50.0) == []
    neighbour = card(made(found_elsets=elsets(), found=[slot]), "neighbour")
    assert neighbour.state == "cannot"
    assert "NORAD 40001 stays within reach throughout (closest 99 km" in neighbour.body


def test_a_step_after_a_month_of_identical_elsets_still_reads_as_a_manoeuvre() -> None:
    first = ANCHOR - timedelta(days=31)
    quiet = []
    for k in range(34 * 8):
        when = first + timedelta(hours=3 * k)
        period = 1436.15 if when >= ANCHOR + timedelta(hours=2) else 1436.1
        item = elset(elset_record(43874, when, period=period, meanMotion=1440.0 / period))
        assert item is not None
        quiet.append(item)
    manoeuvre = card(made(found_elsets=quiet), "manoeuvre")
    assert manoeuvre.state == "fits"
    assert "500 times" in manoeuvre.body


def test_the_strip_carries_its_markings_every_pass_and_each_step_label_above_its_bar() -> None:
    from lightbee.figures.board import board_figure
    from lightbee.figures.common import Caption

    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    twice = [(A - HOUR_MS + k * 12 * HOUR_MS, 74.0) for k in range(4)]
    result = made(
        photometry(dict.fromkeys(SENSORS, (1.0, 4.0, 0.3))),
        elsets(step_at_hours=3.0),
        [approach(67226, twice)],
        names={67226: "TJS-23"},
    )
    figure = board_figure(result, caption, {67226: "TJS-23"})
    kinds = [t["type"] for t in figure["data"]]
    assert kinds[0] == "heatmap"
    assert figure["data"][0]["y"] == list(SENSORS)
    labels = [t for t in figure["data"] if t.get("mode") == "text"]
    assert len(labels) == 1
    assert labels[0]["y"] == [1.35]  # above the bar at 1
    assert labels[0]["text"][0].startswith("+0.10")
    (passes,) = [t for t in figure["data"] if t.get("mode") == "markers+text"]
    assert len(passes["x"]) == 4
    assert passes["text"][0] == "TJS-23 (67226) 74 km"
    texts = [a["text"] for a in figure["layout"]["annotations"]]
    assert texts.count("UNCLASSIFIED") == 2
    assert "your change time, 19 Nov 04:30Z" in texts
    assert figure["summary"].startswith("Triage strip. 3 of 3 sensors depart")


def test_a_strip_with_no_sensor_says_so() -> None:
    from lightbee.figures.board import board_figure
    from lightbee.figures.common import Caption

    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    figure = board_figure(made(), caption, {})
    assert all(t["type"] != "heatmap" for t in figure["data"])
    texts = [a["text"] for a in figure["layout"]["annotations"]]
    assert "No sensor saw the object in these 72 hours." in texts
