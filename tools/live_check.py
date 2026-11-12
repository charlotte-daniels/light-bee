"""Light Bee live check: measure the real UDL before the analysis is built on it.

Read-only. Run it on your own machine with your own UDL login; nothing is
stored except a small report of counts and timings (no credentials, no
observation data). It answers the questions the build plan's day 1 decision
rests on:

1. Does the login work?
2. Is a column-trimmed read faster than a full read, and by how much?
3. How often are solarEqPhaseAngle, magNormRange and geolon filled in, and does
   every record come back with an accepted UNCLASSIFIED marking under `columns`?
4. Is the `start..end` time filter inclusive at each end?
5. Which elset sources exist for the object, and does KBR have elsets? If an
   elset read is refused, which query parameter does the UDL object to?
6. How big is a 12-hour GEO catalogue snapshot, and how long does it take?
7. Does a cold run fit in 3 minutes: 2 years of photometry, 2 years of KBR
   elsets and the GEO catalogue, timed together, for each reference object?
   And what do the analyses then say, and how long do they take: the seasonal
   flag for the last 7 days and the close approaches over the same week?
8. Is the UDL's solarEqPhaseAngle the same way round as the predecessor's
   SEPA, which the reference plots were drawn with?

Every measurement uses a fresh client, so a slowdown in one cannot carry into
the next.

From the repository root (Windows PowerShell shown):

    python -m venv .venv
    .\\.venv\\Scripts\\python.exe -m pip install httpx2==2.16.0 numpy==2.4.6
    $env:PYTHONPATH = "src"
    .\\.venv\\Scripts\\python.exe tools\\live_check.py

It needs httpx2 and numpy (as Light Bee does) and Python 3.11 or newer.
Options: --objects 43874 58204 63157   --years 2   --skip-cold   --out FILE
         --use-proxy   (only if your machine reaches the internet through a proxy)
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from statistics import median
from typing import Any, Final

import numpy as np
import sepa_check

from lightbee.analysis.screen import approaches
from lightbee.analysis.seasonal import build_grid, seasonal_flag
from lightbee.analysis.series import DAY_MS, Series
from lightbee.config import load_settings
from lightbee.udl.client import Credentials, UdlError, UdlReadClient, UdlRequestError
from lightbee.udl.queries import (
    CATALOGUE_COLUMNS,
    ELSET_COLUMNS,
    OBSERVATION_COLUMNS,
    fetcher,
    read_elsets,
    read_geo_catalogue,
    read_observations,
)
from lightbee.udl.records import controls_of, parse_time
from lightbee.udl.windows import read_window, time_filter

REFERENCE_OBJECTS: Final = (43874, 58204, 63157)
COLD_TARGET_SECONDS: Final = 180.0
SPEED_OBJECT: Final = 58204
SPEED_DAYS: Final = 30
FIELDS: Final = (
    "solarEqPhaseAngle",
    "magNormRange",
    "mag",
    "geolon",
    "idSensor",
    "georange",
    "range",
)
# Asked for only in the fields check, to see whether line-of-sight range is filled.
RANGE_COLUMNS: Final = ("range",)
ELSETS: Final = "/udl/elset/tuple"
HISTORY: Final = "/udl/eoobservation/history"
ONORBIT_TUPLE: Final = "/udl/onorbit/tuple"

# A client factory takes optional client settings (for example a larger body
# cap for the speed comparison) and returns a fresh client.
ClientFactory = Callable[..., UdlReadClient]
# The full-field read in the speed comparison must not be split by the app's
# 16 MiB safety cap, or the comparison would measure re-reads, not field cost.
SPEED_BODY_BYTES: Final = 512 * 1024 * 1024


@dataclass
class Report:
    """Counts, fractions and timings only. Never credentials, never records."""

    started: str
    checks: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def record(self, name: str, passed: bool, **details: Any) -> None:
        self.checks[name] = {"passed": passed, **details}
        if not passed:
            self.failures.append(name)
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {json.dumps(details, default=str)}")


def _failure(exc: UdlError) -> dict[str, str]:
    """An error for the report: the message, plus the UDL's own sanitised reason if any."""
    details = {"error": str(exc)}
    if isinstance(exc, UdlRequestError) and exc.detail:
        details["detail"] = exc.detail
    return details


