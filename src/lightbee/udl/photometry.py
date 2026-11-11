"""Photometry held as compact columns, built slice by slice as the UDL answers.

Two years of a densely observed GEO object is about 1.2 million observations.
As columns (int64 epoch milliseconds, four float32 values and an int16 sensor
code) that is about 30 MB; as Python objects it was measured at about 1 GB.
Each slice is converted in the worker that read it, so raw records are
discarded as soon as they land.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from lightbee.udl.records import ObservationRow, Tally, controls_of, observation

FloatArray = npt.NDArray[np.float32]
# Range-normalised magnitude is used only when nearly every observation has it;
# otherwise the whole run uses raw magnitude, so the two are never mixed.
NORMALISED_SHARE: Final = 0.9


@dataclass(frozen=True)
class Photometry:
    """Validated photometry for one object, oldest first."""

    epoch_ms: npt.NDArray[np.int64]
    mag: FloatArray
    mag_norm: FloatArray
    sepa: FloatArray
    geolon: FloatArray
    sensor: npt.NDArray[np.int16]
    sensors: tuple[str, ...]
    tally: Tally = field(default_factory=Tally)

    def __len__(self) -> int:
        return int(self.epoch_ms.size)

    @property
    def nbytes(self) -> int:
        arrays = (self.epoch_ms, self.mag, self.mag_norm, self.sepa, self.geolon, self.sensor)
        return sum(array.nbytes for array in arrays)

    def magnitudes(self) -> tuple[FloatArray, str]:
        """The magnitudes to plot and their basis: "range-normalised" or "raw"."""
        if self.epoch_ms.size and np.isfinite(self.mag_norm).mean() >= NORMALISED_SHARE:
            return self.mag_norm, "range-normalised"
        return self.mag, "raw"


@dataclass(frozen=True)
class PhotometryChunk:
    """One slice's validated rows as columns, with a local sensor table."""

    columns: dict[str, npt.NDArray[Any]]
    sensors: tuple[str, ...]
    tally: Tally


def chunk_from_records(records: Sequence[dict[str, Any]], sat_no: int) -> PhotometryChunk:
    """Validate one slice and convert it to columns, counting every drop."""
    tally = Tally(received=len(records))
    rows: list[ObservationRow] = []
    for record in records:
        controls = controls_of(record)
        if controls is None:
            tally.refuse(record.get("classificationMarking"))
            continue
        row = observation(record, sat_no)
        if row is None:
            tally.invalid += 1
            continue
        tally.controls |= controls
        rows.append(row)
    names = sorted({row.sensor for row in rows})
    code = {name: index for index, name in enumerate(names)}
    columns: dict[str, npt.NDArray[Any]] = {
        "epoch_ms": np.fromiter((r.epoch_ms for r in rows), np.int64, len(rows)),
        "mag": np.fromiter((r.mag for r in rows), np.float32, len(rows)),
        "mag_norm": np.fromiter((r.mag_norm for r in rows), np.float32, len(rows)),
        "sepa": np.fromiter((r.sepa for r in rows), np.float32, len(rows)),
        "geolon": np.fromiter((r.geolon for r in rows), np.float32, len(rows)),
        "sensor": np.fromiter((code[r.sensor] for r in rows), np.int16, len(rows)),
    }
    return PhotometryChunk(columns, tuple(names), tally)


def _same(a: FloatArray) -> npt.NDArray[np.bool_]:
    """Element i equals element i-1, treating NaN as equal to NaN."""
    equal: npt.NDArray[np.bool_] = (a[1:] == a[:-1]) | (np.isnan(a[1:]) & np.isnan(a[:-1]))
    return equal


def combine(chunks: Iterable[PhotometryChunk]) -> Photometry:
    """Merge chunks into one time-ordered set, dropping exact duplicates and counting them."""
    tally = Tally()
    names: dict[str, int] = {}
    parts: dict[str, list[npt.NDArray[Any]]] = {key: [] for key in _KEYS}
    for chunk in chunks:
        tally.add(chunk.tally)
        remap = np.array([names.setdefault(n, len(names)) for n in chunk.sensors], np.int16)
        for key in _KEYS:
            column = chunk.columns[key]
            parts[key].append(remap[column] if key == "sensor" and column.size else column)
    merged = {key: _concat(parts[key], _DTYPES[key]) for key in _KEYS}
    # Every compared column is a sort key, so exact duplicates are always adjacent.
    keys = ("geolon", "sepa", "mag", "mag_norm", "sensor", "epoch_ms")
    order = np.lexsort(tuple(merged[key] for key in keys))
    merged = {key: value[order] for key, value in merged.items()}
    keep = np.ones(order.size, dtype=bool)
    if order.size > 1:
        duplicate = (merged["epoch_ms"][1:] == merged["epoch_ms"][:-1]) & (
            merged["sensor"][1:] == merged["sensor"][:-1]
        )
        for key in ("mag", "mag_norm", "sepa", "geolon"):
            duplicate &= _same(merged[key])
        keep[1:] = ~duplicate
    tally.duplicates = int(order.size - keep.sum())
    tally.kept = int(keep.sum())
    return Photometry(
        epoch_ms=merged["epoch_ms"][keep],
        mag=merged["mag"][keep],
        mag_norm=merged["mag_norm"][keep],
        sepa=merged["sepa"][keep],
        geolon=merged["geolon"][keep],
        sensor=merged["sensor"][keep],
        sensors=tuple(names),
        tally=tally,
    )


_KEYS = ("epoch_ms", "mag", "mag_norm", "sepa", "geolon", "sensor")
_DTYPES: dict[str, type[np.generic]] = {
    "epoch_ms": np.int64,
    "mag": np.float32,
    "mag_norm": np.float32,
    "sepa": np.float32,
    "geolon": np.float32,
    "sensor": np.int16,
}


def _concat(parts: list[npt.NDArray[Any]], dtype: type[np.generic]) -> npt.NDArray[Any]:
    return np.concatenate(parts).astype(dtype, copy=False) if parts else np.empty(0, dtype)


def as_chunk(photometry: Photometry) -> PhotometryChunk:
    """A finished read as a chunk again, so two reads can be merged with `combine`."""
    columns = {key: getattr(photometry, key) for key in _KEYS}
    return PhotometryChunk(columns, photometry.sensors, photometry.tally)


def merge(first: Photometry, second: Photometry) -> Photometry:
    """Two reads of one object as one set: sensors remapped, exact duplicates dropped.

    The tallies add, and the duplicates count is the merged set's own, so an
    observation both reads returned is counted once as kept and once as a
    duplicate, never twice as kept.
    """
    merged = combine([as_chunk(first), as_chunk(second)])
    merged.tally.duplicates += first.tally.duplicates + second.tally.duplicates
    return merged
