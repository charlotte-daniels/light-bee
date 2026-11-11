"""The seasonal raster and flag on synthetic objects whose answer is known."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from lightbee.analysis.groups import circular_mean_deg, group_mad_sigma, group_medians, wrap_deg
from lightbee.analysis.seasonal import (
    MIN_COMPARABLE_BINS,
    SeasonalFlag,
    build_grid,
    seasonal_flag,
)
from lightbee.analysis.series import DAY_MS, Series, concatenate

END = int(datetime(2026, 11, 10, tzinfo=UTC).timestamp() * 1000)
WEEK = 7 * DAY_MS


def synthetic(
    days: int = 760,
    *,
    step_after_ms: int | None = None,
    step_mag: float = 0.0,
    longitude: float = 100.0,
    move_after_ms: int | None = None,
    moved_longitude: float = 140.0,
    per_night: int = 60,
) -> Series:
    """A GEO object seen every night across SEPA -100 to 100, with a seasonal cycle.

    Magnitude rises away from zero SEPA and swings 0.4 mag over the year, with
    0.05 mag of noise, so the same season in each year looks the same.
    """
    rng = np.random.default_rng(7)
    night = np.repeat(np.arange(days), per_night)
    epoch = END - (night + 1) * DAY_MS + np.tile(np.linspace(0, DAY_MS - 1, per_night), days)
    epoch = epoch.astype(np.int64)
    sepa = np.tile(np.linspace(-100.0, 100.0, per_night), days)
    season = 0.4 * np.sin(2 * np.pi * (epoch / DAY_MS) / 365.2422)
    mag = 11.0 + 0.03 * np.abs(sepa) + season + rng.normal(0.0, 0.05, epoch.size)
    lon = np.full(epoch.size, longitude)
    if step_after_ms is not None:
        mag = np.where(epoch >= step_after_ms, mag + step_mag, mag)
    if move_after_ms is not None:
        lon = np.where(epoch >= move_after_ms, moved_longitude, lon)
    order = np.argsort(epoch)
    return Series.from_arrays(epoch[order], mag[order], sepa[order], lon[order])


def flag(series: Series) -> SeasonalFlag:
    return seasonal_flag(series, recent_start_ms=END - WEEK, recent_end_ms=END)


def test_group_medians_match_numpy_for_every_group() -> None:
    rng = np.random.default_rng(1)
    keys = rng.integers(0, 40, 5000)
    values = rng.normal(size=5000)
    values[::50] = np.nan
    unique, medians, counts = group_medians(keys, values)
    for key, median, count in zip(unique, medians, counts, strict=True):
        chosen = values[(keys == key) & np.isfinite(values)]
        assert median == pytest.approx(np.median(chosen))
        assert count == chosen.size
    _, _, sigma, _ = group_mad_sigma(keys, values)
    chosen = values[(keys == unique[0]) & np.isfinite(values)]
    assert sigma[0] == pytest.approx(1.4826 * np.median(np.abs(chosen - np.median(chosen))))


def test_empty_groups_and_circular_means() -> None:
    unique, medians, counts = group_medians(np.array([1, 2]), np.array([np.nan, np.nan]))
    assert unique.size == medians.size == counts.size == 0
    assert group_mad_sigma(np.array([], dtype=int), np.array([]))[2].size == 0
    assert circular_mean_deg(np.array([179.0, -179.0])) == pytest.approx(180.0, abs=1e-9) or (
        circular_mean_deg(np.array([179.0, -179.0])) == pytest.approx(-180.0, abs=1e-9)
    )
    assert np.isnan(circular_mean_deg(np.array([np.nan])))
    assert float(wrap_deg(190.0)) == pytest.approx(-170.0)


def test_the_grid_puts_the_most_recent_day_on_row_zero_with_daily_medians() -> None:
    epoch = np.array([END - 1, END - 2, END - 3, END - DAY_MS - 1, END - 400 * DAY_MS])
    series = Series.from_arrays(
        epoch,
        [10.0, 11.0, 12.0, 9.0, 8.0],
        [1.0, 1.5, 0.5, -3.0, 0.0],
        [179.0, -179.0, 179.0, 10, 0],
    )
    grid = build_grid(series, end_ms=END, days=30)
    assert grid.values.shape == (30, 180)
    column = int(np.digitize(1.0, grid.sepa_edges) - 1)
    assert grid.values[0, column] == 11.0
    assert grid.counts[0, column] == 3
    assert grid.values[1, int(np.digitize(-3.0, grid.sepa_edges) - 1)] == 9.0
    assert int(grid.counts.sum()) == 4  # the 400-day-old point is outside the grid
    assert abs(abs(grid.geo_longitude[0]) - 180.0) < 1.0  # averaged on the circle
    assert grid.geo_longitude[1] == pytest.approx(10.0)
    assert np.isnan(grid.geo_longitude[2])
    assert grid.row_start_ms[0] == END - DAY_MS
    assert grid.sepa_centres[0] == -179.0
    with pytest.raises(ValueError, match="at least one day"):
        build_grid(series, end_ms=END, days=0)


def test_an_unchanged_seasonal_object_reads_consistent() -> None:
    result = flag(synthetic())
    assert result.outcome == "consistent"
    assert result.comparable_bins >= MIN_COMPARABLE_BINS
    assert result.context.years_used == (1, 2)
    assert "like for like in longitude" in result.detail


def test_a_step_in_brightness_reads_departs() -> None:
    result = flag(synthetic(step_after_ms=END - WEEK, step_mag=-0.8))
    assert result.outcome == "departs"
    assert "brighter" in result.detail
    assert result.median_delta_mag == pytest.approx(-0.8, abs=0.1)


def test_a_small_step_reads_marginal_and_a_noisy_offset_reads_offset() -> None:
    # 0.17 mag is inside the 0.20 band but outside its comfortable 0.15; earlier
    # years with 0.15 mag of scatter keep the z inside 2 sigma.
    wide = synthetic(step_after_ms=END - WEEK, step_mag=0.17)
    before = wide.epoch_ms < END - WEEK
    wide.mag[before] += np.random.default_rng(5).normal(0.0, 0.15, int(before.sum()))
    marginal = flag(wide)
    assert marginal.outcome == "marginal"
    assert marginal.marginal is True
    noisy = synthetic(step_after_ms=END - WEEK, step_mag=0.4)
    rng = np.random.default_rng(3)
    earlier = noisy.epoch_ms < END - WEEK
    noisy.mag[earlier] += rng.normal(0.0, 0.6, int(earlier.sum()))
    assert flag(noisy).outcome == "offset"


def test_sparse_or_short_histories_read_inconclusive() -> None:
    sparse = flag(synthetic(per_night=2))
    assert sparse.outcome == "inconclusive"
    assert "Widen the window" in sparse.detail
    short = flag(synthetic(days=60))
    assert short.outcome == "inconclusive"
    assert "at least a year" in short.detail
    empty = seasonal_flag(synthetic(), recent_start_ms=END + DAY_MS, recent_end_ms=END + WEEK)
    assert empty.outcome == "inconclusive"
    assert "no usable photometry" in empty.detail


def test_a_relocation_suppresses_the_verdict_and_says_why() -> None:
    moved = flag(synthetic(move_after_ms=END - 30 * DAY_MS))
    assert moved.outcome == "relocated"
    assert moved.context.longitude_shift_deg == pytest.approx(40.0)
    assert "100.0°" in moved.detail
    assert "140.0°" in moved.detail


def test_a_partial_relocation_keeps_the_comparable_part_and_says_so() -> None:
    series = synthetic(move_after_ms=END - 900 * DAY_MS, longitude=140.0, moved_longitude=100.0)
    # Earlier seasons were mostly at 140 degrees; add a slice of history at 100.
    back = synthetic(days=760, longitude=100.0).between(
        END - 365 * DAY_MS - 20 * DAY_MS, END - 365 * DAY_MS + 5 * DAY_MS
    )
    earlier = series.take(series.epoch_ms < END - 30 * DAY_MS)
    earlier = Series.from_arrays(
        earlier.epoch_ms, earlier.mag, earlier.sepa, np.full(len(earlier), 140.0)
    )
    recent = synthetic(longitude=100.0).between(END - 30 * DAY_MS, END)
    combined = concatenate([earlier, back, recent])
    result = flag(combined)
    assert result.context.relocated is True
    assert result.outcome in {"consistent", "marginal", "relocated"}
    assert "set aside" in result.detail or "moved" in result.detail


def test_unknown_longitude_is_stated_not_hidden() -> None:
    base = synthetic()
    blind = Series.from_arrays(base.epoch_ms, base.mag, base.sepa)
    result = flag(blind)
    assert result.outcome == "consistent"
    assert "Longitude is unknown" in result.detail


def test_a_relocation_that_leaves_too_few_comparable_bins_names_the_move() -> None:
    moved = synthetic(longitude=140.0, move_after_ms=END - 30 * DAY_MS, moved_longitude=100.0)
    year = round(365.2422 * DAY_MS)
    sliver = synthetic(per_night=3, longitude=100.0).between(END - year - 3 * DAY_MS, END - year)
    result = flag(concatenate([moved, sliver]))
    assert result.outcome == "relocated"
    assert result.comparable_bins < MIN_COMPARABLE_BINS
    assert result.context.matched_points == len(sliver)
    assert "most likely explanation" in result.detail


def test_drift_inside_the_recent_window_suppresses_the_verdict() -> None:
    """The reviewer's case: 96 to 100.2 degrees through the week, references at 98.1."""
    base = synthetic(longitude=98.1)
    recent = base.epoch_ms >= END - WEEK
    progress = (base.epoch_ms[recent] - (END - WEEK)) / WEEK
    base.geolon[recent] = 96.0 + 4.2 * progress
    result = flag(base)
    assert result.outcome == "drifting"
    assert result.context.recent_spread_deg > 3.0
    assert "96." in result.detail
    assert "100." in result.detail
    assert "throughout" not in result.detail


