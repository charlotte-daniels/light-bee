"""Light curve, VisMag v Time, ghost years, orbit series and the Series adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from lightbee.analysis.lightcurve import by_day, by_sensor, ghost_years
from lightbee.analysis.orbit import orbit_series
from lightbee.analysis.series import DAY_MS, Series, concatenate
from lightbee.udl.photometry import chunk_from_records, combine
from lightbee.udl.records import elset
from tests.fake_udl import elset_record, observation

NOW = datetime(2026, 11, 10, tzinfo=UTC)
END = int(NOW.timestamp() * 1000)
YEAR = round(365.2422 * DAY_MS)


def test_days_are_counted_back_from_the_end_with_day_zero_newest() -> None:
    epoch = [END - 1, END - DAY_MS - 1, END - 6 * DAY_MS - 1, END - 8 * DAY_MS]
    series = Series.from_arrays(epoch, [10.0, 11.0, np.nan, 12.0], [0.0, 1.0, 2.0, 3.0])
    week = by_day(series, end_ms=END, days=7)
    assert week.day.tolist() == [0, 1]  # NaN magnitude dropped, 8 days back outside
    assert week.days_ago[0] == pytest.approx(1 / DAY_MS)
    with pytest.raises(ValueError, match="at least one day"):
        by_day(series, end_ms=END, days=0)


def test_sensors_are_listed_busiest_first_with_their_own_points() -> None:
    records = [
        observation(43874, NOW - timedelta(hours=k), k, idSensor=name)
        for k, name in enumerate(["A", "B", "B", "C", "B", "A"], start=1)
    ]
    photometry = combine([chunk_from_records(records, 43874)])
    tracks = by_sensor(Series.of(photometry), start_ms=END - DAY_MS, end_ms=END)
    assert [t.sensor for t in tracks] == ["B", "A", "C"]
    assert tracks[0].epoch_ms.size == 3
    assert np.all(np.diff(tracks[0].epoch_ms) > 0)


def test_series_uses_normalised_magnitude_unless_asked_for_raw() -> None:
    records = [observation(43874, NOW - timedelta(hours=k), k, mag=12.0) for k in range(1, 5)]
    photometry = combine([chunk_from_records(records, 43874)])
    assert Series.of(photometry).basis == "range-normalised"
    assert Series.of(photometry).mag.tolist() == pytest.approx([11.2] * 4)
    raw = Series.of(photometry, normalised=False)
    assert raw.basis == "raw"
    assert raw.mag.tolist() == [12.0] * 4
    assert len(concatenate([])) == 0
    assert raw.nights == 1


def test_ghost_years_take_the_same_window_in_earlier_years() -> None:
    window = (END - 7 * DAY_MS, END)
    epoch = [END - DAY_MS, END - YEAR - DAY_MS, END - 2 * YEAR - DAY_MS, END - YEAR - 30 * DAY_MS]
    series = Series.from_arrays(epoch, [10.0, 11.0, 12.0, 13.0], [5.0, 6.0, 7.0, 8.0])
    ghosts = ghost_years(series, start_ms=window[0], end_ms=window[1])
    assert [(g.years_ago, g.mag.tolist(), g.sepa.tolist()) for g in ghosts] == [
        (1, [11.0], [6.0]),
        (2, [12.0], [7.0]),
    ]
    only_recent = Series.from_arrays([END - DAY_MS], [10.0], [0.0])
    assert ghost_years(only_recent, start_ms=window[0], end_ms=window[1]) == []


def test_orbit_series_keeps_one_provider_inside_the_window_in_epoch_order() -> None:
    items = [
        elset(elset_record(43874, NOW - timedelta(days=d), inclination=0.05 + d / 100, raan=359.5))
        for d in (3, 1, 2)
    ]
    items.append(elset(elset_record(43874, NOW - timedelta(days=1), source="OTHER")))
    items.append(elset(elset_record(43874, NOW - timedelta(days=1))))  # same epoch again
    items.append(elset(elset_record(43874, NOW - timedelta(days=20))))
    items.append(elset(elset_record(43874, NOW - timedelta(days=2, hours=1), raan=0.5)))
    present = [i for i in items if i is not None]
    series = orbit_series(present, "KBR", start_ms=END - 7 * DAY_MS, end_ms=END)
    assert len(series) == 4
    assert np.all(np.diff(series.epoch_ms) > 0)
    assert series.source == "KBR"
    assert np.isnan(series.largest_step("raan_deg"))  # undefined at 0.05 degrees inclination
    assert series.largest_plane_change() > 0.0
    assert series.largest_step("inclination_deg") == pytest.approx(0.03)  # 0.08 to 0.05
    single = orbit_series(present, "OTHER", start_ms=END - 7 * DAY_MS, end_ms=END)
    assert single.largest_step("period_min") == 0.0


def test_raan_steps_wrap_when_the_orbit_is_inclined_enough_to_define_them() -> None:
    items = [
        elset(elset_record(43874, NOW - timedelta(days=d), inclination=2.0, raan=raan))
        for d, raan in ((3, 359.5), (2, 0.5), (1, 0.6))
    ]
    series = orbit_series(
        [i for i in items if i is not None], "KBR", start_ms=END - 7 * DAY_MS, end_ms=END
    )
    assert series.largest_step("raan_deg") == pytest.approx(1.0)  # 359.5 to 0.5 wraps
    assert series.largest_plane_change() == pytest.approx(2.0 * np.deg2rad(1.0), rel=0.01)
    assert orbit_series([], "KBR", start_ms=0, end_ms=1).largest_plane_change() == 0.0
