"""Validating UDL records at the boundary. Fail closed: anything doubtful is dropped and counted.

Every record must be marked UNCLASSIFIED. Light Bee is strictly unclassified,
so any other marking, or none, is refused outright, never displayed, and the
refusal is counted so the analyst can see it happened. An UNCLASSIFIED marking
may carry only the dissemination controls the owner has accepted; the controls
on everything kept are collected so every product is marked with them, and the
markings refused are counted by label so a refusal can be explained.

Elsets arrive as mean elements, not TLE lines: the UDL's tuple endpoint does
not allow line1 or line2 to be selected. Only SGP4 elsets (ephemeris type 0)
are kept, because those are what the propagator expects.

A range-normalised magnitude is taken from magNormRange, else computed from
georange with the UDL's own formula, else left empty so the analysis can fall
back to raw magnitude for the whole run (never a mix of the two).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple

UNCLASSIFIED_MARKINGS: Final = frozenset({"U", "UNCLASSIFIED"})
# Accepted by the owner on 10 November 2026, from what the live check found on
# the reference objects' photometry and KBR elsets. Any other control, or an
# unusual marking form, is refused.
ACCEPTED_CONTROLS: Final = frozenset({"PR-MRKT", "PR-EXO-OBS", "DS-NSFCTC-OBS", "PR-KBR-ELSETS"})
_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_SENSOR: Final = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_SOURCE: Final = re.compile(r"[A-Za-z0-9 _.\-]{1,64}")
# Five-digit catalogue numbers only: TLE lines cannot carry larger ones without
# the Alpha-5 encoding, which Light Bee does not accept.
MAX_SAT_NO: Final = 99_999

MAG_RANGE: Final = (-5.0, 25.0)
# The UDL normalises to 1,000,000 km, so a GEO object reads about 7 magnitudes
# fainter normalised than raw; the bound allows for that.
MAG_NORM_RANGE: Final = (-5.0, 40.0)
GEORANGE_KM: Final = (1_000.0, 1_000_000.0)
SEPA_RANGE: Final = (-180.0, 180.0)
# The UDL's solarEqPhaseAngle is mirrored against the predecessor's SEPA, which
# drew the reference plots: measured on 1,797 observations of 43874 from every
# provider (live check, 10 November 2026), the signs never agreed and the flipped
# values matched to a median of 1.9 degrees. So it is flipped here, once, at the
# boundary, and everything downstream uses the predecessor's orientation
# (negative before opposition, as the reference plots read).
SEPA_SIGN: Final = -1.0
GEOLON_RANGE: Final = (-180.0, 360.0)
SGP4_EPHEMERIS_TYPE: Final = 0
# Refused markings are counted by label; past this many distinct labels the
# rest are counted together, so a hostile reply cannot grow the tally.
MAX_REFUSED_LABELS: Final = 16
MAX_LABEL_CHARACTERS: Final = 40
_OTHER_LABEL: Final = "(other)"


@dataclass
class Tally:
    """What happened to every record read: kept, refused, invalid or duplicated."""

    received: int = 0
    kept: int = 0
    refused_classification: int = 0
    invalid: int = 0
    duplicates: int = 0
    controls: set[str] = field(default_factory=set)
    refused_markings: Counter[str] = field(default_factory=Counter)
    # Which field made a record invalid, from a fixed set of field names.
    invalid_fields: Counter[str] = field(default_factory=Counter)

    def refuse(self, marking: object) -> None:
        """Count one record refused for its marking, under a short safe label."""
        self.refused_classification += 1
        self._count_refused(_label(marking), 1)

    def _count_refused(self, label: str, count: int) -> None:
        full = len(self.refused_markings) >= MAX_REFUSED_LABELS
        key = _OTHER_LABEL if full and label not in self.refused_markings else label
        self.refused_markings[key] += count

    def add(self, other: Tally) -> None:
        self.received += other.received
        self.kept += other.kept
        self.refused_classification += other.refused_classification
        self.invalid += other.invalid
        self.duplicates += other.duplicates
        self.controls |= other.controls
        for label, count in other.refused_markings.items():
            self._count_refused(label, count)
        self.invalid_fields.update(other.invalid_fields)

    def banner(self) -> str:
        """The marking for any product made from the records kept."""
        if not self.controls:
            return "UNCLASSIFIED"
        return "UNCLASSIFIED//" + "/".join(sorted(self.controls))

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "kept": self.kept,
            "refused_classification": self.refused_classification,
            "invalid": self.invalid,
            "duplicates": self.duplicates,
        }


def _label(marking: object) -> str:
    """A refused marking as printable ASCII of bounded length, for display only."""
    if marking is None:
        return "(none)"
    if not isinstance(marking, str):
        return "(not text)"
    printable = "".join(c if " " <= c <= "~" else " " for c in marking)
    return " ".join(printable.split())[:MAX_LABEL_CHARACTERS] or "(empty)"


def controls_of(record: dict[str, Any]) -> frozenset[str] | None:
    """The accepted controls on an UNCLASSIFIED marking, or None to refuse the record.

    `U` and `U//PR-MRKT` pass. A missing marking, any other level, any control
    not accepted, an empty control, a second `//` block, or any character
    outside ASCII (so no lookalike letter or Unicode space can fold into a
    match) is refused.
    """
    marking = record.get("classificationMarking")
    if not isinstance(marking, str) or not marking.isascii():
        return None
    level, *blocks = marking.strip(" ").upper().split("//")
    if level.strip(" ") not in UNCLASSIFIED_MARKINGS or len(blocks) > 1:
        return None
    controls = (
        frozenset(part.strip(" ") for part in blocks[0].split("/")) if blocks else frozenset()
    )
    return controls if controls <= ACCEPTED_CONTROLS else None


def is_unclassified(record: dict[str, Any]) -> bool:
    """Only an explicit UNCLASSIFIED marking with accepted controls passes."""
    return controls_of(record) is not None


def parse_time(value: object) -> datetime | None:
    """A UDL timestamp as an aware UTC datetime, or None if it is not one."""
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def bounded(value: object, bounds: tuple[float, float]) -> float:
    """A finite number within bounds, else NaN. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return math.nan
    number = float(value)
    low, high = bounds
    return number if math.isfinite(number) and low <= number <= high else math.nan


