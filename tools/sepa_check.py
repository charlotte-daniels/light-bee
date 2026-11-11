"""Which way round is the UDL's SEPA, against the predecessor's own definition?

Light Bee plots the UDL's `solarEqPhaseAngle`. The predecessor computed SEPA
itself, and the reference plots were drawn with its convention: positive is
anticlockwise from the direction to the Sun to the direction to the observer,
viewed from the north. This module recomputes SEPA from each observation's
geometry with the predecessor's formulae (ported from its `geometry.py`, low
precision Sun good to about 0.01 degrees, IAU 1982 sidereal time, IAU 1976
precession) so the live check can say whether the UDL's sign matches.

Two estimates, so the answer does not hang on one field being filled:

* from the observer's position and the measured line of sight and range
  (the predecessor's method), when senlat, senlon, senalt, ra, declination
  and range are all present;
* from the sub-satellite longitude alone, with the observer at the Earth's
  centre, which is always available and is within a few degrees, ample to
  read a sign away from zero.

Tool only: never shipped, never imported by the app.
"""

from __future__ import annotations

from typing import Any, Final

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]

WGS84_A: Final = 6378.137
WGS84_F: Final = 1.0 / 298.257223563
WGS84_E2: Final = WGS84_F * (2.0 - WGS84_F)
AU_KM: Final = 149_597_870.7
J2000_JD: Final = 2_451_545.0
UNIX_JD: Final = 2_440_587.5
GEO_RADIUS_KM: Final = 42_164.0
# SEPA near zero carries no sign information worth reading.
SIGN_FLOOR_DEG: Final = 20.0


def julian_date(epoch_ms: npt.ArrayLike) -> Array:
    return UNIX_JD + np.asarray(epoch_ms, dtype=np.float64) / 86_400_000.0


def gmst_radians(jd: Array) -> Array:
    """Greenwich mean sidereal time, IAU 1982, wrapped to [0, 2 pi)."""
    t = (jd - J2000_JD) / 36525.0
    seconds = (
        67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * t + 0.093104 * t**2 - 6.2e-6 * t**3
    )
    return np.asarray(np.deg2rad(np.mod(seconds, 86_400.0) / 240.0) % (2.0 * np.pi))


def sun_position_km(jd: Array) -> Array:
    """Geocentric Sun, mean equinox of date, low precision series."""
    n = jd - J2000_JD
    mean_longitude = np.deg2rad(np.mod(280.460 + 0.9856474 * n, 360.0))
    mean_anomaly = np.deg2rad(np.mod(357.528 + 0.9856003 * n, 360.0))
    longitude = (
        mean_longitude
        + np.deg2rad(1.915) * np.sin(mean_anomaly)
        + np.deg2rad(0.020) * np.sin(2.0 * mean_anomaly)
    )
    obliquity = np.deg2rad(23.439 - 4.0e-7 * n)
    distance = (
        1.00014 - 0.01671 * np.cos(mean_anomaly) - 0.00014 * np.cos(2.0 * mean_anomaly)
    ) * AU_KM
    return np.stack(
        [
            distance * np.cos(longitude),
            distance * np.cos(obliquity) * np.sin(longitude),
            distance * np.sin(obliquity) * np.sin(longitude),
        ],
        axis=-1,
    )


def geodetic_to_ecef(lat_deg: Array, lon_deg: Array, alt_km: Array) -> Array:
    lat, lon = np.deg2rad(lat_deg), np.deg2rad(lon_deg)
    normal = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(lat) ** 2)
    return np.stack(
        [
            (normal + alt_km) * np.cos(lat) * np.cos(lon),
            (normal + alt_km) * np.cos(lat) * np.sin(lon),
            (normal * (1.0 - WGS84_E2) + alt_km) * np.sin(lat),
        ],
        axis=-1,
    )


def ecef_to_inertial(r: Array, theta: Array) -> Array:
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    return np.stack(
        [cos_t * r[..., 0] - sin_t * r[..., 1], sin_t * r[..., 0] + cos_t * r[..., 1], r[..., 2]],
        axis=-1,
    )


