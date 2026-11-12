"""The three reads Light Bee makes: photometry, one object's elsets, and the GEO belt.

Every read asks for named columns only, including the classification marking
so the UNCLASSIFIED check never depends on the service adding it, and
georange so a range-normalised magnitude can be computed when magNormRange is
empty (it was on every record in the first live check). The
predecessor fetched all 106 fields of every observation.

Photometry is read from the archive (`/history`) for the whole window and from
the live store for the most recent days, because the archive can lag
ingestion. The two overlap; exact duplicates are dropped and counted.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from lightbee.udl.client import PAGE_CAP, UdlReadClient
from lightbee.udl.photometry import Photometry, PhotometryChunk, chunk_from_records, combine
from lightbee.udl.records import (
    MAX_SAT_NO,
    Elset,
    Tally,
    controls_of,
    elset_or_problem,
    satellite_name,
)
from lightbee.udl.windows import Fetch, Progress, read_window, time_filter

OBSERVATION_COLUMNS: Final = (
    "classificationMarking",
    "obTime",
    "satNo",
    "mag",
    "magNormRange",
    "georange",
    "solarEqPhaseAngle",
    "geolon",
    "idSensor",
    "dataMode",
)
# line1 and line2 cannot be selected on the tuple endpoint (live check,
# 10 November 2026), so elsets are read as mean elements.
ELSET_COLUMNS: Final = (
    "classificationMarking",
    "satNo",
    "epoch",
    "source",
    "dataMode",
    "ephemType",
    "meanMotion",
    "eccentricity",
    "inclination",
    "raan",
    "argOfPerigee",
    "meanAnomaly",
    "bStar",
    "meanMotionDot",
    "meanMotionDDot",
    "period",
)
# The catalogue read spans every object, so it also asks for idOnOrbit to
# identify an elset that comes back without satNo.
CATALOGUE_COLUMNS: Final = (*ELSET_COLUMNS, "idOnOrbit")
LIVE_OVERLAP: Final = timedelta(days=3)
GEO_MEAN_MOTION: Final = (0.9, 1.1)
CATALOGUE_SPAN: Final = timedelta(hours=12)
DEFAULT_ELSET_SOURCE: Final = "KBR"
# Satellite names (1.2): the on-orbit tuple endpoint answers a comma list of
# catalogue numbers in one read (live check, 12 November 2026: TJS-3, TJS-10
# and TJS-15 in one request, each marked U). A hundred numbers keep the query
# string short; a GEO snapshot of about 900 objects is then 9 small reads.
ONORBIT: Final = "/udl/onorbit/tuple"
NAME_COLUMNS: Final = ("classificationMarking", "satNo", "commonName")
NAME_BATCH: Final = 100
NAME_WORKERS: Final = 8


def fetcher(client: UdlReadClient, path: str, time_field: str, params: dict[str, str]) -> Fetch:
    """A slice reader for one endpoint, asking for at most a page of records."""

    def fetch(start: datetime, end: datetime) -> list[dict[str, Any]]:
        query = {**params, time_field: time_filter(start, end), "maxResults": str(PAGE_CAP)}
        return client.read(path, query)

    return fetch


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("times must be timezone-aware UTC")
    return moment.astimezone(UTC)


def _checked_sat_no(sat_no: int) -> int:
    valid = isinstance(sat_no, int) and not isinstance(sat_no, bool)
    if not valid or not 1 <= sat_no <= MAX_SAT_NO:
        raise ValueError(f"a NORAD catalogue number must be between 1 and {MAX_SAT_NO}")
    return sat_no


def read_observations(
    client: UdlReadClient,
    sat_no: int,
    window: tuple[datetime, datetime],
    *,
    now: datetime,
    on_progress: Progress | None = None,
) -> Photometry:
    """Every REAL, UNCLASSIFIED observation of one object in the window, as columns."""
    sat_no = _checked_sat_no(sat_no)
    start, end = (_utc(moment) for moment in window)
    params = {"satNo": str(sat_no), "dataMode": "REAL", "columns": ",".join(OBSERVATION_COLUMNS)}

    def transform(records: list[dict[str, Any]]) -> PhotometryChunk:
        return chunk_from_records(records, sat_no)

    archive = fetcher(client, "/udl/eoobservation/history", "obTime", params)
    chunks = read_window(
        archive, start, end, transform, workers=client.throttle.limit, on_progress=on_progress
    )
    live_start = max(start, _utc(now) - LIVE_OVERLAP)
    if live_start < end:
        live = fetcher(client, "/udl/eoobservation/tuple", "obTime", params)
        chunks += read_window(live, live_start, end, transform, workers=client.throttle.limit)
    return combine(chunks)


@dataclass
class ElsetRead:
    """Validated elsets, oldest first, with their tally."""

    elsets: list[Elset]
    tally: Tally = field(default_factory=Tally)


def _usable(
    record: dict[str, Any], source: str, sat_no: int | None, window: tuple[datetime, datetime]
) -> Elset | str:
    """A usable elset, or why not. An epoch outside the window asked for is refused,
    so a stray far-future epoch cannot make the propagator work hard."""
    item = elset_or_problem(record, sat_no)
    if isinstance(item, str):
        return item
    if item.source != source:
        return "source"
    if not window[0] <= item.epoch <= window[1]:
        return "epoch"
    return item


def _validated_elsets(
    raw: Iterable[dict[str, Any]],
    source: str,
    sat_no: int | None,
    window: tuple[datetime, datetime],
) -> ElsetRead:
    result = ElsetRead([])
    for record in raw:
        result.tally.received += 1
        controls = controls_of(record)
        if controls is None:
            result.tally.refuse(record.get("classificationMarking"))
            continue
        item = _usable(record, source, sat_no, window)
        if isinstance(item, str):
            result.tally.invalid += 1
            result.tally.invalid_fields[item] += 1
            continue
        result.tally.controls |= controls
        result.elsets.append(item)
    result.elsets.sort(key=lambda item: item.epoch)
    result.tally.kept = len(result.elsets)
    return result


def _elset_reader(
    source: str, window: tuple[datetime, datetime], sat_no: int | None = None
) -> Callable[[list[dict[str, Any]]], ElsetRead]:
    def transform(records: list[dict[str, Any]]) -> ElsetRead:
        return _validated_elsets(records, source, sat_no, window)

    return transform


def _merge(parts: Iterable[ElsetRead]) -> ElsetRead:
    merged = ElsetRead([])
    for part in parts:
        merged.elsets.extend(part.elsets)
        merged.tally.add(part.tally)
    merged.elsets.sort(key=lambda item: item.epoch)
    return merged


def read_elsets(
    client: UdlReadClient,
    sat_no: int,
    window: tuple[datetime, datetime],
    *,
    source: str = DEFAULT_ELSET_SOURCE,
) -> ElsetRead:
    """One object's elsets from one provider across the window."""
    sat_no = _checked_sat_no(sat_no)
    start, end = (_utc(moment) for moment in window)
    params = {
        "satNo": str(sat_no),
        "source": source,
        "dataMode": "REAL",
        "columns": ",".join(ELSET_COLUMNS),
    }
    fetch = fetcher(client, "/udl/elset/tuple", "epoch", params)
    reader = _elset_reader(source, (start, end), sat_no)
    parts = read_window(fetch, start, end, reader, workers=client.throttle.limit)
    return _merge(parts)


