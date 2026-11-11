"""The "Seasonal Behaviour?" mode: the date by SEPA raster and the seasonal flag.

Ported from the predecessor's tested `grid.py` (pandas) to NumPy, keeping its
thresholds and its wording, so the verdicts agree on the reference objects.

The comparison is like for like. It sets the recent window against the same
calendar season in earlier years, within a SEPA bin rather than across the
whole night, because observing geometry moves the magnitude by whole
magnitudes and would swamp anything real. Earlier years are also restricted to
the longitude the object occupies now: a body-stabilised GEO satellite seen
from a different slot presents a different face at the identical SEPA, so a
relocation suppresses the verdict and says why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from lightbee.analysis.groups import (
    FloatArray,
    IntArray,
    circular_mean_deg,
    group_mad_sigma,
    group_medians,
    wrap_deg,
)
from lightbee.analysis.series import DAY_MS, Series, concatenate

TROPICAL_YEAR_DAYS: Final = 365.2422
GRID_SEPA_BIN_DEG: Final = 2.0
FLAG_SEPA_BIN_DEG: Final = 5.0
# A comparison bin with fewer points than this is noise, not a median.
MIN_POINTS_PER_BIN: Final = 3
MIN_COMPARABLE_BINS: Final = 8
# Two degrees is far outside any station-keeping box (about 0.1 degrees) and
# small enough that a genuine relocation always trips it.
LONGITUDE_TOLERANCE_DEG: Final = 2.0
CONSISTENT_DELTA_MAG: Final = 0.20
CONSISTENT_ROBUST_Z: Final = 2.0
COMFORTABLE_FRACTION: Final = 0.75
# Stops a bin that was very tight in earlier years turning a trivial
# difference into an enormous z.
SCATTER_FLOOR_MAG: Final = 0.05
SEASON_PAD_DAYS: Final = 10.0
REFERENCE_YEARS: Final = 2

# Longitude movement inside the recent window is measured on a running median
# over this fraction of the observations, held between these limits.
DRIFT_WINDOW_FRACTION: Final = 50
DRIFT_WINDOW_LIMITS: Final = (5, 50)

Outcome = Literal[
    "consistent", "marginal", "offset", "departs", "inconclusive", "relocated", "drifting"
]


@dataclass(frozen=True)
class SepaGrid:
    """A date by SEPA raster of median magnitude. Row 0 is the most recent day."""

    values: npt.NDArray[np.float32]  # (rows, cols), NaN where nothing was observed
    counts: npt.NDArray[np.int32]  # (rows, cols)
    row_start_ms: IntArray  # (rows,), the earlier edge of each row
    sepa_edges: FloatArray  # (cols + 1,)
    geo_longitude: FloatArray  # (rows,), circular mean, NaN where unknown

    @property
    def sepa_centres(self) -> FloatArray:
        return 0.5 * (self.sepa_edges[:-1] + self.sepa_edges[1:])


def sepa_edges(bin_deg: float) -> FloatArray:
    return np.arange(-180.0, 180.0 + bin_deg / 2, bin_deg)


def sepa_columns(sepa: FloatArray, edges: FloatArray) -> IntArray:
    """The SEPA bin of each value; exactly +180 joins the last bin rather than falling off."""
    columns = np.digitize(sepa, edges) - 1
    columns[sepa == edges[-1]] = edges.size - 2
    return np.asarray(columns, dtype=np.int64)


def build_grid(
    series: Series, *, end_ms: int, days: int, sepa_bin_deg: float = GRID_SEPA_BIN_DEG
) -> SepaGrid:
    """Raster observations into daily rows (newest first) and SEPA columns.

    A median, because a single specular glint is a real measurement and a
    misleading summary of a night. Every observed cell is drawn, as the
    predecessor did, so the raster matches the reference plots.
    """
    if days < 1 or sepa_bin_deg <= 0:
        raise ValueError("the grid needs at least one day and a positive SEPA bin")
    edges = sepa_edges(sepa_bin_deg)
    cols = edges.size - 1
    row = (end_ms - series.epoch_ms) // DAY_MS
    col = sepa_columns(series.sepa, edges)
    inside = (row >= 0) & (row < days) & (col >= 0) & (col < cols)

    values = np.full((days, cols), np.nan, dtype=np.float32)
    counts = np.zeros((days, cols), dtype=np.int32)
    cell, median, count = group_medians(row[inside] * cols + col[inside], series.mag[inside])
    values.flat[cell] = median
    counts.flat[cell] = count
    row_start = end_ms - (np.arange(days, dtype=np.int64) + 1) * DAY_MS
    return SepaGrid(values, counts, row_start, edges, _daily_longitude(row, inside, series, days))


def _daily_longitude(
    row: IntArray, inside: npt.NDArray[np.bool_], series: Series, days: int
) -> FloatArray:
    """Per row, the circular mean longitude, so an object on the date line averages to 180."""
    longitude = np.full(days, np.nan)
    lon = series.geolon[inside]
    known = np.isfinite(lon)
    if known.any():
        rows = row[inside][known]
        radians = np.deg2rad(lon[known])
        sin = np.bincount(rows, np.sin(radians), minlength=days)
        cos = np.bincount(rows, np.cos(radians), minlength=days)
        seen = np.bincount(rows, minlength=days) > 0
        longitude[seen] = np.rad2deg(np.arctan2(sin[seen], cos[seen]))
    return longitude


@dataclass(frozen=True)
class BinTable:
    """The working behind the flag: one row per comparable SEPA bin."""

    sepa_centre: FloatArray = field(default_factory=lambda: np.empty(0))
    recent: FloatArray = field(default_factory=lambda: np.empty(0))
    reference: FloatArray = field(default_factory=lambda: np.empty(0))
    robust_z: FloatArray = field(default_factory=lambda: np.empty(0))

    def __len__(self) -> int:
        return int(self.sepa_centre.size)


@dataclass(frozen=True)
class Context:
    """What the comparison could use: years, slots and coverage."""

    years_used: tuple[int, ...]
    recent_longitude_deg: float
    reference_longitude_deg: float
    tolerance_deg: float
    recent_nights: int
    reference_nights: int
    matched_points: int
    total_points: int
    recent_spread_deg: float = 0.0  # longitude travelled inside the recent window
    # Where the object was in each earlier season read, newest first.
    season_longitudes: tuple[tuple[int, float], ...] = ()

    @property
    def where_then(self) -> str:
        """'at 128.7°E 1 year ago and 175.4°E 2 years ago', from each season's own mean.

        A mean across years at different slots names a place the object never
        was, so each year is stated on its own.
        """
        parts = [
            f"{lon:.1f}°E {year} year{'s' if year > 1 else ''} ago"
            for year, lon in self.season_longitudes
            if np.isfinite(lon)
        ]
        if not parts:
            return f"at {self.reference_longitude_deg:.1f}°E in the reference seasons"
        return "at " + " and ".join(parts)

    @property
    def longitude_shift_deg(self) -> float:
        """How far the object has moved since the reference seasons; NaN if unknown."""
        return float(wrap_deg(self.recent_longitude_deg - self.reference_longitude_deg))

    @property
    def relocated(self) -> bool:
        shift = self.longitude_shift_deg
        return bool(np.isfinite(shift) and abs(shift) > self.tolerance_deg)


@dataclass(frozen=True)
class SeasonalFlag:
    """The recent window set against the same season in earlier years, bin by bin."""

    outcome: Outcome
    headline: str
    detail: str
    marginal: bool
    median_delta_mag: float
    median_robust_z: float
    context: Context
    bins: BinTable

    @property
    def comparable_bins(self) -> int:
        return len(self.bins)


def season_windows(
    start_ms: int, end_ms: int, years: int = REFERENCE_YEARS
) -> list[tuple[int, int, int]]:
    """The same calendar season in each of the preceding years, padded either side."""
    year_ms = round(TROPICAL_YEAR_DAYS * DAY_MS)
    pad_ms = round(SEASON_PAD_DAYS * DAY_MS)
    return [
        (y, start_ms - y * year_ms - pad_ms, end_ms - y * year_ms + pad_ms)
        for y in range(1, years + 1)
    ]


def seasonal_flag(
    series: Series,
    *,
    recent_start_ms: int,
    recent_end_ms: int,
    years: int = REFERENCE_YEARS,
    tolerance_deg: float = LONGITUDE_TOLERANCE_DEG,
) -> SeasonalFlag:
    """Is the recent photometry what this object did at this point in its year before?

    The z score uses the earlier years' own scatter, so an object that is
    genuinely erratic in this season needs a larger departure before it is
    called a change. Earlier observations are kept one by one only if they
    were taken within the tolerance of the current longitude.
    """
    recent = series.between(recent_start_ms, recent_end_ms)
    recent_lon = circular_mean_deg(recent.geolon)
    spread, first_lon, last_lon = _drift(recent, recent_lon)
    seasons: list[Series] = []
    matched: list[Series] = []
    years_used: list[int] = []
    season_longitudes: list[tuple[int, float]] = []
    for year, low, high in season_windows(recent_start_ms, recent_end_ms, years):
        part = series.between(low, high)
        if not len(part):
            continue
        seasons.append(part)
        season_longitudes.append((year, circular_mean_deg(part.geolon)))
        if np.isfinite(recent_lon):
            part = part.take(np.abs(wrap_deg(part.geolon - recent_lon)) <= tolerance_deg)
        if len(part):
            matched.append(part)
            years_used.append(year)

    reference = concatenate(matched)
    context = Context(
        years_used=tuple(years_used),
        recent_longitude_deg=recent_lon,
        reference_longitude_deg=circular_mean_deg(concatenate(seasons).geolon),
        tolerance_deg=tolerance_deg,
        recent_nights=recent.nights,
        reference_nights=sum(part.nights for part in matched),
        matched_points=len(reference),
        total_points=sum(len(s) for s in seasons),
        recent_spread_deg=spread,
        season_longitudes=tuple(season_longitudes),
    )
    if spread > tolerance_deg:
        return _drifting(context, first_lon, last_lon)
    if not len(recent) or not matched:
        return _without_reference(context, recent_empty=not len(recent))
    bins = compare_bins(recent, reference, sepa_edges(FLAG_SEPA_BIN_DEG))
    delta = float(np.median(bins.recent - bins.reference)) if len(bins) else float("nan")
    z = float(np.median(bins.robust_z)) if len(bins) else float("nan")
    outcome, headline, detail, marginal = _Words(len(bins), delta, z, context).verdict()
    return SeasonalFlag(outcome, headline, detail, marginal, delta, z, context, bins)


def _drift(recent: Series, mean_lon: float) -> tuple[float, float, float]:
    """How far the object moved during the recent window, and where it started and ended.

    Measured on a running median of each observation's offset from the mean
    longitude, in time order. The median ignores a stray value, and the
    running window is short enough (a fiftieth of the observations, 5 to 50)
    that a move in the last few hours of the window still shows in full. A
    move seen in fewer than half a window of the newest observations (about
    25, or half an hour for a dense object) is indistinguishable from strays
    and is not counted. With fewer than 5 observations there is no spread.
    """
    known = recent.take(np.isfinite(recent.geolon))
    if not len(known) or not np.isfinite(mean_lon):
        return 0.0, float("nan"), float("nan")
    order = np.argsort(known.epoch_ms, kind="stable")
    offsets = wrap_deg(known.geolon[order] - mean_lon)
    width = int(np.clip(offsets.size // DRIFT_WINDOW_FRACTION, *DRIFT_WINDOW_LIMITS))
    if offsets.size < width:
        running = np.array([np.median(offsets)])
    else:
        running = np.median(np.lib.stride_tricks.sliding_window_view(offsets, width), axis=1)
    first = float(wrap_deg(mean_lon + running[0]))
    last = float(wrap_deg(mean_lon + running[-1]))
    return float(running.max() - running.min()), first, last


def _drifting(context: Context, first_lon: float, last_lon: float) -> SeasonalFlag:
    """A drifting object has no fixed slot to compare against, so no verdict is given."""
    nan = float("nan")
    detail = (
        f"The object moved {context.recent_spread_deg:.1f}° in longitude during the recent "
        f"window, from about {first_lon:.1f}°E to {last_lon:.1f}°E, more than the "
        f"{context.tolerance_deg:g}° a like-for-like comparison allows. A body-stabilised GEO "
        "satellite changes how it presents to every ground sensor as it drifts, so a "
        "brightness change now cannot be set against its seasonal history. Treat the drift "
        "as a leading explanation, and look for the manoeuvre that started it in Other Causes."
    )
    headline = "Cannot be judged while the object is drifting"
    return SeasonalFlag("drifting", headline, detail, True, nan, nan, context, BinTable())


def compare_bins(recent: Series, reference: Series, edges: FloatArray) -> BinTable:
    """Per SEPA bin with enough points in both windows: medians and the robust z."""
    cols = edges.size - 1

    def binned(part: Series) -> tuple[IntArray, FloatArray, FloatArray, IntArray]:
        col = sepa_columns(part.sepa, edges)
        inside = (col >= 0) & (col < cols)
        return group_mad_sigma(col[inside], part.mag[inside])

    r_bin, r_median, _, r_n = binned(recent)
    p_bin, p_median, p_scatter, p_n = binned(reference)
    common, r_at, p_at = np.intersect1d(r_bin, p_bin, assume_unique=True, return_indices=True)
    enough = (r_n[r_at] >= MIN_POINTS_PER_BIN) & (p_n[p_at] >= MIN_POINTS_PER_BIN)
    common, r_at, p_at = common[enough], r_at[enough], p_at[enough]
    delta = r_median[r_at] - p_median[p_at]
    z = delta / np.maximum(p_scatter[p_at], SCATTER_FLOOR_MAG)
    centres = 0.5 * (edges[:-1] + edges[1:])
    return BinTable(centres[common], r_median[r_at], p_median[p_at], z)


def _without_reference(context: Context, *, recent_empty: bool) -> SeasonalFlag:
    """Say which half of the comparison is missing, because the fixes differ."""
    nan = float("nan")
    if recent_empty:
        detail = (
            "There is no usable photometry in the recent window, so there is nothing to "
            "compare. Widen the window or move the change date."
        )
        return SeasonalFlag(
            "inconclusive", "Inconclusive", detail, False, nan, nan, context, BinTable()
        )
    if context.total_points and context.relocated:
        detail = (
            f"The same season of earlier years was observed with the object "
            f"{context.where_then}, and it is at "
            f"{context.recent_longitude_deg:.1f}°E now. None of the earlier data was taken within "
            f"{context.tolerance_deg:g}° of this slot. A body-stabilised GEO satellite seen from a "
            "new slot presents a different face to every ground sensor, so the same SEPA no "
            "longer means the same view. Treat the move itself as the leading explanation, and "
            "date it in Other Causes."
        )
        headline = "Cannot be separated from the longitude change"
        return SeasonalFlag("relocated", headline, detail, True, nan, nan, context, BinTable())
    detail = (
        "There is recent photometry but nothing from the same season of any earlier year "
        "at this slot. The comparison needs the object's own past at the same point in its "
        "year, so it needs at least a year of history."
    )
    return SeasonalFlag(
        "inconclusive", "Inconclusive", detail, False, nan, nan, context, BinTable()
    )


@dataclass(frozen=True)
class _Words:
    """Turn the numbers into a sentence without claiming more than they support."""

    comparable: int
    delta: float
    z: float
    context: Context

    def verdict(self) -> tuple[Outcome, str, str, bool]:
        if self.comparable < MIN_COMPARABLE_BINS:
            return self._too_few()
        size = abs(self.delta)
        within = size < CONSISTENT_DELTA_MAG and abs(self.z) < CONSISTENT_ROBUST_Z
        comfortable = (
            size < CONSISTENT_DELTA_MAG * COMFORTABLE_FRACTION
            and abs(self.z) < CONSISTENT_ROBUST_Z * COMFORTABLE_FRACTION
        )
        if within and comfortable:
            return "consistent", "Consistent with seasonal variation", self._consistent(), False
        if within:
            return "marginal", "Marginal, not clearly seasonal", self._marginal(), True
        if abs(self.z) < CONSISTENT_ROBUST_Z:
            headline = "Offset, but within the object's seasonal scatter"
            return "offset", headline, self._offset(), True
        return "departs", "Departs from the seasonal pattern", self._departs(), False

    @property
    def _against(self) -> str:
        """'0.12 mag brighter than the same season 1 year ago and 2 years ago'."""
        direction = "brighter" if self.delta < 0 else "fainter"
        seasons = " and ".join(
            f"{y} year{'s' if y > 1 else ''} ago" for y in self.context.years_used
        )
        return f"{abs(self.delta):.2f} mag {direction} than the same season {seasons}"

    def _consistent(self) -> str:
        return (
            f"Across {self.comparable} SEPA bins the recent photometry sits {self._against}, "
            f"a median departure of {self.z:+.1f} sigma against those years' own scatter. "
            "That is comfortably inside what this object does at this point in its year "
            f"(called at {CONSISTENT_DELTA_MAG:.2f} mag and {CONSISTENT_ROBUST_Z:.1f} sigma)."
            + self._longitude_note()
        )

    def _marginal(self) -> str:
        return (
            f"The recent photometry is {self._against}, at {self.z:+.1f} sigma, across "
            f"{self.comparable} SEPA bins. That is inside the seasonal band of "
            f"{CONSISTENT_DELTA_MAG:.2f} mag and {CONSISTENT_ROBUST_Z:.1f} sigma, but only just. "
            "Treat it as unresolved: if you came to this object because you noticed a change, "
            "this result does not stand it down." + self._longitude_note()
        )

    def _offset(self) -> str:
        return (
            f"The recent photometry is {self._against}, but only {self.z:+.1f} sigma against "
            "those years' own scatter, which is wide. Treat it as suggestive rather than "
            "established, and re-run once the window is longer." + self._longitude_note()
        )

    def _departs(self) -> str:
        return (
            f"Across {self.comparable} SEPA bins the recent photometry is {self._against}, a "
            f"median departure of {self.z:+.1f} sigma against those years' own scatter. This is "
            "not the seasonal signature repeating. Check for a manoeuvre, an attitude change or "
            "a sensor or calibration change in Other Causes." + self._longitude_note()
        )

    def _too_few(self) -> tuple[Outcome, str, str, bool]:
        c = self.context
        if c.relocated:
            detail = (
                f"The object has moved in longitude: it was {c.where_then}, and is at "
                f"{c.recent_longitude_deg:.1f}°E now. Keeping only earlier observations within "
                f"{c.tolerance_deg:g}° of the current slot leaves {c.matched_points:,} of "
                f"{c.total_points:,}, and {self.comparable} comparable SEPA bins against the "
                f"{MIN_COMPARABLE_BINS} needed. The relocation is itself the most likely "
                "explanation for a brightness change."
            )
            return "relocated", "Cannot be separated from the longitude change", detail, True
        detail = (
            f"Only {self.comparable} SEPA bins have at least {MIN_POINTS_PER_BIN} observations "
            "in both the recent window and the same season of earlier years, and "
            f"{MIN_COMPARABLE_BINS} are needed. Widen the window."
        )
        return "inconclusive", "Inconclusive", detail, False

    def _longitude_note(self) -> str:
        """Always stated, so the analyst knows the comparison was held to the current slot."""
        c = self.context
        if not np.isfinite(c.recent_longitude_deg):
            return (
                " Longitude is unknown for these observations, so the comparison could not be "
                "held to the object's current slot; if it has moved, do not rely on this verdict."
            )
        if not c.relocated:
            return (
                f" The object was at {c.recent_longitude_deg:.1f}°E throughout, so the comparison "
                "is like for like in longitude as well as in solar geometry."
            )
        return (
            f" The object has moved: it was {c.where_then}, and is at "
            f"{c.recent_longitude_deg:.1f}°E now, so only the {c.matched_points:,} earlier "
            f"observations within {c.tolerance_deg:g}° of the current slot were used and "
            f"{c.total_points - c.matched_points:,} were set aside."
        )