def test_station_keeping_and_a_stray_longitude_do_not_count_as_drift() -> None:
    base = synthetic()
    recent = np.flatnonzero(base.epoch_ms >= END - WEEK)
    base.geolon[recent] += np.random.default_rng(2).uniform(-0.05, 0.05, recent.size)
    base.geolon[recent[5]] = 150.0  # one bad value
    result = flag(base)
    assert result.outcome == "consistent"
    assert result.context.recent_spread_deg < 0.2


def test_a_sepa_of_exactly_180_lands_in_the_last_bin() -> None:
    series = Series.from_arrays([END - 1, END - 2], [10.0, 11.0], [180.0, -180.0])
    grid = build_grid(series, end_ms=END, days=1)
    assert grid.counts[0, -1] == 1
    assert grid.counts[0, 0] == 1


@pytest.mark.parametrize("share", [0.04, 0.01])
def test_a_move_in_the_last_hours_of_the_window_is_caught(share: float) -> None:
    """The reviewer's case: 98.1 degrees, then 101.5 for the last few hours only."""
    base = synthetic(longitude=98.1)
    moved = base.epoch_ms >= END - round(share * WEEK)
    base.geolon[moved] = 101.5
    result = flag(base)
    assert result.outcome == "drifting"
    assert result.context.recent_spread_deg == pytest.approx(3.4, abs=0.1)
    assert "101.5" in result.detail
    assert "throughout" not in result.detail


def test_drift_with_very_few_recent_observations() -> None:
    base = synthetic(per_night=1)
    recent = np.flatnonzero(base.epoch_ms >= END - WEEK)
    assert recent.size == 7
    base.geolon[recent[-1]] = 110.0  # one stray among seven is rejected
    assert flag(base).context.recent_spread_deg < 0.1
    few = base.take(base.epoch_ms < END - 4 * DAY_MS)  # three recent observations
    result = seasonal_flag(few, recent_start_ms=END - WEEK, recent_end_ms=END)
    assert result.context.recent_spread_deg == 0.0
    assert result.outcome != "drifting"


def test_each_earlier_season_is_named_at_its_own_longitude() -> None:
    from lightbee.analysis.seasonal import Context

    context = Context(
        (2,), 175.4, 128.7, 2.0, 7, 30, 10, 20, season_longitudes=((1, 82.0), (2, 175.4))
    )
    assert context.where_then == "at 82.0°E 1 year ago and 175.4°E 2 years ago"
    bare = Context((), 100.0, 140.0, 2.0, 7, 0, 0, 0)
    assert bare.where_then == "at 140.0°E in the reference seasons"
