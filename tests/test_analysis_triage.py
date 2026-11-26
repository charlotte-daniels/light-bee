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
    baseline,
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
    events: dict[str, tuple[float, float, float]] | None = None,
    *,
    seed: int = 3,
    nightly_mag: float = 0.0,
) -> Series:
    """Three sensors every 10 minutes from the baseline's start to the strip's end.

    Each sensor has its own offset; brightness follows SEPA through the day;
    `nightly_mag` gives each sensor a fresh zero-point offset every day, as
    real sensors have; `events` adds mag to one sensor between two times, in
    hours from the anchor.
    """
    rng = np.random.default_rng(seed)
    _, end = strip(A)
    first, _ = baseline(A)
    times, mags, sepas, sensor = [], [], [], []
    for index, name in enumerate(SENSORS):
        t = np.arange(first + index * 120_000, end, 600_000, dtype=np.int64)
        sepa = ((t % 86_400_000) / 86_400_000) * 160.0 - 80.0
        mag = 11.0 + 0.02 * np.abs(sepa) + 0.25 * index + rng.normal(0.0, 0.04, t.size)
        day = (t - first) // 86_400_000
        mag += rng.normal(0.0, nightly_mag, int(day.max()) + 1)[day] if nightly_mag else 0.0
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
        "From 19 Nov 05:30Z, all 3 sensors watching turn fainter against their own previous 7 days"
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
        "NORAD 67226 (TJS-23) passed at 74 km on 19 Nov 03:18Z, 1.2 hours before your time"
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
    assert steady.headline.startswith("None of the 3 sensors compared leaves its normal range")


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
    # The compact panel: one row per sensor, each labelled with its normal range.
    assert all(t["type"] == "scatter" for t in figure["data"])
    ticks = figure["layout"]["yaxis"]["ticktext"]
    assert [t.split()[0] for t in ticks] == list(SENSORS)
    assert all(chr(0xB1) in t for t in ticks)  # each row says its normal range
    fainter = next(t for t in figure["data"] if t.get("name") == "fainter")
    assert fainter["marker"]["color"] == "#f39c12"
    boxes = [
        s
        for s in figure["layout"]["shapes"]
        if s["type"] == "rect" and s.get("yref") == "y" and s["line"]["width"]
    ]
    assert len(boxes) == 3  # each sensor's 1 to 4 hour departure
    notes = [a["text"] for a in figure["layout"]["annotations"]]
    assert sum(1 for t in notes if t.startswith(("<b>+0.2", "<b>+0.3"))) == 3
    labels = [t for t in figure["data"] if t.get("mode") == "text"]
    assert len(labels) == 1
    assert labels[0]["y"] == [1.35]  # above the bar at 1
    assert labels[0]["text"][0].startswith("+0.10")
    (passes,) = [t for t in figure["data"] if t.get("mode") == "markers+text"]
    assert len(passes["x"]) == 4
    assert passes["text"][0] == "NORAD 67226 (TJS-23) 74 km"
    texts = [a["text"] for a in figure["layout"]["annotations"]]
    assert texts.count("UNCLASSIFIED") == 2
    assert "your change time, 19 Nov 04:30Z" in texts
    assert figure["summary"].startswith(
        "Triage strip. From 19 Nov 05:30Z, all 3 sensors watching turn fainter"
    )


def test_a_strip_with_no_sensor_says_so() -> None:
    from lightbee.figures.board import board_figure
    from lightbee.figures.common import Caption

    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    figure = board_figure(made(), caption, {})
    assert all(t["type"] != "heatmap" for t in figure["data"])
    texts = [a["text"] for a in figure["layout"]["annotations"]]
    assert "No sensor could be compared with its own previous 7 days." in texts


def test_the_season_card_cannot_judge_when_no_sensor_can_be_compared() -> None:
    season = card(made(), "season")  # a consistent flag, but no photometry near the time
    assert season.state == "cannot"
    assert "No sensor could be compared" in season.body
    assert "Consistent with earlier years" in season.body


def test_a_change_three_hours_before_the_time_is_dated_to_when_it_began() -> None:
    """Engineering review 1.4: the reference must not be the analyst's own guess."""
    rows = sensor_rows(photometry(dict.fromkeys(SENSORS, (-3.0, 10.0, 0.5))), A)
    for row in rows:
        assert row.departs
        assert [(s.start_ms - A) / HOUR_MS for s in row.spans] == [-3.0], row.spans
        assert row.spans[0].end_ms == A + 10 * HOUR_MS


