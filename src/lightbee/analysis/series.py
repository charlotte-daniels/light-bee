"""The photometry an analysis reads: one magnitude basis, never a mix of two."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from lightbee.analysis.groups import FloatArray, IntArray
from lightbee.udl.photometry import Photometry

DAY_MS = 86_400_000


@dataclass(frozen=True)
class Series:
    """Time-ordered observations with the chosen magnitude. Times are UTC milliseconds."""

    epoch_ms: IntArray
    mag: FloatArray
    sepa: FloatArray
    geolon: FloatArray
    sensor: npt.NDArray[np.int16]
    sensors: tuple[str, ...] = ()
    basis: str = "raw"

    @classmethod
    def of(cls, photometry: Photometry, *, normalised: bool = True) -> Series:
        """The photometry with range-normalised magnitude when it is usable, else raw."""
        mag, basis = photometry.magnitudes() if normalised else (photometry.mag, "raw")
        return cls(
            photometry.epoch_ms.astype(np.int64),
            np.asarray(mag, dtype=np.float64),
            photometry.sepa.astype(np.float64),
            photometry.geolon.astype(np.float64),
            photometry.sensor,
            photometry.sensors,
            basis,
        )

    @classmethod
    def from_arrays(
        cls,
        epoch_ms: npt.ArrayLike,
        mag: npt.ArrayLike,
        sepa: npt.ArrayLike,
        geolon: npt.ArrayLike | None = None,
    ) -> Series:
        """A series from plain arrays, for tests and tools."""
        times = np.asarray(epoch_ms, dtype=np.int64)
        lon = np.full(times.size, np.nan) if geolon is None else np.asarray(geolon, np.float64)
        return cls(
            times,
            np.asarray(mag, dtype=np.float64),
            np.asarray(sepa, dtype=np.float64),
            lon,
            np.zeros(times.size, dtype=np.int16),
            ("unknown",),
        )

    def __len__(self) -> int:
        return int(self.epoch_ms.size)

    def take(self, mask: npt.NDArray[np.bool_]) -> Series:
        return Series(
            self.epoch_ms[mask],
            self.mag[mask],
            self.sepa[mask],
            self.geolon[mask],
            self.sensor[mask],
            self.sensors,
            self.basis,
        )

    def between(self, start_ms: int, end_ms: int) -> Series:
        """Observations with start <= time < end."""
        return self.take((self.epoch_ms >= start_ms) & (self.epoch_ms < end_ms))

    @property
    def nights(self) -> int:
        """Distinct UTC days observed."""
        return int(np.unique(self.epoch_ms // DAY_MS).size)


def concatenate(parts: list[Series]) -> Series:
    """Several series as one; the first part's sensor table and basis are kept."""
    if not parts:
        return Series.from_arrays([], [], [])
    return Series(
        np.concatenate([p.epoch_ms for p in parts]),
        np.concatenate([p.mag for p in parts]),
        np.concatenate([p.sepa for p in parts]),
        np.concatenate([p.geolon for p in parts]),
        np.concatenate([p.sensor for p in parts]),
        parts[0].sensors,
        parts[0].basis,
    )
