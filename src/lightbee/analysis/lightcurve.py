"""The light curve, VisMag v Time by sensor, the recent panel, and ghost years.

All of these are selections and labels over one `Series`; the figures draw
them. Day 0 is always the most recent day of the window, matching the "days
ago" colouring of the Seasonal Behaviour? top panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from lightbee.analysis.groups import FloatArray, IntArray
from lightbee.analysis.seasonal import TROPICAL_YEAR_DAYS
from lightbee.analysis.series import DAY_MS, Series

RECENT_PANEL_DAYS: Final = 15
GHOST_YEARS: Final = 2


@dataclass(frozen=True)
class DayColoured:
    """Observations in a window with their age in whole days (0 is the newest day)."""

    epoch_ms: IntArray
    mag: FloatArray
    sepa: FloatArray
    day: npt.NDArray[np.int16]
    days_ago: FloatArray  # fractional, for a continuous colour scale


def by_day(series: Series, *, end_ms: int, days: int) -> DayColoured:
    """The last `days` days before `end_ms`, each observation labelled with its day."""
    if days < 1:
        raise ValueError("the window must be at least one day")
    part = series.between(end_ms - days * DAY_MS, end_ms)
    usable = part.take(np.isfinite(part.mag))
    age = (end_ms - usable.epoch_ms) / DAY_MS
    # An observation exactly on the window's start is still in its oldest day.
    day = np.minimum(np.floor(age), days - 1).astype(np.int16)
    return DayColoured(usable.epoch_ms, usable.mag, usable.sepa, day, age)


@dataclass(frozen=True)
class SensorTrack:
    """One sensor's observations in the window, for VisMag v Time."""

    sensor: str
    epoch_ms: IntArray
    mag: FloatArray


def by_sensor(series: Series, *, start_ms: int, end_ms: int) -> list[SensorTrack]:
    """Per sensor, busiest first, so the legend leads with the sensors that matter."""
    part = series.between(start_ms, end_ms)
    part = part.take(np.isfinite(part.mag))
    codes, counts = np.unique(part.sensor, return_counts=True)
    order = np.argsort(-counts, kind="stable")
    tracks = []
    for code in codes[order]:
        mine = part.sensor == code
        tracks.append(SensorTrack(part.sensors[int(code)], part.epoch_ms[mine], part.mag[mine]))
    return tracks


@dataclass(frozen=True)
class GhostYear:
    """The same calendar window in an earlier year, to draw faintly beneath the current one."""

    years_ago: int
    sepa: FloatArray
    mag: FloatArray


def ghost_years(
    series: Series, *, start_ms: int, end_ms: int, years: int = GHOST_YEARS
) -> list[GhostYear]:
    """The same window one and two years earlier, aligned on SEPA. Empty years are left out.

    The prior years are already in the history read for the seasonal flag, so
    this costs no UDL reads.
    """
    year_ms = round(TROPICAL_YEAR_DAYS * DAY_MS)
    ghosts = []
    for back in range(1, years + 1):
        part = series.between(start_ms - back * year_ms, end_ms - back * year_ms)
        part = part.take(np.isfinite(part.mag))
        if len(part):
            ghosts.append(GhostYear(back, part.sepa, part.mag))
    return ghosts