def _timed(action: Callable[[], Any]) -> tuple[Any, float]:
    began = time.perf_counter()
    result = action()
    return result, time.perf_counter() - began


def _raw(
    client: UdlReadClient, params: dict[str, str], window: tuple[datetime, datetime]
) -> list[dict[str, Any]]:
    chunks = read_window(
        fetcher(client, HISTORY, "obTime", params),
        *window,
        lambda records: records,
        workers=client.throttle.limit,
    )
    return [record for chunk in chunks for record in chunk]


def check_login(make_client: ClientFactory, report: Report, now: datetime) -> bool:
    params = {
        "satNo": str(REFERENCE_OBJECTS[0]),
        "columns": "classificationMarking,satNo",
        "epoch": time_filter(now - timedelta(days=1), now),
    }
    try:
        with make_client() as client:
            records = client.read(ELSETS, params)
    except UdlError as exc:
        report.record("login", False, **_failure(exc))
        return False
    report.record("login", True, records=len(records))
    return True


def _read_rate(
    make_client: ClientFactory, params: dict[str, str], now: datetime
) -> tuple[int, float]:
    window = (now - timedelta(days=SPEED_DAYS + 3), now - timedelta(days=3))
    with make_client(max_body_bytes=SPEED_BODY_BYTES) as client:
        records, seconds = _timed(partial(_raw, client, params, window))
    return len(records), seconds


def check_speed(make_client: ClientFactory, report: Report, now: datetime) -> None:
    base = {"satNo": str(SPEED_OBJECT), "dataMode": "REAL"}
    trimmed = {**base, "columns": ",".join(OBSERVATION_COLUMNS)}
    # Alternate the order so a service-side cache cannot favour either form.
    runs = [
        ("trimmed", _read_rate(make_client, trimmed, now)),
        ("full", _read_rate(make_client, base, now)),
        ("trimmed", _read_rate(make_client, trimmed, now)),
        ("full", _read_rate(make_client, base, now)),
    ]
    kinds = ("trimmed", "full")
    seconds = {kind: median(s for k, (_, s) in runs if k == kind) for kind in kinds}
    counts = {kind: max(n for k, (n, _) in runs if k == kind) for kind in kinds}
    speedup = round(seconds["full"] / seconds["trimmed"], 2) if seconds["trimmed"] else 0.0
    report.record(
        "trimmed_vs_full",
        counts["trimmed"] == counts["full"] and counts["trimmed"] > 0,
        object=SPEED_OBJECT,
        days=SPEED_DAYS,
        records=counts,
        median_seconds={kind: round(value, 1) for kind, value in seconds.items()},
        rows_per_second={k: round(counts[k] / seconds[k]) if seconds[k] else 0 for k in kinds},
        speedup=speedup,
    )


def _filled(records: list[dict[str, Any]], name: str) -> float:
    present = sum(1 for record in records if record.get(name) not in (None, ""))
    return round(present / len(records), 4) if records else 0.0


def _sepa_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        float(r["solarEqPhaseAngle"])
        for r in records
        if isinstance(r.get("solarEqPhaseAngle"), float | int)
    ]
    if not values:
        return {"sepa_min": None, "sepa_max": None, "sepa_share_negative": None}
    negative = sum(1 for value in values if value < 0) / len(values)
    return {
        "sepa_min": min(values),
        "sepa_max": max(values),
        "sepa_share_negative": round(negative, 3),
    }


def check_fields(make_client: ClientFactory, report: Report, now: datetime, sat_no: int) -> None:
    columns = ",".join((*OBSERVATION_COLUMNS, *RANGE_COLUMNS))
    params = {"satNo": str(sat_no), "dataMode": "REAL", "columns": columns}
    with make_client() as client:
        records = _raw(client, params, (now - timedelta(days=10), now - timedelta(days=3)))
    markings = Counter(str(r.get("classificationMarking")) for r in records)
    filled = {name: _filled(records, name) for name in FIELDS}
    marked = sum(1 for record in records if controls_of(record) is not None)
    report.record(
        f"fields_{sat_no}",
        bool(records)
        and marked == len(records)
        and filled["solarEqPhaseAngle"] >= 0.9
        and max(filled["magNormRange"], filled["georange"]) >= 0.9,
        records=len(records),
        filled=filled,
        markings=dict(markings),
        **_sepa_summary(records),
    )