def test_a_change_over_before_the_time_is_seen_and_leaves_no_phantoms() -> None:
    rows = sensor_rows(photometry(dict.fromkeys(SENSORS, (-5.0, -1.0, 0.5))), A)
    for row in rows:
        assert row.departs  # it ended within EARLY_MS of the time
        assert [((s.start_ms - A) / HOUR_MS, (s.end_ms - A) / HOUR_MS) for s in row.spans] == [
            (-5.0, -1.0)
        ]


@pytest.mark.parametrize("nightly", [0.03, 0.1])
def test_nightly_sensor_offsets_alone_raise_no_departure(nightly: float) -> None:
    """Real sensors drift night to night by 0.05 to 0.2 mag; that is not a change."""
    for seed in range(10):
        rows = sensor_rows(photometry(seed=seed, nightly_mag=nightly), A)
        assert not any(r.departs for r in rows), [(r.name, r.spans) for r in rows]


def test_a_large_step_far_from_the_time_does_not_fit() -> None:
    manoeuvre = card(made(found_elsets=elsets(step_at_hours=20.0)), "manoeuvre")
    assert manoeuvre.state == "against"
    assert "too far from your time" in manoeuvre.body


def test_a_three_tenths_fade_still_shows_through_nightly_offsets() -> None:
    for seed in range(5):
        fade = dict.fromkeys(SENSORS, (1.0, 6.0, 0.3))
        rows = sensor_rows(photometry(fade, seed=seed, nightly_mag=0.03), A)
        # A sensor whose 7 nights happen to scatter widely may miss it; the others do not.
        departing = sum(r.departs for r in rows)
        assert departing >= 2, (seed, [(r.name, r.scatter_mag) for r in rows])


def test_a_short_baseline_uses_its_hourly_spread_alone() -> None:
    """Fewer than 3 baseline nights: no nightly spread, but the hours still judge."""
    full = photometry(dict.fromkeys(SENSORS, (1.0, 6.0, 0.5)))
    short = full.take(full.epoch_ms >= A - 54 * HOUR_MS)  # two baseline nights
    rows = sensor_rows(short, A)
    assert all(r.scatter_mag is not None for r in rows)
    assert all(r.departs for r in rows)


def test_a_neighbour_with_no_track_in_the_strip_is_not_listed() -> None:
    far = np.array([A - 10 * 86_400_000], dtype=np.int64)
    gone = Approach(40002, int(far[0]), 50.0, True, False, far, np.array([50.0]), ())
    assert passes_in_strip([gone], A, reach_km=237.0) == []


def test_a_step_between_elsets_far_apart_says_its_timing_is_loose() -> None:
    sparse = [
        e for e in elsets(step_at_hours=3.0) if not ANCHOR <= e.epoch < ANCHOR + timedelta(hours=18)
    ]
    manoeuvre = card(made(found_elsets=sparse), "manoeuvre")
    assert manoeuvre.state == "fits"
    assert "so the timing is loose" in manoeuvre.body


def test_the_season_card_does_not_fit_when_the_flag_departs_and_no_sensor_does() -> None:
    season = card(
        made(photometry(), the_flag=flag("departs", "Departs from the seasonal pattern")), "season"
    )
    assert season.state == "against"


def test_one_hour_in_a_second_sensor_confirms_anothers_departure() -> None:
    """Case 4 of the owner's events: EXO1840 brightened 3 mag for 2 hours and
    EXO1841 by as much in one of them; that is two sensors, not one artefact."""
    events = {"EXO1612": (1.0, 3.0, -3.0), "EXO1834": (2.0, 3.0, -3.0)}
    series = photometry(events)
    keep = (series.sensor != 2) | (series.epoch_ms < A)  # EXO1840 silent after the time
    result = made(series.take(keep))
    sensor = card(result, "sensor")
    assert sensor.state == "against"
    assert "EXO1612 and EXO1834 depart the same way (brighter) in the same hours" in sensor.body
    assert ", both sensors watching turn brighter" in result.headline


def test_a_lone_departure_no_other_sensor_observed_cannot_be_judged() -> None:
    series = photometry({"EXO1612": (1.0, 4.0, -1.0)})
    hours = (series.epoch_ms >= A) & (series.epoch_ms < A + 5 * HOUR_MS)
    alone = series.take(~hours | (series.sensor == 0))  # the others silent in those hours
    sensor = card(made(alone), "sensor")
    assert sensor.state == "cannot"
    assert "no other sensor observed those hours" in sensor.body


