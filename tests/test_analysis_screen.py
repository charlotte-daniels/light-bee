"""The neighbourhood screen on synthetic GEO objects whose separations are known."""

from __future__ import annotations

import itertools
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from lightbee.analysis.screen import (
    MAX_CANDIDATES,
    MAX_ECCENTRICITY,
    MAX_SCAN_TIMES,
    SGP4_UNCERTAINTY_KM,
    ScreenError,
    _minima,
    _refine,
    approaches,
    candidates,
    scan,
    scan_times,
    separation_at,
    sweep_slack_km,
)
from lightbee.analysis.series import DAY_MS
from lightbee.udl.records import Elset, elset
from tests.fake_udl import elset_record

EPOCH = datetime(2026, 11, 7, tzinfo=UTC)
T0 = int(EPOCH.timestamp() * 1000)
GEO_RADIUS_KM = 42_164.0
TARGET = 43874


def geo(sat_no: int, *, offset_deg: float = 0.0, **overrides: Any) -> Elset:
    """A near-circular GEO elset; `offset_deg` moves it along the orbit from the target."""
    item = elset(elset_record(sat_no, EPOCH, meanAnomaly=(270.0 + offset_deg) % 360.0, **overrides))
    assert item is not None
    return item


def offset_for(km: float) -> float:
    return math.degrees(km / GEO_RADIUS_KM)


def test_a_known_along_track_offset_propagates_to_the_right_separation() -> None:
    times = T0 + np.arange(0, DAY_MS, 3_600_000)
    distance = separation_at(geo(TARGET), geo(40001, offset_deg=offset_for(100.0)), times)
    assert distance == pytest.approx(np.full(times.size, 100.0), rel=0.02)


def test_a_selection_finds_close_objects_in_and_out_of_plane_and_excludes_the_far() -> None:
    catalogue = {
        40001: geo(40001, offset_deg=offset_for(100.0)),
        40002: geo(40002, offset_deg=offset_for(3_000.0)),
        # 8 degrees out of plane, but 50 km away at the node: close, so listed.
        40003: geo(40003, offset_deg=offset_for(50.0), inclination=8.0),
        40004: geo(40004, offset_deg=offset_for(200.0)),  # inside the uncertainty band
        TARGET: geo(TARGET),
    }
    selection = T0 + np.arange(0, 3 * 3_600_000, 1_000)
    result = scan(selection, [geo(TARGET)], catalogue, threshold_km=150.0)
    assert [n.sat_no for n in result.neighbours] == [40003, 40001, 40004]
    _, first, second = result.neighbours
    assert first.min_km == pytest.approx(100.0, rel=0.02)
    assert first.inside is True
    assert first.borderline is True  # 100 km is within 87 km of 150
    assert second.inside is False
    assert second.borderline is True
    assert first.separations_km.size == result.times_ms.size == 180
    assert first.elset_age_hours < 3.0
    assert result.screened == 4  # the target is not propagated
    assert result.thinned is False