def _fraction_digits(value: object) -> int:
    """Digits after the decimal point in a UDL timestamp; 0 when there is no fraction."""
    text = str(value or "")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("Z"))


def check_boundaries(make_client: ClientFactory, report: Report, now: datetime) -> None:
    params = {
        "satNo": str(SPEED_OBJECT),
        "dataMode": "REAL",
        "columns": "classificationMarking,obTime",
    }
    with make_client() as client:
        sample = _raw(client, params, (now - timedelta(days=6), now - timedelta(days=5)))
        times = sorted(t for t in (parse_time(r.get("obTime")) for r in sample) if t is not None)
        if not times:
            report.record("time_filter_bounds", False, error="no observations to test with")
            return
        whole_ms = [t for t in times if t.microsecond % 1000 == 0]
        if not whole_ms:
            report.record("time_filter_bounds", False, error="no whole-millisecond times to test")
            return
        moment = whole_ms[len(whole_ms) // 2]
        second = timedelta(seconds=1)
        starting = client.read(HISTORY, {**params, "obTime": time_filter(moment, moment + second)})
        ending = client.read(HISTORY, {**params, "obTime": time_filter(moment - second, moment)})

    def found(rows: list[dict[str, Any]]) -> bool:
        return any(parse_time(r.get("obTime")) == moment for r in rows)

    digits = max((_fraction_digits(r.get("obTime")) for r in sample), default=0)
    report.record(
        "time_filter_bounds",
        True,
        start_inclusive=found(starting),
        end_inclusive=found(ending),
        fraction_digits_seen=digits,
    )


SEPA_GEOMETRY: Final = ("ra", "declination", "range", "senlat", "senlon", "senalt")


def _column(records: list[dict[str, Any]], name: str) -> Any:
    values = [r.get(name) for r in records]
    return np.array(
        [v if isinstance(v, int | float) and not isinstance(v, bool) else np.nan for v in values],
        dtype=np.float64,
    )


def check_sepa(make_client: ClientFactory, report: Report, now: datetime) -> None:
    """Recompute SEPA with the predecessor's geometry and compare it with the UDL's."""
    columns = ",".join((*OBSERVATION_COLUMNS, *SEPA_GEOMETRY))
    params = {"satNo": str(REFERENCE_OBJECTS[0]), "dataMode": "REAL", "columns": columns}
    with make_client() as client:
        records = _raw(client, params, (now - timedelta(days=5), now - timedelta(days=3)))
    # Only records the app itself would accept are measured.
    records = [r for r in records if controls_of(r) is not None]
    times = [parse_time(r.get("obTime")) for r in records]
    records = [r for r, t in zip(records, times, strict=True) if t is not None]
    epoch = np.array([t.timestamp() * 1000 for t in times if t is not None], dtype=np.float64)
    udl = _column(records, "solarEqPhaseAngle")
    geometry = {name: _column(records, name) for name in SEPA_GEOMETRY}
    complete = np.all([np.isfinite(v) for v in geometry.values()], axis=0) if records else []
    by_longitude = sepa_check.compare(
        udl, sepa_check.from_longitude(epoch, _column(records, "geolon"))
    )
    by_sight: dict[str, Any] = {"compared": 0, "verdict": "sensor position not filled"}
    if np.any(complete):
        rows = {name: values[complete] for name, values in geometry.items()}
        rows["epoch_ms"] = epoch[complete]
        by_sight = sepa_check.compare(udl[complete], sepa_check.from_line_of_sight(rows))
    report.record(
        "sepa_sign",
        by_longitude["verdict"] in {"same convention", "mirrored"},
        records=len(records),
        geometry_filled={name: _filled(records, name) for name in SEPA_GEOMETRY},
        from_longitude=by_longitude,
        from_line_of_sight=by_sight,
    )


def check_elsets(make_client: ClientFactory, report: Report, now: datetime, sat_no: int) -> None:
    window = (now - timedelta(days=7), now)
    params = {
        "satNo": str(sat_no),
        "columns": ",".join(CATALOGUE_COLUMNS),
        "epoch": time_filter(*window),
        "maxResults": "25000",
    }
    with make_client() as client:
        raw = client.read(ELSETS, params)
        kbr = read_elsets(client, sat_no, window, source="KBR")
    report.record(
        f"elsets_{sat_no}",
        kbr.tally.kept > 0,
        sources_last_7_days=dict(Counter(str(record.get("source")) for record in raw)),
        ephemeris_types=dict(Counter(str(record.get("ephemType")) for record in raw)),
        data_modes=dict(Counter(str(record.get("dataMode")) for record in raw)),
        kbr_tally=kbr.tally.as_dict(),
        kbr_refused_markings=dict(kbr.tally.refused_markings),
        kbr_invalid_fields=dict(kbr.tally.invalid_fields),
        kbr_field_types=_field_types([r for r in raw if r.get("source") == "KBR"]),
        kbr_epoch_shapes=_shapes(r.get("epoch") for r in raw if r.get("source") == "KBR"),
        returned_fields_by_source=_returned_fields(raw),
    )


def _returned_fields(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per source, which field names came back and how often (names only, no values)."""
    found: dict[str, Counter[str]] = {}
    for record in records:
        found.setdefault(str(record.get("source"))[:40], Counter()).update(record.keys())
    return {source: dict(sorted(names.items())) for source, names in found.items()}


def _field_types(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """For each elset column, how many records hold which JSON type (no values)."""
    return {
        column: dict(
            Counter(type(r[column]).__name__ if column in r else "missing" for r in records)
        )
        for column in CATALOGUE_COLUMNS
    }


def _shapes(values: Iterable[object]) -> dict[str, int]:
    """Timestamp layouts with every digit shown as 9, so the format shows but no value does."""
    shapes = Counter("".join("9" if c.isdigit() else c for c in str(v))[:40] for v in values)
    return dict(shapes.most_common(5))


def _elset_variants(sat_no: int, now: datetime) -> dict[str, dict[str, str]]:
    """The login query, then one change at a time, so a refusal names its cause."""
    base = {
        "satNo": str(sat_no),
        "columns": "classificationMarking,satNo",
        "epoch": time_filter(now - timedelta(days=1), now),
    }
    return {
        "login_form": base,
        "max_results_25000": {**base, "maxResults": "25000"},
        "max_results_10000": {**base, "maxResults": "10000"},
        "source_kbr": {**base, "source": "KBR"},
        "data_mode_real": {**base, "dataMode": "REAL"},
        "full_columns": {**base, "columns": ",".join(ELSET_COLUMNS)},
        "app_query": {
            **base,
            "source": "KBR",
            "dataMode": "REAL",
            "columns": ",".join(ELSET_COLUMNS),
            "maxResults": "25000",
        },
    }


def _unselectable(client: UdlReadClient, base: dict[str, str]) -> list[str]:
    """Each elset column asked for on its own; the ones the UDL refuses to select."""
    refused = []
    for column in CATALOGUE_COLUMNS:
        try:
            client.read(ELSETS, {**base, "columns": f"classificationMarking,{column}"})
        except UdlRequestError:
            refused.append(column)
    return refused


def check_elset_queries(make_client: ClientFactory, report: Report, now: datetime) -> None:
    outcomes: dict[str, dict[str, Any]] = {}
    variants = _elset_variants(REFERENCE_OBJECTS[0], now)
    with make_client() as client:
        for name, params in variants.items():
            try:
                outcomes[name] = {"ok": True, "records": len(client.read(ELSETS, params))}
            except UdlError as exc:
                outcomes[name] = {"ok": False, **_failure(exc)}
        unselectable = _unselectable(client, variants["login_form"])
    report.record(
        "elset_queries",
        all(o["ok"] for o in outcomes.values()) and not unselectable,
        variants=outcomes,
        unselectable_columns=unselectable,
    )


def _name_variants(objects: list[int]) -> dict[str, tuple[str, dict[str, str]]]:
    """The name read 1.2 makes, for one object and for a comma list.

    The first live check (12 November 2026) also tried the plain `/udl/onorbit`
    path; both answered, and 1.2 uses only the tuple endpoint.
    """
    first = str(objects[0])
    columns = "classificationMarking,satNo,commonName"
    return {
        "tuple_one": (ONORBIT_TUPLE, {"satNo": first, "columns": columns}),
        "tuple_list": (ONORBIT_TUPLE, {"satNo": ",".join(map(str, objects)), "columns": columns}),
    }


def _name_outcome(records: list[dict[str, Any]]) -> dict[str, Any]:
    """What one name read returned: fields present, markings and the names themselves.

    Satellite names are public catalogue data, so they are shown; nothing else
    from the records is.
    """
    return {
        "ok": True,
        "records": len(records),
        "fields": sorted({key for record in records for key in record})[:60],
        "markings": dict(Counter(str(r.get("classificationMarking"))[:60] for r in records)),
        "names": {
            str(r.get("satNo", r.get("onOrbit.satNo"))): str(r.get("commonName"))[:60]
            for r in records[:10]
        },
    }


def check_names(make_client: ClientFactory, report: Report, objects: list[int]) -> None:
    """1.2: can satellite names be read, from which endpoint, in one request or one each?"""
    outcomes: dict[str, dict[str, Any]] = {}
    with make_client() as client:
        for name, (path, params) in _name_variants(objects).items():
            try:
                outcomes[name] = _name_outcome(client.read(path, params))
            except UdlError as exc:
                outcomes[name] = {"ok": False, **_failure(exc)}
    named = [o for o in outcomes.values() if o.get("ok") and o.get("names")]
    report.record("names", bool(named), variants=outcomes)


def check_catalogue(make_client: ClientFactory, report: Report, now: datetime) -> None:
    with make_client() as client:
        read = partial(read_geo_catalogue, client, now - timedelta(hours=6))
        (catalogue, tally), seconds = _timed(read)
    report.record(
        "geo_catalogue",
        len(catalogue) > 100 and seconds <= 60,
        geo_objects=len(catalogue),
        elsets=tally.as_dict(),
        invalid_fields=dict(tally.invalid_fields),
        seconds=round(seconds, 1),
    )


def _progress(sat_no: int) -> Callable[[int, int, int], None]:
    last = [0.0]

    def show(records: int, done: int, pending: int) -> None:
        if time.perf_counter() - last[0] > 5:
            last[0] = time.perf_counter()
            print(f"      {sat_no}: {records} records, {done} slices done, {pending} pending")

    return show


def _part(action: Callable[[], Any]) -> tuple[Any, float, dict[str, str]]:
    """Run one part of a cold run; a refusal is recorded and the other parts still run."""
    began = time.perf_counter()
    try:
        result = action()
    except UdlError as exc:
        return None, time.perf_counter() - began, _failure(exc)
    return result, time.perf_counter() - began, {}


def check_cold(
    make_client: ClientFactory, report: Report, now: datetime, sat_no: int, years: float
) -> None:
    window = (now - timedelta(days=365.25 * years), now)
    with make_client() as client:
        limit_before = client.throttle.limit
        photometry, photometry_s, photometry_error = _part(
            partial(
                read_observations, client, sat_no, window, now=now, on_progress=_progress(sat_no)
            )
        )
        elsets, elsets_s, elsets_error = _part(partial(read_elsets, client, sat_no, window))
        catalogue, catalogue_s, catalogue_error = _part(partial(read_geo_catalogue, client, now))
        total = photometry_s + elsets_s + catalogue_s
        errors = {
            part: error
            for part, error in (
                ("photometry", photometry_error),
                ("elsets", elsets_error),
                ("catalogue", catalogue_error),
            )
            if error
        }
        received = photometry.tally.received if photometry is not None else 0
        report.record(
            f"cold_{sat_no}",
            not errors and total <= COLD_TARGET_SECONDS and received > 0,
            years=years,
            total_seconds=round(total, 1),
            target_seconds=COLD_TARGET_SECONDS,
            seconds={
                "photometry": round(photometry_s, 1),
                "elsets": round(elsets_s, 1),
                "catalogue": round(catalogue_s, 1),
            },
            photometry=photometry.tally.as_dict() if photometry is not None else None,
            refused_markings=(
                dict(photometry.tally.refused_markings) if photometry is not None else {}
            ),
            photometry_megabytes=(
                round(photometry.nbytes / 1e6, 1) if photometry is not None else 0.0
            ),
            elsets=elsets.tally.as_dict() if elsets is not None else None,
            elset_invalid_fields=dict(elsets.tally.invalid_fields) if elsets is not None else {},
            geo_objects=len(catalogue[0]) if catalogue is not None else 0,
            analysis=_analyse(photometry, elsets, catalogue, now),
            errors=errors,
            rate_limited=client.rate_limited,
            throttle_limit={"before": limit_before, "after": client.throttle.limit},
        )


def _analyse(photometry: Any, elsets: Any, catalogue: Any, now: datetime) -> dict[str, Any]:
    """The analyses on the real data: verdicts and timings only, no observation values."""
    if photometry is None or not len(photometry):
        return {"skipped": "no photometry"}
    end_ms = round(now.timestamp() * 1000)
    week_ms = 7 * DAY_MS
    began = time.perf_counter()
    series = Series.of(photometry)
    build_grid(series, end_ms=end_ms, days=731)
    grid_s = time.perf_counter() - began
    flag, flag_s = _timed(
        partial(seasonal_flag, series, recent_start_ms=end_ms - week_ms, recent_end_ms=end_ms)
    )
    result: dict[str, Any] = {
        "magnitude_basis": series.basis,
        "seasonal": {
            "outcome": flag.outcome,
            "headline": flag.headline,
            "comparable_bins": flag.comparable_bins,
            "median_delta_mag": _rounded(flag.median_delta_mag, 3),
            "median_robust_z": _rounded(flag.median_robust_z, 2),
            "recent_longitude_spread_deg": round(flag.context.recent_spread_deg, 2),
        },
        "seconds": {"grid": round(grid_s, 2), "seasonal_flag": round(flag_s, 2)},
    }
    if elsets is not None and elsets.elsets and catalogue is not None:
        found, approaches_s = _timed(
            partial(approaches, end_ms - week_ms, end_ms, elsets.elsets, catalogue[0])
        )
        result["approaches_last_7_days"] = {
            "inside_150_km": sum(1 for a in found if a.inside),
            "within_reach": len(found),
        }
        result["seconds"]["approaches"] = round(approaches_s, 2)
    return result


def _rounded(value: float, digits: int) -> float | None:
    return round(value, digits) if np.isfinite(value) else None


def run(make_client: ClientFactory, args: argparse.Namespace, now: datetime) -> Report:
    report = Report(started=now.isoformat())
    if not check_login(make_client, report, now):
        return report
    steps: list[tuple[str, Callable[[], None]]] = [
        ("names", partial(check_names, make_client, report, list(args.objects))),
        ("elset_queries", partial(check_elset_queries, make_client, report, now)),
        ("trimmed_vs_full", partial(check_speed, make_client, report, now)),
        ("time_filter_bounds", partial(check_boundaries, make_client, report, now)),
        ("sepa_sign", partial(check_sepa, make_client, report, now)),
    ]
    for sat_no in args.objects:
        steps.append((f"fields_{sat_no}", partial(check_fields, make_client, report, now, sat_no)))
        steps.append((f"elsets_{sat_no}", partial(check_elsets, make_client, report, now, sat_no)))
    steps.append(("geo_catalogue", partial(check_catalogue, make_client, report, now)))
    if not args.skip_cold:
        steps.extend(
            (f"cold_{s}", partial(check_cold, make_client, report, now, s, args.years))
            for s in args.objects
        )
    if args.names_only:
        steps = steps[:1]  # the names check is first
    for name, step in steps:
        try:
            step()
        except UdlError as exc:
            report.record(name, False, **_failure(exc))
    return report


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only live check of the UDL for Light Bee.")
    parser.add_argument("--objects", type=int, nargs="+", default=list(REFERENCE_OBJECTS))
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--skip-cold", action="store_true")
    parser.add_argument(
        "--names-only",
        action="store_true",
        help="run only the login and the 1.2 satellite-name check (about 10 seconds)",
    )
    parser.add_argument("--out", type=Path, default=Path("live-check-report.json"))
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="honour this machine's proxy settings (the app itself never does)",
    )
    return parser.parse_args(argv)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: list[str] | None = None,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    args = _arguments(argv)
    credentials = Credentials(
        ask("UDL username: ").strip(), ask_secret("UDL password (not shown): ")
    )
    settings = load_settings()
    now = clock().replace(microsecond=0)

    def make_client(**options: Any) -> UdlReadClient:
        return UdlReadClient(settings, credentials, trust_env=args.use_proxy, **options)

    report = run(make_client, args, now)
    args.out.write_text(json.dumps(report.__dict__, indent=2, default=str), encoding="utf-8")
    passed = len(report.checks) - len(report.failures)
    print(f"\n{passed} of {len(report.checks)} checks passed; report written to {args.out}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