def test_departures_at_different_times_are_not_one_change_seen_twice() -> None:
    """Engineering review: two unrelated departures read as confirmation."""
    events = {"EXO1612": (-3.0, -1.0, 0.5), "EXO1834": (30.0, 33.0, -0.5)}
    sensor = card(made(photometry(events)), "sensor")
    assert sensor.state == "fits"  # the one nearest the time, the others level then
    assert sensor.body.startswith("Only EXO1612 departs (fainter, 19 Nov 01:30Z")
    assert "EXO1834 also turns brighter later, from 20 Nov 10:30Z" in sensor.body


def test_sensors_departing_opposite_ways_in_the_same_hours_disagree() -> None:
    events = {"EXO1612": (2.0, 5.0, 0.5), "EXO1834": (2.0, 5.0, -0.5)}
    result = made(photometry(events))
    sensor = card(result, "sensor")
    assert sensor.state == "cannot"
    assert "went the other way in those hours, so the sensors disagree" in sensor.body
    assert ", and 1 the other way" in result.headline


def test_one_opposite_hour_is_a_conflict_not_a_level_witness() -> None:
    events = {"EXO1612": (2.0, 5.0, 0.5), "EXO1834": (3.0, 4.0, -0.5)}
    sensor = card(made(photometry(events)), "sensor")
    assert sensor.state == "cannot"
    assert "EXO1834 went the other way" in sensor.body


def test_a_second_departure_by_the_same_sensor_is_named_too() -> None:
    events = {"EXO1612": (2.0, 5.0, 0.5), "EXO1834": (2.0, 5.0, 0.5)}
    series = photometry(events)
    later = (series.sensor == 0) & (series.epoch_ms >= A + 30 * HOUR_MS)
    series = Series(
        series.epoch_ms,
        np.where(later & (series.epoch_ms < A + 33 * HOUR_MS), series.mag - 0.5, series.mag),
        series.sepa,
        series.geolon,
        series.sensor,
        series.sensors,
        series.basis,
    )
    sensor = card(made(series), "sensor")
    assert sensor.state == "against"
    assert "EXO1612 also turns brighter later, from 20 Nov 10:30Z" in sensor.body


def test_a_near_pass_beyond_the_distance_but_within_the_uncertainty_cannot_be_judged() -> None:
    near = [approach(45985, [(A + HOUR_MS, 200.0)])]
    neighbour = card(made(found_elsets=elsets(), found=near), "neighbour")
    assert neighbour.state == "cannot"
    assert "beyond your 150 km but within the elsets' uncertainty" in neighbour.body


def test_a_busy_strip_labels_only_the_passes_nearest_the_time() -> None:
    from lightbee.figures.board import LABELLED_PASSES, board_figure
    from lightbee.figures.common import Caption

    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    many = [approach(40100 + k, [(A + (k - 3) * 6 * HOUR_MS, 100.0 + k)]) for k in range(8)]
    figure = board_figure(made(found_elsets=elsets(), found=many), caption, {})
    (passes,) = [t for t in figure["data"] if t.get("mode") == "markers+text"]
    labelled = [i for i, text in enumerate(passes["text"]) if text]
    assert len(labelled) == LABELLED_PASSES
    assert len(passes["hovertext"]) == 8  # every pass still says what it is on hover
    nearest = sorted(range(8), key=lambda k: abs((k - 3) * 6))[:LABELLED_PASSES]
    assert sorted(labelled) == sorted(nearest)


def test_attitude_fits_a_departure_that_recurs_at_the_same_geometry() -> None:
    """Case 2's shape: brighter for 3 hours, and again at the same hours the next night."""
    events = dict.fromkeys(SENSORS, (1.0, 4.0, -0.5))
    series = photometry(events)
    again = (series.epoch_ms >= A + 25 * HOUR_MS) & (series.epoch_ms < A + 28 * HOUR_MS)
    series = Series(
        series.epoch_ms,
        np.where(again, series.mag - 0.5, series.mag),
        series.sepa,
        series.geolon,
        series.sensor,
        series.sensors,
        series.basis,
    )
    result = made(series)
    attitude = card(result, "attitude")
    assert attitude.state == "fits"
    assert "recurs at the same geometry the next night" in attitude.body
    assert "Photometry cannot say why" in attitude.body
    assert "Attitude" in result.headline