def sat_no_of(value: object) -> int | None:
    """A catalogue number, refusing booleans, strings and out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SAT_NO:
        return None
    return value


class ObservationRow(NamedTuple):
    """One validated photometric observation, as compact scalars."""

    epoch_ms: int
    mag: float
    mag_norm: float
    sepa: float
    geolon: float
    sensor: str


def observation(record: dict[str, Any], sat_no: int) -> ObservationRow | None:
    """Validate one EO observation for this object, or None to drop it."""
    when = parse_time(record.get("obTime"))
    if when is None or record.get("dataMode") != "REAL" or sat_no_of(record.get("satNo")) != sat_no:
        return None
    mag = bounded(record.get("mag"), MAG_RANGE)
    mag_norm = _normalised(record, mag)
    if math.isnan(mag) and math.isnan(mag_norm):
        return None
    geolon = bounded(record.get("geolon"), GEOLON_RANGE)
    sensor = record.get("idSensor")
    return ObservationRow(
        epoch_ms=round(when.timestamp() * 1000),
        mag=mag,
        mag_norm=mag_norm,
        sepa=SEPA_SIGN * bounded(record.get("solarEqPhaseAngle"), SEPA_RANGE),
        geolon=geolon - 360.0 if geolon > 180.0 else geolon,
        sensor=sensor if isinstance(sensor, str) and _SENSOR.fullmatch(sensor) else "unknown",
    )


def _normalised(record: dict[str, Any], mag: float) -> float:
    """magNormRange if given, else mag - 5 log10(georange / 1e6), as the UDL defines it."""
    given = bounded(record.get("magNormRange"), MAG_NORM_RANGE)
    if not math.isnan(given):
        return given
    georange = bounded(record.get("georange"), GEORANGE_KM)
    if math.isnan(mag) or math.isnan(georange):
        return math.nan
    return bounded(mag - 5.0 * math.log10(georange / 1_000_000.0), MAG_NORM_RANGE)


@dataclass(frozen=True, slots=True)
class Elset:
    """One validated SGP4 element set, in the UDL's units (degrees, revolutions a day)."""

    sat_no: int
    epoch: datetime
    source: str
    mean_motion: float
    eccentricity: float
    inclination: float
    raan: float
    arg_of_perigee: float
    mean_anomaly: float
    b_star: float
    mean_motion_dot: float
    mean_motion_ddot: float
    period: float


