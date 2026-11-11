"""Period, inclination and RAAN over time from one provider's elsets.

The values are the provider's filed elements, so nothing is derived and
nothing can be subtly wrong. A step in any of them at the time the brightness
changed points to a manoeuvre.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from lightbee.analysis.groups import FloatArray, IntArray
from lightbee.udl.records import Elset

RAAN_MIN_INCLINATION_DEG: Final = 0.1


@dataclass(frozen=True)
class OrbitSeries:
    """One provider's elements in epoch order, duplicates of an epoch removed."""

    source: str
    epoch_ms: IntArray
    period_min: FloatArray
    inclination_deg: FloatArray
    raan_deg: FloatArray

    def __len__(self) -> int:
        return int(self.epoch_ms.size)

    def largest_step(self, name: str) -> float:
        """The largest change between consecutive elsets: a crude manoeuvre pointer.

        RAAN is not defined for an orbit with no inclination, and at the few
        hundredths of a degree a station-kept GEO satellite holds, its value
        wanders by tens of degrees with no manoeuvre at all. So below
        RAAN_MIN_INCLINATION_DEG the RAAN step is NaN; `largest_plane_change`
        measures the same thing in a way that stays defined.
        """
        values = getattr(self, name)
        if values.size < 2:
            return 0.0
        if name == "raan_deg" and float(np.min(self.inclination_deg)) < RAAN_MIN_INCLINATION_DEG:
            return float("nan")
        steps = np.abs(np.diff(values))
        if name == "raan_deg":
            steps = np.minimum(steps, 360.0 - steps)
        return float(steps.max())

    def largest_plane_change(self) -> float:
        """The largest step in the inclination vector (i cos RAAN, i sin RAAN), in degrees.

        Defined at any inclination, so it points to a north-south manoeuvre even
        where RAAN itself means nothing.
        """
        if self.epoch_ms.size < 2:
            return 0.0
        node = np.deg2rad(self.raan_deg)
        vector = np.stack(
            [self.inclination_deg * np.cos(node), self.inclination_deg * np.sin(node)]
        )
        return float(np.linalg.norm(np.diff(vector, axis=1), axis=0).max())


def orbit_series(
    elsets: Sequence[Elset], source: str, *, start_ms: int, end_ms: int
) -> OrbitSeries:
    """The chosen provider's elsets inside the window as columns, oldest first."""
    chosen = sorted(
        (e for e in elsets if e.source == source and start_ms <= _ms(e) < end_ms), key=_ms
    )
    unique: dict[int, Elset] = {}
    for item in chosen:
        unique.setdefault(_ms(item), item)
    items = list(unique.values())
    return OrbitSeries(
        source,
        np.array([_ms(e) for e in items], dtype=np.int64),
        np.array([e.period for e in items], dtype=np.float64),
        np.array([e.inclination for e in items], dtype=np.float64),
        np.array([e.raan for e in items], dtype=np.float64),
    )


def _ms(item: Elset) -> int:
    return round(item.epoch.timestamp() * 1000)
