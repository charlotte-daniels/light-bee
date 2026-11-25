"""The neighbourhood screen: which GEO objects came within the screening distance.

Two questions, both answered by propagating the GEO catalogue snapshot with
SGP4 (vectorised over objects and times) and measuring the distance to the
target. Separations between two objects at the same instant do not depend on
the frame, so positions stay in TEME, as SGP4 gives them.

* `scan`: for a drag selection, every neighbour within reach and its
  separation at every selected time, across as many dates as the selection
  spans.
* `approaches`: over the whole window, each neighbour's closest approach,
  found on a 5 minute sweep and refined to a tenth of a second, with its track for the
  close approaches against time panel.

Accuracy, measured by the predecessor rather than assumed: SGP4 positions for
a GEO object against positions from its own observations differed by 1.8 km
at the median and 87 km at the 95th percentile. So the screen searches that
far beyond the threshold and marks results inside that band as borderline,
rather than drawing a hard line it has no right to draw. It finds neighbours;
it does not assess conjunctions.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from sgp4.api import WGS72, Satrec, SatrecArray

from lightbee.analysis.groups import FloatArray, IntArray
from lightbee.analysis.series import DAY_MS
from lightbee.udl.records import Elset

DEFAULT_SCREEN_KM: Final = 150.0
SCREEN_KM_RANGE: Final = (1.0, 2_000.0)
# An object 3 degrees out of plane cannot come close without a very large
# relative velocity; the predecessor screened with the same cut.
SCREEN_INCLINATION_DEG: Final = 3.0
# Near-circular objects only, as the GEO belt is.
MAX_ECCENTRICITY: Final = 0.1
# Circular GEO speed. Two orbits whose planes differ by an angle a, with
# eccentricities e1 and e2, cannot pass faster than about
# V (2 sin(a / 2) + e1 + e2); a margin covers the small speed differences of
# slightly different mean motions. Between two samples a pass can hide at most
# half a step's travel at that speed, so each candidate's sweep cut is widened
# by exactly that much.
GEO_SPEED_KM_S: Final = 3.075
SPEED_MARGIN_KM_S: Final = 0.05
SGP4_UNCERTAINTY_KM: Final = 87.0
# Selections are scanned at one-minute resolution, at most this many instants;
# a longer selection is thinned evenly and says so.
SCAN_RESOLUTION_MS: Final = 60_000
# Selected instants this close are taken as one pass and every minute between
# them is scanned, so the scan does not depend on how thinly the plot is drawn.
FILL_GAP_MINUTES: Final = 10
MAX_SCAN_TIMES: Final = 4_000
COARSE_STEP_MS: Final = 5 * 60_000
REFINE_STEP_MS: Final = 10_000
FINE_STEP_MS: Final = 100
REFINE_HALF_WIDTH_MS: Final = 15 * 60_000
# Two dips in a neighbour's distance are separate passes only if it opens by
# this much between them; less is one long encounter, not two.
PASS_RISE_KM: Final = 25.0
MAX_REFINEMENTS: Final = 20  # local minima refined per neighbour
MAX_APPROACH_WINDOW_MS: Final = 31 * 86_400_000
# Far above the roughly 900 GEO objects seen live, so it only trips on a reply
# that is not a GEO snapshot at all; then the screen refuses rather than grind.
MAX_CANDIDATES: Final = 3_000
# Objects propagated together, so memory stays flat however long the window.
BLOCK: Final = 200

_UNIX_JD: Final = 2_440_587.5
_SGP4_EPOCH_JD: Final = 2_433_281.5  # 1949-12-31 00:00 UTC, SGP4's epoch origin
_MINUTES_PER_DAY: Final = 1440.0
_TWO_PI: Final = 2.0 * np.pi
_HOUR_MS: Final = 3_600_000


class ScreenError(ValueError):
    """The screen cannot run, for a reason the analyst can act on."""


def satrec(item: Elset) -> Satrec:
    """An SGP4 satellite from mean elements, in the units sgp4init expects."""
    epoch_jd = _UNIX_JD + item.epoch.timestamp() / 86_400.0
    model = Satrec()
    model.sgp4init(
        WGS72,
        "i",
        item.sat_no,
        epoch_jd - _SGP4_EPOCH_JD,
        item.b_star,
        item.mean_motion_dot * _TWO_PI / _MINUTES_PER_DAY**2,
        item.mean_motion_ddot * _TWO_PI / _MINUTES_PER_DAY**3,
        item.eccentricity,
        np.deg2rad(item.arg_of_perigee),
        np.deg2rad(item.inclination),
        np.deg2rad(item.mean_anomaly),
        item.mean_motion * _TWO_PI / _MINUTES_PER_DAY,
        np.deg2rad(item.raan),
    )
    return model


def _julian(times_ms: IntArray) -> tuple[FloatArray, FloatArray]:
    """Julian date split into whole and fraction, for full precision in SGP4."""
    days = times_ms // DAY_MS
    return _UNIX_JD + days.astype(np.float64), (times_ms % DAY_MS) / DAY_MS


def _epoch_ms(item: Elset) -> int:
    return round(item.epoch.timestamp() * 1000)


def _positions(models: Sequence[Satrec], times_ms: IntArray) -> FloatArray:
    """Positions in km, shape (objects, times, 3); NaN where SGP4 reports an error."""
    jd, fr = _julian(times_ms)
    errors, positions, _ = SatrecArray(list(models)).sgp4(jd, fr)
    positions[errors != 0] = np.nan
    return np.asarray(positions, dtype=np.float64)


def _target_positions(targets: Sequence[Elset], times_ms: IntArray) -> tuple[FloatArray, IntArray]:
    """The target at each time from its elset nearest in epoch, shape (times, 3), and the
    distance in time from that elset's epoch."""
    if not targets:
        raise ScreenError("there is no elset for the target in this window")
    epochs = np.array([_epoch_ms(t) for t in targets], dtype=np.int64)
    gaps = np.abs(times_ms[:, None] - epochs[None, :])
    nearest = gaps.argmin(axis=1)
    result = np.full((times_ms.size, 3), np.nan)
    for index in np.unique(nearest):
        mine = nearest == index
        result[mine] = _positions([satrec(targets[int(index)])], times_ms[mine])[0]
    return result, gaps[np.arange(times_ms.size), nearest]