def test_attitude_fits_a_level_step_held_in_several_sensors() -> None:
    attitude = card(made(photometry(dict.fromkeys(SENSORS, (0.0, 48.0, 0.5)))), "attitude")
    assert attitude.state == "fits"
    assert "a level step" in attitude.body
    assert "recurs" not in attitude.body  # a held step is not a repeat


def test_attitude_fits_a_departure_alongside_a_manoeuvre() -> None:
    result = made(photometry(dict.fromkeys(SENSORS, (1.0, 4.0, -0.5))), elsets(step_at_hours=3.0))
    attitude = card(result, "attitude")
    assert card(result, "manoeuvre").state == "fits"
    assert attitude.state == "fits"
    assert "alongside the manoeuvre" in attitude.body


def test_attitude_does_not_fit_one_sensors_artefact() -> None:
    attitude = card(made(photometry({"EXO1834": (0.0, 48.0, 0.4)})), "attitude")
    assert attitude.state == "against"
    assert "EXO1834 alone" in attitude.body


def test_attitude_does_not_fit_a_steady_object_and_cannot_judge_without_sensors() -> None:
    assert card(made(photometry()), "attitude").state == "against"
    assert card(made(), "attitude").state == "cannot"


def test_flicker_onset_in_several_sensors_reads_as_rotation() -> None:
    """Spread within each hour rises fivefold after the time, with no change in level."""
    series = photometry()
    rng = np.random.default_rng(9)
    after = series.epoch_ms >= A
    noisy = series.mag + np.where(after, rng.normal(0.0, 0.2, series.mag.size), 0.0)
    series = Series(
        series.epoch_ms,
        noisy,
        series.sepa,
        series.geolon,
        series.sensor,
        series.sensors,
        series.basis,
    )
    rows = sensor_rows(series, A)
    assert all(r.flicker_ratio is not None and r.flicker_ratio > 2.0 for r in rows)
    steady = sensor_rows(photometry(), A)
    assert all(r.flicker_ratio is not None and r.flicker_ratio < 1.5 for r in steady)
    attitude = card(made(series), "attitude")
    assert attitude.state == "fits"
    assert "as a rotation or tumble gives" in attitude.body


def test_the_equinox_glint_season_is_named_only_near_an_equinox() -> None:
    from lightbee.analysis.triage import _glint_season

    near = int(datetime(2026, 10, 5, 12, tzinfo=UTC).timestamp() * 1000)
    assert "13 days from an equinox" in _glint_season(near)
    assert _glint_season(A) == ""  # 19 November is 58 days on


def test_the_panel_summary_names_who_was_compared_and_who_moved() -> None:
    from lightbee.analysis.triage import marks, summary

    rows = sensor_rows(photometry({"EXO1612": (1.0, 3.0, -3.0), "EXO1834": (2.0, 3.0, -3.0)}), A)
    text = summary(rows, A)
    assert text.startswith("Compared: 3 sensors with a baseline.")
    assert "left their normal range around your time" in text
    assert any(m.name == "EXO1834" for m in marks(rows, A))