_ELEMENTS: Final = (
    ("meanMotion", (0.0, 20.0)),
    ("eccentricity", (0.0, 0.99)),
    ("inclination", (0.0, 180.0)),
    ("raan", (0.0, 360.0)),
    ("argOfPerigee", (0.0, 360.0)),
    ("meanAnomaly", (0.0, 360.0)),
)
# Drag terms: an absent one is taken as zero, which is what an SGP4 elset
# without drag means and is negligible at GEO. A present but bad one is invalid.
_DRAG_TERMS: Final = (
    ("bStar", (-1.0, 1.0)),
    ("meanMotionDot", (-1.0, 1.0)),
    ("meanMotionDDot", (-1.0, 1.0)),
)
MINUTES_PER_DAY: Final = 1440.0


_IDENTITY_KEYS: Final = ("satNo", "onOrbit.satNo", "idOnOrbit")


def _identity(record: dict[str, Any], expected: int | None) -> int | None:
    """The elset's catalogue number, or None to refuse it.

    The tuple endpoint returns the requested satNo column under the key
    `onOrbit.satNo` (live check, 10 November 2026); idOnOrbit carries the
    catalogue number as text for the public catalogue. Fail closed: every
    identity key present must parse to a five-digit number, they must all
    agree, and they must agree with the object the read asked for. Only when
    no identity key is present at all does a per-object read take the number
    it filtered on.
    """
    present = [key for key in _IDENTITY_KEYS if key in record]
    if not present:
        return expected
    found = {
        sat_no_of(record[key]) if key != "idOnOrbit" else _numbered(record[key]) for key in present
    }
    if None in found or len(found) != 1:
        return None
    number = found.pop()
    return number if expected in (None, number) else None


def _numbered(text: object) -> int | None:
    """A five-digit catalogue number written as text, else None."""
    if isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= 5:
        return sat_no_of(int(text))
    return None


def _header(record: dict[str, Any], expected: int | None) -> tuple[int, datetime, str] | str:
    """The elset's object, epoch and source, or the name of the field that failed."""
    sat_no = _identity(record, expected)
    if sat_no is None:
        return "satNo"
    epoch = parse_time(record.get("epoch"))
    if epoch is None:
        return "epoch"
    if record.get("dataMode") != "REAL":
        return "dataMode"
    source = record.get("source")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        return "source"
    kind = record.get("ephemType")
    if isinstance(kind, bool) or kind != SGP4_EPHEMERIS_TYPE:
        return "ephemType"
    return sat_no, epoch, source


def elset_or_problem(record: dict[str, Any], sat_no: int | None = None) -> Elset | str:
    """A validated elset, or the name of the first field that failed validation.

    `sat_no` is the object the read asked for, if it asked for one.
    Classification is checked separately. The period, if absent, is derived
    from the mean motion; it is a display value, not a propagation input.
    """
    header = _header(record, sat_no)
    if isinstance(header, str):
        return header
    elements = []
    for name, limits in _ELEMENTS:
        value = bounded(record.get(name), limits)
        if math.isnan(value):
            return name
        elements.append(value)
    for name, limits in _DRAG_TERMS:
        raw = record.get(name)
        value = 0.0 if raw is None else bounded(raw, limits)
        if math.isnan(value):
            return name
        elements.append(value)
    if elements[0] <= 0.0:  # bounded to 0 to 20 above, so this means exactly zero
        return "meanMotion"
    raw_period = record.get("period")
    # A derived period is bounded like a supplied one: a vanishing but positive
    # mean motion (5e-324) would otherwise give an infinite period.
    derived = MINUTES_PER_DAY / elements[0] if raw_period is None else raw_period
    period = bounded(derived, (0.0, 1e6))
    if math.isnan(period):
        return "period"
    sat_no, epoch, source = header
    return Elset(
        sat_no=sat_no,
        epoch=epoch,
        source=source,
        mean_motion=elements[0],
        eccentricity=elements[1],
        inclination=elements[2],
        raan=elements[3],
        arg_of_perigee=elements[4],
        mean_anomaly=elements[5],
        b_star=elements[6],
        mean_motion_dot=elements[7],
        mean_motion_ddot=elements[8],
        period=period,
    )


def elset(record: dict[str, Any]) -> Elset | None:
    """Validate one elset record, or None to drop it."""
    result = elset_or_problem(record)
    return result if isinstance(result, Elset) else None