def _reference(targets: Sequence[Elset], times_ms: IntArray) -> Elset:
    """The target elset nearest the middle of the times, for the plane cut and speed bound.

    Called only after `_target_positions`, which refuses an empty list."""
    middle = int(times_ms[times_ms.size // 2])
    return min(targets, key=lambda t: abs(_epoch_ms(t) - middle))


def _normal(item: Elset) -> FloatArray:
    inclination, node = np.deg2rad(item.inclination), np.deg2rad(item.raan)
    return np.array(
        [
            np.sin(inclination) * np.sin(node),
            -np.sin(inclination) * np.cos(node),
            np.cos(inclination),
        ]
    )


def sweep_slack_km(item: Elset, target: Elset) -> float:
    """How far below a 5 minute sample a pass with this candidate could hide."""
    cosine = float(np.clip(_normal(item) @ _normal(target), -1.0, 1.0))
    plane = np.arccos(cosine)
    speed = GEO_SPEED_KM_S * (2.0 * np.sin(plane / 2.0) + item.eccentricity + target.eccentricity)
    return float((speed + SPEED_MARGIN_KM_S) * COARSE_STEP_MS / 2_000)


def candidates(catalogue: Mapping[int, Elset], target: Elset, *, exclude: int) -> list[Elset]:
    """Near-circular GEO objects close enough in plane to matter, the target excluded."""
    chosen = [
        item
        for sat_no, item in sorted(catalogue.items())
        if sat_no != exclude
        and item.eccentricity <= MAX_ECCENTRICITY
        and abs(item.inclination - target.inclination) <= SCREEN_INCLINATION_DEG
    ]
    if len(chosen) > MAX_CANDIDATES:
        raise ScreenError(
            f"the GEO snapshot holds {len(chosen):,} candidate objects, more than the "
            f"{MAX_CANDIDATES:,} a real snapshot holds, so the screen was not run"
        )
    return chosen


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise ScreenError("the screen took longer than its time limit; narrow the window")


def _distances(
    near: Sequence[Elset], times: IntArray, target_r: FloatArray, deadline: float | None = None
) -> Iterator[FloatArray]:
    """Each candidate's distance from the target at every time, propagated in blocks."""
    for first in range(0, len(near), BLOCK):
        _check(deadline)
        block = [satrec(item) for item in near[first : first + BLOCK]]
        yield from np.linalg.norm(_positions(block, times) - target_r, axis=2)


@dataclass(frozen=True)
class Neighbour:
    """One object that came within reach during a selection."""

    sat_no: int
    min_km: float
    min_at_ms: int
    inside: bool  # the nearest point is within the screening distance
    borderline: bool  # within the SGP4 uncertainty of the screening distance
    elset_age_hours: float  # the larger of the two propagation spans at the nearest point
    separations_km: FloatArray  # at every scanned time


@dataclass(frozen=True)
class Scan:
    """The neighbours of the target across a selection."""

    times_ms: IntArray
    neighbours: list[Neighbour]
    threshold_km: float
    screened: int  # objects propagated after the plane and eccentricity cuts
    thinned: bool  # the selection had more instants than MAX_SCAN_TIMES


def scan_times(selected_ms: Iterable[int]) -> tuple[IntArray, bool]:
    """Selected instants at one-minute resolution, gaps within a pass filled, thinned to the cap."""
    minutes = np.unique(np.fromiter(selected_ms, dtype=np.int64) // SCAN_RESOLUTION_MS)
    minutes = _filled(minutes)
    times = minutes * SCAN_RESOLUTION_MS + SCAN_RESOLUTION_MS // 2
    if times.size <= MAX_SCAN_TIMES:
        return times, False
    keep = np.linspace(0, times.size - 1, MAX_SCAN_TIMES).round().astype(np.int64)
    return times[np.unique(keep)], True


def _filled(minutes: IntArray) -> IntArray:
    """Every minute between neighbouring selected minutes up to FILL_GAP_MINUTES apart."""
    if minutes.size < 2:  # a gap needs two minutes
        return minutes
    gaps = np.diff(minutes)
    short = (gaps > 1) & (gaps <= FILL_GAP_MINUTES)
    if not short.any():
        return minutes
    starts = minutes[:-1][short]
    steps = np.arange(1, FILL_GAP_MINUTES, dtype=np.int64)
    between = (starts[:, None] + steps[None, :])[steps[None, :] < gaps[short][:, None]]
    return np.unique(np.concatenate([minutes, between]))


def _checked_threshold(threshold_km: float) -> float:
    low, high = SCREEN_KM_RANGE
    if not low <= threshold_km <= high:
        raise ScreenError(f"the screening distance must be between {low:g} and {high:g} km")
    return threshold_km


def scan(
    selected_ms: Iterable[int],
    targets: Sequence[Elset],
    catalogue: Mapping[int, Elset],
    *,
    threshold_km: float = DEFAULT_SCREEN_KM,
    deadline: float | None = None,
) -> Scan:
    """Every neighbour within the screening distance (plus the uncertainty band) at any
    selected instant, nearest first, with its separation at every instant."""
    threshold = _checked_threshold(threshold_km)
    times, thinned = scan_times(selected_ms)
    if times.size == 0:
        raise ScreenError("the selection contains no observations")
    target_r, target_age = _target_positions(targets, times)
    reference = _reference(targets, times)
    near = candidates(catalogue, reference, exclude=reference.sat_no)
    neighbours = []
    reach = threshold + SGP4_UNCERTAINTY_KM
    for item, row in zip(near, _distances(near, times, target_r, deadline), strict=True):
        if not np.isfinite(row).any() or np.nanmin(row) > reach:
            continue
        at = int(np.nanargmin(row))
        age = max(abs(int(times[at]) - _epoch_ms(item)), int(target_age[at]))
        nearest = float(row[at])
        neighbours.append(
            Neighbour(
                sat_no=item.sat_no,
                min_km=nearest,
                min_at_ms=int(times[at]),
                inside=nearest <= threshold,
                borderline=abs(nearest - threshold) <= SGP4_UNCERTAINTY_KM,
                elset_age_hours=age / _HOUR_MS,
                separations_km=row,
            )
        )
    neighbours.sort(key=lambda n: n.min_km)
    return Scan(times, neighbours, threshold, len(near), thinned)


@dataclass(frozen=True)
class Approach:
    """One neighbour's closest approach over the window, with its coarse track.

    `passes` holds every refined pass within reach, in time order, as
    (time ms, km): a neighbour can pass more than once in a window, and the
    closest pass need not be the one that matters (1.4, owner's case 2).
    """

    sat_no: int
    tca_ms: int
    range_km: float
    inside: bool
    borderline: bool
    track_ms: IntArray
    track_km: FloatArray
    passes: tuple[tuple[int, float], ...] = ()


def _distinct_passes(
    found: list[tuple[int, float]], times: IntArray, track: FloatArray
) -> list[tuple[int, float]]:
    """Passes in time order. Two dips count as separate passes only when the
    coarse distance rises at least PASS_RISE_KM above both between them; a
    co-located neighbour wobbling at a near-constant range is one encounter,
    kept once at its closest."""
    kept: list[tuple[int, float]] = []
    for tca, distance in sorted(found):
        if kept:
            between = track[(times > kept[-1][0]) & (times < tca)]
            rise = float(np.nanmax(between)) if between.size else -np.inf
            if rise - max(distance, kept[-1][1]) < PASS_RISE_KM:
                if distance < kept[-1][1]:
                    kept[-1] = (tca, distance)
                continue
        kept.append((tca, distance))
    return kept


def approaches(
    start_ms: int,
    end_ms: int,
    targets: Sequence[Elset],
    catalogue: Mapping[int, Elset],
    *,
    threshold_km: float = DEFAULT_SCREEN_KM,
    deadline: float | None = None,
) -> list[Approach]:
    """Each neighbour's closest approach in the window, nearest first.

    Every local minimum of the 5 minute sweep that could hide a pass inside
    reach (within half a step's travel at that candidate's bounding relative
    speed) is refined, not only the lowest one, because the
    lowest sample need not be nearest the true closest approach.
    """
    threshold = _checked_threshold(threshold_km)
    if not 0 < end_ms - start_ms <= MAX_APPROACH_WINDOW_MS:
        raise ScreenError("the window must end after it starts and span at most 31 days")
    times = np.arange(start_ms, end_ms + 1, COARSE_STEP_MS, dtype=np.int64)
    target_r, _ = _target_positions(targets, times)
    reference = _reference(targets, times)
    near = candidates(catalogue, reference, exclude=reference.sat_no)
    found = []
    reach = threshold + SGP4_UNCERTAINTY_KM
    for item, row in zip(near, _distances(near, times, target_r, deadline), strict=True):
        refined_passes: list[tuple[int, float]] = []
        for at in _minima(row, reach + sweep_slack_km(item, reference)):
            _check(deadline)
            centre = int(times[at])
            span = (
                max(start_ms, centre - REFINE_HALF_WIDTH_MS),
                min(end_ms, centre + REFINE_HALF_WIDTH_MS),
            )
            refined_passes.append(_refine(item, targets, *span))
        within = _distinct_passes([p for p in refined_passes if p[1] <= reach], times, row)
        if within:
            tca, distance = min(within, key=lambda p: p[1])
            inside = distance <= threshold
            borderline = abs(distance - threshold) <= SGP4_UNCERTAINTY_KM
            passes = tuple((int(t), float(d)) for t, d in within)
            found.append(
                Approach(item.sat_no, tca, distance, inside, borderline, times, row, passes)
            )
    found.sort(key=lambda a: a.range_km)
    return found


def _minima(row: FloatArray, cut: float) -> IntArray:
    """Indices of local minima at or below the cut, lowest first, at most MAX_REFINEMENTS."""
    if row.size == 0 or not np.isfinite(row).any():
        return np.empty(0, dtype=np.int64)
    padded = np.concatenate(([np.inf], np.where(np.isfinite(row), row, np.inf), [np.inf]))
    middle = padded[1:-1]
    local = (middle <= padded[:-2]) & (middle <= padded[2:]) & (middle <= cut)
    indices = np.flatnonzero(local)
    return indices[np.argsort(middle[indices], kind="stable")][:MAX_REFINEMENTS]


def _closest(item: Elset, targets: Sequence[Elset], fine: IntArray) -> tuple[int, float]:
    target_r, _ = _target_positions(targets, fine)
    row = np.linalg.norm(_positions([satrec(item)], fine)[0] - target_r, axis=1)
    if not np.isfinite(row).any():
        return int(fine[0]), float("inf")
    at = int(np.nanargmin(row))
    return int(fine[at]), float(row[at])


def _refine(item: Elset, targets: Sequence[Elset], start_ms: int, end_ms: int) -> tuple[int, float]:
    """The closest approach around a coarse minimum: a 10 second grid, then 0.1 seconds
    around its best point, so even a fast crossing is measured to well under a kilometre."""
    tca, _ = _closest(
        item, targets, np.arange(start_ms, end_ms + 1, REFINE_STEP_MS, dtype=np.int64)
    )
    low, high = max(start_ms, tca - REFINE_STEP_MS), min(end_ms, tca + REFINE_STEP_MS)
    return _closest(item, targets, np.arange(low, high + 1, FINE_STEP_MS, dtype=np.int64))


def separation_at(first: Elset, second: Elset, times_ms: npt.ArrayLike) -> FloatArray:
    """Distance in km between two objects at the given instants (for checks and tools)."""
    times = np.asarray(times_ms, dtype=np.int64)
    a = _positions([satrec(first)], times)[0]
    b = _positions([satrec(second)], times)[0]
    return np.asarray(np.linalg.norm(a - b, axis=1))