def night_only(series: Series) -> Series:
    """Keep 20:00 to 06:00 UTC: real sensors see the object by night only."""
    hour = (series.epoch_ms // HOUR_MS) % 24
    return series.take((hour >= 20) | (hour < 6))


def shifted(series: Series, lo_hours: float, hi_hours: float, by: float) -> Series:
    """The series with every sensor `by` mag fainter between two times from the anchor."""
    t = series.epoch_ms
    inside = (t >= A + lo_hours * HOUR_MS) & (t < A + hi_hours * HOUR_MS)
    return Series(
        series.epoch_ms,
        np.where(inside, series.mag + by, series.mag),
        series.sepa,
        series.geolon,
        series.sensor,
        series.sensors,
        series.basis,
    )


def test_a_lasting_step_seen_night_by_night_reads_as_a_step_not_a_repeat() -> None:
    """Engineering review of 1.5: night-only observing splits a step into one span a night."""
    series = shifted(night_only(photometry()), 16.0, 48.0, 0.4)
    attitude = card(made(series), "attitude")
    assert attitude.state == "fits"
    assert "a level step" in attitude.body
    assert "recurs" not in attitude.body


def test_a_feature_back_at_the_same_hours_the_next_night_reads_as_a_repeat() -> None:
    series = shifted(shifted(night_only(photometry()), 16.0, 18.0, -0.5), 40.0, 42.0, -0.5)
    attitude = card(made(series), "attitude")
    assert attitude.state == "fits"
    assert "recurs at the same geometry" in attitude.body


def test_a_lone_sensor_alongside_a_manoeuvre_is_not_called_an_attitude_change() -> None:
    """Engineering review of 1.5: every attitude signature needs 2 or more sensors."""
    series = photometry({"EXO1612": (1.0, 4.0, -0.6)})
    t = series.epoch_ms
    alone = series.take((series.sensor == 0) | (t < A) | (t >= A + 6 * HOUR_MS))
    result = made(alone, elsets(step_at_hours=3.0))
    assert card(result, "manoeuvre").state == "fits"
    assert card(result, "attitude").state != "fits"


def test_the_strip_reads_out_its_departures_and_draws_band_edges() -> None:
    from lightbee.figures.board import EDGE, board_figure
    from lightbee.figures.common import Caption

    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    figure = board_figure(made(photometry(dict.fromkeys(SENSORS, (1.0, 4.0, 0.3)))), caption, {})
    said = figure["summary"]
    assert "Compared: 3 sensors with a baseline." in said
    assert "Departures: EXO1612 +0." in said
    assert "0 elset steps marked; 0 passes within reach." in said
    shapes = figure["layout"]["shapes"]
    edges = [s for s in shapes if s.get("type") == "line" and s["line"]["color"] == EDGE]
    assert len(edges) == 6  # top and bottom of each of 3 bands
    assert all(t.endswith(" mag") for t in figure["layout"]["yaxis"]["ticktext"])


def test_a_gap_under_an_hour_reads_in_minutes() -> None:
    from lightbee.analysis.triage import apart

    assert apart(A + 24 * 60_000, A) == "24 minutes after your time"
    assert apart(A - 90 * 60_000, A) == "1.5 hours before your time"


def test_the_page_key_uses_the_same_colours_as_the_strip() -> None:
    """Design review of 1.5: the key's swatches and the plot must never drift apart."""
    import re
    from pathlib import Path

    from lightbee.figures import board as strip_figure

    css = (Path(strip_figure.__file__).parents[1] / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", root))

    def same(token: str, constant: str) -> bool:
        return tokens[token].replace(" ", "").lower() == constant.replace(" ", "").lower()

    assert same("plot-band", strip_figure.BAND)
    assert same("plot-within", strip_figure.WITHIN)
    assert same("plot-brighter", strip_figure.BRIGHTER)
    assert same("plot-fainter", strip_figure.FAINTER)
    assert same("control-edge", strip_figure.EDGE)


def test_counts_and_gaps_read_naturally() -> None:
    from lightbee.analysis.triage import _how_many, apart

    assert _how_many(3, 3) == "all 3 sensors"
    assert _how_many(2, 2) == "both sensors"
    assert _how_many(1, 1) == "the 1 sensor"
    assert _how_many(2, 3) == "2 of the 3 sensors"
    assert apart(A + 60_000, A) == "1 minute after your time"
    assert apart(A + 10_000, A) == "under a minute after your time"
    assert apart(A + 59 * 60_000 + 50_000, A) == "1.0 hours after your time"


def test_an_hour_too_thin_to_judge_is_left_off_the_panel() -> None:
    """An hour with 1 or 2 observations has a median, but too few to judge."""
    from lightbee.figures.board import board_figure
    from lightbee.figures.common import Caption

    series = photometry()
    t = series.epoch_ms
    sparse_start = A + 10 * HOUR_MS
    in_hour = (t >= sparse_start) & (t < sparse_start + HOUR_MS)
    first_two = in_hour & (np.cumsum(in_hour) <= 2)
    thin = series.take(~in_hour | first_two)
    result = made(thin)
    row = next(r for r in result.sensors if r.hour_count[34] > 0)
    assert 0 < row.hour_count[34] < 3  # the strip starts 24 hours before the time
    caption = Caption("UNCLASSIFIED", 43874, "test window", "test source", ANCHOR)
    figure = board_figure(result, caption, {})
    hour_text = "19 Nov 14:30Z hour"
    dots = [h for tr in figure["data"] if tr.get("yaxis") == "y" for h in tr.get("hovertext", [])]
    assert not any(hour_text in h for h in dots)