def test_a_selection_across_several_dates_lists_every_time() -> None:
    catalogue = {40001: geo(40001, offset_deg=offset_for(100.0))}
    days = [T0 + d * DAY_MS + np.arange(0, 600_000, 10_000) for d in (0, 1, 3)]
    result = scan(np.concatenate(days), [geo(TARGET)], catalogue)
    assert result.times_ms.size == 30
    covered = np.unique((result.times_ms - T0) // DAY_MS)
    assert covered.tolist() == [0, 1, 3]
    assert np.isfinite(result.neighbours[0].separations_km).all()


def test_long_selections_are_thinned_and_say_so() -> None:
    times, thinned = scan_times(range(T0, T0 + (MAX_SCAN_TIMES + 500) * 60_000, 60_000))
    assert thinned is True
    assert times.size == MAX_SCAN_TIMES
    few, thinned = scan_times([T0, T0 + 1, T0 + 59_999, T0 + 60_000])
    assert few.size == 2
    assert thinned is False


def test_gaps_within_a_pass_are_filled_so_drawing_density_does_not_matter() -> None:
    minute = 60_000
    # Two points 5 minutes apart are one pass: every minute between is scanned.
    times, _ = scan_times([T0, T0 + 5 * minute])
    assert times.size == 6
    # Points 30 minutes apart are separate passes: nothing is invented between.
    apart, _ = scan_times([T0, T0 + 30 * minute])
    assert apart.size == 2
    one, _ = scan_times([T0])
    assert one.size == 1


def test_the_target_uses_its_nearest_elset_at_each_time() -> None:
    later = elset(elset_record(TARGET, EPOCH + timedelta(days=2), meanAnomaly=270.0))
    assert later is not None
    catalogue = {40001: geo(40001, offset_deg=offset_for(100.0))}
    selection = [T0 + 3_600_000, T0 + 2 * DAY_MS + 3_600_000]
    result = scan(selection, [geo(TARGET), later], catalogue)
    # The later target elset restarts the target's mean anomaly at its own epoch,
    # so the neighbour (propagated from the first epoch) is far away by then.
    assert result.neighbours[0].separations_km[0] == pytest.approx(100.0, rel=0.02)
    assert result.neighbours[0].separations_km[1] > 1_000.0


def test_bad_requests_are_refused_with_a_reason() -> None:
    with pytest.raises(ScreenError, match="between 1 and 2000 km"):
        scan([T0], [geo(TARGET)], {}, threshold_km=0.5)
    with pytest.raises(ScreenError, match="no observations"):
        scan([], [geo(TARGET)], {})
    with pytest.raises(ScreenError, match="no elset for the target"):
        scan([T0], [], {})
    with pytest.raises(ScreenError, match="end after it starts"):
        approaches(T0, T0, [geo(TARGET)], {})
    with pytest.raises(ScreenError, match="at most 31 days"):
        approaches(T0, T0 + 32 * DAY_MS, [geo(TARGET)], {})
    assert scan([T0], [geo(TARGET)], {}).neighbours == []
    assert approaches(T0, T0 + DAY_MS, [geo(TARGET)], {}) == []


def test_approaches_over_the_window_find_and_refine_the_closest_pass() -> None:
    # A neighbour drifting slowly past: slightly different mean motion.
    drifting = geo(40005, offset_deg=-offset_for(400.0), meanMotion=1.0027 + 0.0006)
    catalogue = {40005: drifting, 40002: geo(40002, offset_deg=offset_for(3_000.0))}
    found = approaches(T0, T0 + 7 * DAY_MS, [geo(TARGET)], catalogue, threshold_km=150.0)
    assert [a.sat_no for a in found] == [40005]
    closest = found[0]
    assert closest.range_km < 150.0
    assert closest.inside is True
    assert T0 < closest.tca_ms < T0 + 7 * DAY_MS
    # The refined minimum is no worse than the coarse track's.
    assert closest.range_km <= float(np.nanmin(closest.track_km)) + 1e-6
    assert closest.track_ms.size == closest.track_km.size == 7 * 24 * 12 + 1


def test_candidates_exclude_the_target_but_not_objects_in_another_plane() -> None:
    catalogue = {TARGET: geo(TARGET), 40001: geo(40001), 40003: geo(40003, inclination=8.0)}
    assert [c.sat_no for c in candidates(catalogue, exclude=TARGET)] == [40001, 40003]
    assert SGP4_UNCERTAINTY_KM == 87.0


def test_a_full_geo_snapshot_scans_well_inside_fifteen_seconds() -> None:
    rng = np.random.default_rng(11)
    catalogue = {
        40000 + k: geo(
            40000 + k,
            offset_deg=float(rng.uniform(0, 360)),
            inclination=float(rng.uniform(0.0, 15.0)),  # the whole GEO belt, old drifters included
            raan=float(rng.uniform(0, 359)),
        )
        for k in range(900)
    }
    selection = np.concatenate(
        [T0 + d * DAY_MS + np.arange(0, 8 * 3_600_000, 5_000) for d in range(7)]
    )
    began = time.perf_counter()
    result = scan(selection, [geo(TARGET)], catalogue)
    window = approaches(T0, T0 + 7 * DAY_MS, [geo(TARGET)], catalogue)
    elapsed = time.perf_counter() - began
    assert result.screened == 900
    assert result.times_ms.size == 7 * 8 * 60
    assert isinstance(window, list)
    assert elapsed < 15.0, f"scan and window sweep took {elapsed:.1f} s"


def test_eccentric_objects_are_left_out_and_a_flood_of_candidates_is_refused() -> None:
    catalogue = {40001: geo(40001, eccentricity=MAX_ECCENTRICITY + 0.05), 40002: geo(40002)}
    assert [c.sat_no for c in candidates(catalogue, exclude=TARGET)] == [40002]
    flood = {40000 + k: geo(40000 + k) for k in range(MAX_CANDIDATES + 1)}
    with pytest.raises(ScreenError, match="more than the"):
        candidates(flood, exclude=TARGET)


def test_a_node_crossing_between_coarse_samples_is_found_at_a_one_km_threshold() -> None:
    crossing = geo(40006, inclination=3.0)  # meets the target at each node, about 160 m/s apart
    start = T0 - 150_000  # every crossing falls midway between two 5 minute samples
    found = approaches(start, start + DAY_MS, [geo(TARGET)], {40006: crossing}, threshold_km=1.0)
    assert [a.sat_no for a in found] == [40006]
    assert found[0].range_km < 1.0
    assert found[0].inside is True


def test_a_neighbour_four_degrees_out_of_plane_is_still_screened() -> None:
    """Case 1 of the owner's known events: Express 103 at 0.01 degrees passed
    104 km from 50321 at 4.08 degrees, and the old 3 degree plane cut hid it."""
    target = geo(TARGET, inclination=4.08)
    neighbour = geo(40008, inclination=0.01, offset_deg=offset_for(60.0))
    found = approaches(T0, T0 + DAY_MS, [target], {40008: neighbour}, threshold_km=150.0)
    assert [a.sat_no for a in found] == [40008]
    assert len(found[0].passes) >= 2  # it meets the target at each node, twice a day


def test_local_minima_are_all_candidates_for_refinement_lowest_first() -> None:
    row = np.array([5.0, 3.0, 4.0, 9.0, 1.0, 2.0, np.nan, 0.5])
    assert _minima(row, cut=4.0).tolist() == [7, 4, 1]
    assert _minima(row, cut=0.1).tolist() == []
    assert _minima(np.array([np.nan, np.nan]), cut=10.0).size == 0


def test_a_refinement_with_no_valid_position_reports_infinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SGP4 reports an error (a decayed or nonsensical orbit) for every instant."""
    from lightbee.analysis import screen

    monkeypatch.setattr(
        screen, "_positions", lambda models, times: np.full((len(models), times.size, 3), np.nan)
    )
    assert _refine(geo(40007), [geo(TARGET)], T0, T0 + 60_000) == (T0, float("inf"))


def test_the_reported_elset_age_includes_the_targets_own() -> None:
    old_target = elset(elset_record(TARGET, EPOCH - timedelta(days=2), meanAnomaly=270.0))
    assert old_target is not None
    catalogue = {40001: geo(40001, offset_deg=offset_for(100.0))}
    result = scan([T0 + 60_000], [old_target], catalogue, threshold_km=2_000.0)
    assert result.neighbours[0].elset_age_hours >= 48.0


def test_a_fast_crossing_between_inclined_orbits_is_found_between_samples() -> None:
    """The reviewer's case: both at 15 degrees, nodes opposite, meeting about 1.6 km/s apart."""
    target = geo(TARGET, inclination=15.0, raan=0.0)
    crossing = geo(40008, offset_deg=180.0, inclination=15.0, raan=180.0)  # mean anomaly 90
    start = T0 - 150_000  # the meeting falls midway between two 5 minute samples
    found = approaches(start, start + 6 * 3_600_000, [target], {40008: crossing}, threshold_km=5.0)
    assert [a.sat_no for a in found] == [40008]
    # The true closest approach (perturbations included) is 2.38 km, measured
    # independently by the Phase 2 reviewer; the old 10 s grid reported 4.9 km.
    assert found[0].range_km == pytest.approx(2.38, abs=0.05)
    assert found[0].inside is True
    assert sweep_slack_km(crossing, target) > 230.0
    assert sweep_slack_km(geo(40009), geo(TARGET)) < 25.0


def test_the_plane_cut_uses_the_target_elset_nearest_the_window() -> None:
    old = elset(elset_record(TARGET, EPOCH - timedelta(days=700), inclination=4.0))
    assert old is not None
    catalogue = {40001: geo(40001, offset_deg=offset_for(100.0))}  # at 0.05 degrees
    result = scan([T0 + 60_000], [old, geo(TARGET)], catalogue)
    assert result.screened == 1  # the cut is centred on the current 0.05, not the old 4.0


def test_a_screen_past_its_deadline_stops_with_a_reason() -> None:
    catalogue = {40001: geo(40001, offset_deg=offset_for(100.0))}
    with pytest.raises(ScreenError, match="time limit"):
        approaches(T0, T0 + DAY_MS, [geo(TARGET)], catalogue, deadline=time.monotonic() - 1)
    with pytest.raises(ScreenError, match="time limit"):
        scan([T0], [geo(TARGET)], catalogue, deadline=time.monotonic() - 1)
    assert scan([T0], [geo(TARGET)], catalogue, deadline=time.monotonic() + 60).neighbours


def test_every_pass_of_a_neighbour_is_kept_not_only_the_closest() -> None:
    # Co-located but 0.2 degrees out of the target's plane: it meets the
    # target at each node crossing, twice a day, and drifts apart between.
    crossing = geo(40007, inclination=geo(TARGET).inclination + 0.2)
    found = approaches(T0, T0 + 2 * DAY_MS, [geo(TARGET)], {40007: crossing}, threshold_km=150.0)
    (neighbour,) = found
    assert len(neighbour.passes) >= 3
    times = [t for t, _ in neighbour.passes]
    assert times == sorted(times)
    # Distinct passes, roughly half a day apart, each within reach.
    assert all(b - a > 6 * 3_600_000 for a, b in itertools.pairwise(times))
    assert all(km <= 150.0 + SGP4_UNCERTAINTY_KM for _, km in neighbour.passes)
    # The closest pass is still the headline one.
    assert (neighbour.tca_ms, neighbour.range_km) == min(neighbour.passes, key=lambda p: p[1])


def test_dips_count_as_separate_passes_only_when_the_range_opens_between_them() -> None:
    from lightbee.analysis.screen import PASS_RISE_KM, _distinct_passes

    minute = 60_000
    times = np.arange(0, 400 * minute, 5 * minute, dtype=np.int64)
    # A crossing: the range opens to 300 km between two dips half a day apart.
    opened = np.full(times.size, 300.0)
    assert _distinct_passes([(10 * minute, 50.0), (200 * minute, 70.0)], times, opened) == [
        (10 * minute, 50.0),
        (200 * minute, 70.0),
    ]
    # A co-located wobble: the range never opens by PASS_RISE_KM, so one encounter,
    # kept at its closest; two refinements of one pass likewise.
    wobble = np.full(times.size, 60.0 + PASS_RISE_KM / 2)
    dips = [(10 * minute, 60.0), (70 * minute, 55.0), (130 * minute, 58.0)]
    assert _distinct_passes(dips, times, wobble) == [(70 * minute, 55.0)]
    assert _distinct_passes([(0, 60.0), (5 * minute, 50.0)], times, opened) == [(5 * minute, 50.0)]
    assert _distinct_passes([], times, opened) == []