def read_names(
    client: UdlReadClient, sat_nos: Iterable[int], check: Callable[[], None] = lambda: None
) -> tuple[dict[int, str], Tally]:
    """Common names for these catalogue numbers, for display only.

    The batches are read in parallel, as the photometry spans are, so a GEO
    snapshot's ten or so reads cost about one read's time. `check` runs
    before each read, so a cancelled run stops asking. Every record must carry
    an accepted UNCLASSIFIED marking, and its controls join the product banner
    like any other read. A record for a number not asked for, or a second
    record for one already named, is not used.
    """
    wanted = sorted({_checked_sat_no(n) for n in sat_nos})
    batches = [wanted[i : i + NAME_BATCH] for i in range(0, len(wanted), NAME_BATCH)]

    def read(batch: list[int]) -> list[dict[str, Any]]:
        check()
        params = {
            "satNo": ",".join(map(str, batch)),
            "columns": ",".join(NAME_COLUMNS),
            "maxResults": str(len(batch) * 2),
        }
        return client.read(ONORBIT, params)

    names: dict[int, str] = {}
    tally = Tally()
    if not batches:
        return names, tally
    workers = min(NAME_WORKERS, len(batches), client.throttle.limit)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lightbee-names") as pool:
        # Each read runs in this run's context, so its log lines carry the request ID.
        answers = list(pool.map(lambda b: contextvars.copy_context().run(read, b), batches))
    # Taken in batch order, so the result never depends on which read finished first.
    for batch, records in zip(batches, answers, strict=True):
        asked = set(batch)
        for record in records:
            _take_name(record, asked, names, tally)
    return names, tally


def _take_name(
    record: dict[str, Any], asked: set[int], names: dict[int, str], tally: Tally
) -> None:
    """Keep one on-orbit record's name if its marking, number and name all pass."""
    tally.received += 1
    controls = controls_of(record)
    if controls is None:
        tally.refuse(record.get("classificationMarking"))
        return
    named = satellite_name(record)
    if named is None or named[0] not in asked:
        tally.invalid += 1
        tally.invalid_fields["commonName" if named is None else "satNo"] += 1
        return
    if named[0] in names:
        tally.duplicates += 1
        return
    names[named[0]] = named[1]
    tally.kept += 1
    tally.controls |= controls


def read_geo_catalogue(
    client: UdlReadClient,
    moment: datetime,
    *,
    source: str = DEFAULT_ELSET_SOURCE,
) -> tuple[dict[int, Elset], Tally]:
    """One elset per GEO object (the epoch nearest the moment), from one provider."""
    centre = _utc(moment)
    params = {"source": source, "dataMode": "REAL", "columns": ",".join(CATALOGUE_COLUMNS)}
    fetch = fetcher(client, "/udl/elset/tuple", "epoch", params)
    span = (centre - CATALOGUE_SPAN / 2, centre + CATALOGUE_SPAN / 2)
    reader = _elset_reader(source, span)
    read = _merge(read_window(fetch, *span, reader, workers=client.throttle.limit))
    nearest: dict[int, Elset] = {}
    low, high = GEO_MEAN_MOTION
    for item in read.elsets:
        if not low <= item.mean_motion <= high:
            continue
        held = nearest.get(item.sat_no)
        if held is None or abs(item.epoch - centre) < abs(held.epoch - centre):
            nearest[item.sat_no] = item
    return nearest, read.tally