def precession_matrix(jd: Array) -> Array:
    """IAU 1976 precession, J2000 to date, shape (..., 3, 3)."""
    t = (jd - J2000_JD) / 36525.0
    arcsecond = np.deg2rad(1.0 / 3600.0)
    zeta = (2306.2181 * t + 0.30188 * t**2 + 0.017998 * t**3) * arcsecond
    z = (2306.2181 * t + 1.09468 * t**2 + 0.018203 * t**3) * arcsecond
    theta = (2004.3109 * t - 0.42665 * t**2 - 0.041833 * t**3) * arcsecond
    cz, sz, ce, se, ct, st = (
        np.cos(zeta),
        np.sin(zeta),
        np.cos(z),
        np.sin(z),
        np.cos(theta),
        np.sin(theta),
    )
    return np.stack(
        [
            np.stack([cz * ct * ce - sz * se, -sz * ct * ce - cz * se, -st * ce], axis=-1),
            np.stack([cz * ct * se + sz * ce, -sz * ct * se + cz * ce, -st * se], axis=-1),
            np.stack([cz * st, -sz * st, ct], axis=-1),
        ],
        axis=-2,
    )


def sepa_degrees(r_sat: Array, r_sun: Array, r_obs: Array) -> Array:
    """The predecessor's SEPA (its sign=1): anticlockwise from Sun to observer, from the north."""
    to_sun = r_sun - r_sat
    to_obs = r_obs - r_sat
    cross = to_sun[..., 0] * to_obs[..., 1] - to_sun[..., 1] * to_obs[..., 0]
    dot = to_sun[..., 0] * to_obs[..., 0] + to_sun[..., 1] * to_obs[..., 1]
    return np.asarray(np.rad2deg(np.arctan2(cross, dot)))


def from_line_of_sight(rows: dict[str, Array]) -> Array:
    """SEPA from the sensor's position and the measured direction and range."""
    jd = julian_date(rows["epoch_ms"])
    theta = gmst_radians(jd)
    observer = ecef_to_inertial(
        geodetic_to_ecef(rows["senlat"], rows["senlon"], rows["senalt"]), theta
    )
    ra, dec = np.deg2rad(rows["ra"]), np.deg2rad(rows["declination"])
    j2000 = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)
    direction = np.einsum("...ij,...j->...i", precession_matrix(jd), j2000)
    satellite = observer + rows["range"][..., None] * direction
    return sepa_degrees(satellite, sun_position_km(jd), observer)


def from_longitude(epoch_ms: Array, geolon: Array) -> Array:
    """SEPA from the sub-satellite longitude, observer at the Earth's centre."""
    jd = julian_date(epoch_ms)
    lon = np.deg2rad(geolon)
    fixed = np.stack(
        [GEO_RADIUS_KM * np.cos(lon), GEO_RADIUS_KM * np.sin(lon), np.zeros_like(lon)], axis=-1
    )
    satellite = ecef_to_inertial(fixed, gmst_radians(jd))
    return sepa_degrees(satellite, sun_position_km(jd), np.zeros_like(satellite))


def compare(udl: Array, ours: Array) -> dict[str, Any]:
    """How the UDL's SEPA relates to ours: same sign, mirrored, or neither."""
    usable = np.isfinite(udl) & np.isfinite(ours) & (np.abs(udl) >= SIGN_FLOOR_DEG)
    if not usable.any():
        return {"compared": 0, "verdict": "no usable pairs"}
    a, b = udl[usable], ours[usable]
    same = float(np.mean(np.sign(a) == np.sign(b)))
    as_is = float(np.median(np.abs(a - b)))
    mirrored = float(np.median(np.abs(a + b)))
    if same >= 0.95 and as_is < 10.0:
        verdict = "same convention"
    elif same <= 0.05 and mirrored < 10.0:
        verdict = "mirrored"
    else:
        verdict = "unclear"
    return {
        "compared": int(usable.sum()),
        "same_sign_share": round(same, 3),
        "median_abs_difference_deg": round(as_is, 2),
        "median_abs_sum_deg": round(mirrored, 2),
        "verdict": verdict,
    }
