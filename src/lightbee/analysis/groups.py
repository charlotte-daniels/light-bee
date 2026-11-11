"""Grouped medians and circular means, vectorised. The building blocks of every raster."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]


def group_medians(
    keys: npt.NDArray[Any], values: npt.NDArray[Any]
) -> tuple[IntArray, FloatArray, IntArray]:
    """The median and count of `values` for each distinct integer key, keys ascending.

    NaN values are ignored. One sort does the work: values ordered within each
    key, then the middle one (or the mean of the middle two) taken by index.
    """
    finite = np.isfinite(values)
    k = np.asarray(keys, dtype=np.int64)[finite]
    v = np.asarray(values, dtype=np.float64)[finite]
    if k.size == 0:
        return np.empty(0, np.int64), np.empty(0, np.float64), np.empty(0, np.int64)
    order = np.lexsort((v, k))
    k, v = k[order], v[order]
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    counts = np.diff(np.r_[starts, k.size])
    low = v[starts + (counts - 1) // 2]
    high = v[starts + counts // 2]
    return k[starts], (low + high) / 2.0, counts


def group_mad_sigma(
    keys: npt.NDArray[Any], values: npt.NDArray[Any]
) -> tuple[IntArray, FloatArray, FloatArray, IntArray]:
    """Per key: median, robust sigma (1.4826 times the median absolute deviation) and count."""
    unique, medians, counts = group_medians(keys, values)
    if unique.size == 0:
        return unique, medians, np.empty(0, np.float64), counts
    finite = np.isfinite(values)
    k = np.asarray(keys, dtype=np.int64)[finite]
    position = np.searchsorted(unique, k)
    deviation = np.abs(np.asarray(values, dtype=np.float64)[finite] - medians[position])
    _, mad, _ = group_medians(k, deviation)
    return unique, medians, 1.4826 * mad, counts


def circular_mean_deg(values: npt.NDArray[Any]) -> float:
    """Mean of angles in degrees on the circle, so 179 and -179 average to 180. NaN if none."""
    usable = np.asarray(values, dtype=np.float64)
    usable = usable[np.isfinite(usable)]
    if usable.size == 0:
        return float("nan")
    radians = np.deg2rad(usable)
    return float(np.rad2deg(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())))


def wrap_deg(angle: npt.NDArray[Any] | float) -> npt.NDArray[np.float64]:
    """An angle in degrees wrapped to [-180, 180)."""
    return np.asarray((np.asarray(angle, dtype=np.float64) + 180.0) % 360.0 - 180.0)
