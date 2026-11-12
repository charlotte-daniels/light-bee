"""The live check script, run end to end against the fake UDL."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import live_check
import numpy as np
import pytest
import sepa_check

from lightbee.config import Settings
from lightbee.udl.client import Credentials, UdlReadClient
from tests.fake_udl import FakeUdl, dense_history, elset_record

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _fake() -> FakeUdl:
    history = []
    for sat in live_check.REFERENCE_OBJECTS:
        history += dense_history(sat, NOW - timedelta(days=60), days=60, per_day=20)
    elsets = [
        elset_record(sat, NOW - timedelta(hours=h))
        for sat in live_check.REFERENCE_OBJECTS
        for h in (2, 30)
    ]
    elsets += [elset_record(40000 + k, NOW - timedelta(hours=6)) for k in range(120)]
    _consistent_sepa(history)
    onorbit = [
        {"satNo": sat, "commonName": f"TEST SAT {sat}", "classificationMarking": "U"}
        for sat in live_check.REFERENCE_OBJECTS
    ]
    return FakeUdl(history=history, live=[], elsets=elsets, onorbit=onorbit)


def _consistent_sepa(history: list[dict[str, Any]], sign: float = 1.0) -> None:
    """Give the fake's observations a SEPA that agrees with their geometry."""
    epoch = np.array([datetime.fromisoformat(r["obTime"]).timestamp() * 1000 for r in history])
    lon = np.array([r["geolon"] for r in history], dtype=np.float64)
    for record, value in zip(history, sepa_check.from_longitude(epoch, lon), strict=True):
        record["solarEqPhaseAngle"] = sign * float(value)


def _factory(fake: FakeUdl) -> Callable[[], UdlReadClient]:
    def make(**options: Any) -> UdlReadClient:
        return UdlReadClient(
            Settings(),
            Credentials("analyst", "not-real-pw"),
            transport=fake.transport(),
            sleep=lambda _: None,
            **options,
        )

    return make


def test_every_check_passes_against_a_healthy_service() -> None:
    fake = _fake()
    report = live_check.run(_factory(fake), live_check._arguments(["--years", "0.15"]), NOW)
    assert report.failures == []
    assert report.checks["trimmed_vs_full"]["records"]["trimmed"] > 0
    assert report.checks["time_filter_bounds"]["start_inclusive"] is True
    assert report.checks["time_filter_bounds"]["fraction_digits_seen"] == 3
    assert report.checks["fields_43874"]["filled"]["solarEqPhaseAngle"] == 1.0
    assert report.checks["fields_43874"]["markings"] == {"U": 7 * 20}
    assert report.checks["elsets_43874"]["sources_last_7_days"] == {"KBR": 2}
    assert report.checks["elsets_43874"]["ephemeris_types"] == {"0": 2}
    assert report.checks["elset_queries"]["unselectable_columns"] == []
    assert report.checks["geo_catalogue"]["geo_objects"] >= 120
    cold = report.checks["cold_58204"]
    assert cold["photometry"]["kept"] > 0
    assert cold["refused_markings"] == {}
    assert set(cold["seconds"]) == {"photometry", "elsets", "catalogue"}
    assert cold["throttle_limit"] == {"before": 24, "after": 24}
    analysis = cold["analysis"]
    assert analysis["seasonal"]["outcome"] in {
        "consistent",
        "marginal",
        "offset",
        "departs",
        "inconclusive",
    }
    assert analysis["magnitude_basis"] == "range-normalised"
    assert set(analysis["seconds"]) == {"grid", "seasonal_flag", "approaches"}
    assert analysis["approaches_last_7_days"]["within_reach"] >= 0
    assert all(request.method == "GET" for request in fake.requests)


def test_the_names_check_shows_each_endpoint_its_fields_markings_and_names() -> None:
    fake = _fake()
    report = live_check.run(_factory(fake), live_check._arguments(["--names-only"]), NOW)
    assert list(report.checks) == ["login", "names"]
    assert report.failures == []
    variants = report.checks["names"]["variants"]
    assert variants["tuple_one"]["names"] == {"43874": "TEST SAT 43874"}
    assert variants["tuple_one"]["fields"] == ["classificationMarking", "commonName", "satNo"]
    assert variants["tuple_one"]["markings"] == {"U": 1}
    assert variants["tuple_list"]["records"] == 3
    assert {r.url.path for r in fake.requests} == {"/udl/elset/tuple", "/udl/onorbit/tuple"}
    assert all(request.method == "GET" for request in fake.requests)


def test_the_names_check_fails_when_no_endpoint_gives_a_name() -> None:
    fake = _fake()
    fake.onorbit = []
    report = live_check.run(_factory(fake), live_check._arguments(["--names-only"]), NOW)
    assert report.failures == ["names"]


def test_a_refused_login_stops_after_the_first_check() -> None:
    fake = _fake()
    fake.fail_once["/udl/elset/tuple"] = 401
    report = live_check.run(_factory(fake), live_check._arguments(["--skip-cold"]), NOW)
    assert report.failures == ["login"]
    assert len(fake.requests) == 1


def test_the_accepted_pr_mrkt_control_passes_the_fields_check() -> None:
    fake = _fake()
    for record in fake.history:
        record["classificationMarking"] = "U//PR-MRKT"
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    assert report.checks["fields_43874"]["passed"] is True
    assert report.checks["fields_43874"]["markings"] == {"U//PR-MRKT": 7 * 20}


def test_missing_markings_fail_the_fields_check() -> None:
    fake = _fake()
    for record in fake.history:
        record.pop("classificationMarking")
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    assert "fields_43874" in report.failures


def test_a_failing_step_is_recorded_and_the_rest_still_run() -> None:
    fake = _fake()
    fake.history = []
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    assert "trimmed_vs_full" in report.failures
    assert "time_filter_bounds" in report.failures
    assert "fields_43874" in report.failures
    assert report.checks["geo_catalogue"]["passed"]


def test_main_writes_a_report_with_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    monkeypatch.setattr(
        live_check,
        "UdlReadClient",
        lambda settings, credentials, **options: UdlReadClient(
            settings, credentials, transport=fake.transport(), **options
        ),
    )
    out = tmp_path / "report.json"
    code = live_check.main(
        ["--skip-cold", "--objects", "43874", "--out", str(out), "--use-proxy"],
        ask=lambda prompt: "analyst-name",
        ask_secret=lambda prompt: "not-real-distinctive-pw",
        clock=lambda: NOW,
    )
    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert "not-real-distinctive-pw" not in text
    assert "analyst-name" not in text
    assert json.loads(text)["failures"] == []


def test_a_refused_elset_parameter_is_named_and_the_photometry_timing_survives() -> None:
    fake = _fake()
    fake.refuse_param["/udl/elset/tuple"] = "maxResults"
    args = live_check._arguments(["--years", "0.15", "--objects", "58204"])
    report = live_check.run(_factory(fake), args, NOW)
    variants = report.checks["elset_queries"]["variants"]
    assert variants["login_form"] == {"ok": True, "records": 1}
    assert variants["source_kbr"]["ok"] is True
    assert variants["full_columns"]["ok"] is True
    refused = variants["max_results_25000"]
    assert refused["ok"] is False
    assert refused["detail"] == "Invalid query parameter: maxResults"
    assert variants["app_query"]["ok"] is False
    assert report.checks["elsets_58204"]["detail"] == "Invalid query parameter: maxResults"
    cold = report.checks["cold_58204"]
    assert cold["passed"] is False
    assert cold["photometry"]["received"] > 0
    assert set(cold["errors"]) == {"elsets", "catalogue"}
    assert cold["errors"]["elsets"]["detail"] == "Invalid query parameter: maxResults"
    assert cold["elsets"] is None


def test_the_fields_check_reports_range_fill_and_passes_on_georange_alone() -> None:
    fake = _fake()
    for record in fake.history:
        record.pop("magNormRange")
        record["georange"] = 37_800.0
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    fields = report.checks["fields_43874"]
    assert fields["filled"]["magNormRange"] == 0.0
    assert fields["filled"]["georange"] == 1.0
    assert fields["filled"]["range"] == 1.0
    assert fields["passed"] is True


def test_unselectable_elset_columns_are_listed_and_refused_markings_named() -> None:
    fake = _fake()
    fake.refuse_column = {"/udl/elset/tuple": "bStar"}
    history = fake.history
    for record in history[::2]:
        record["classificationMarking"] = "U//OTHER-CONTROL"
    args = live_check._arguments(["--years", "0.15", "--objects", "58204"])
    report = live_check.run(_factory(fake), args, NOW)
    assert report.checks["elset_queries"]["unselectable_columns"] == ["bStar"]
    assert report.checks["elset_queries"]["passed"] is False
    cold = report.checks["cold_58204"]
    assert set(cold["refused_markings"]) == {"U//OTHER-CONTROL"}
    assert (
        cold["refused_markings"]["U//OTHER-CONTROL"] == cold["photometry"]["refused_classification"]
    )


def test_why_elsets_are_invalid_is_reported_without_any_values() -> None:
    fake = _fake()
    for record in fake.elsets:
        record["epoch"] = record["epoch"].replace("Z", "")
        record["inclination"] = str(record["inclination"])
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    elsets = report.checks["elsets_43874"]
    assert elsets["kbr_invalid_fields"] == {"epoch": 2}
    assert elsets["kbr_field_types"]["inclination"] == {"str": 2}
    assert elsets["kbr_field_types"]["bStar"] == {"float": 2}
    assert all(set(shape) <= set("9-T:.") for shape in elsets["kbr_epoch_shapes"])
    assert report.checks["geo_catalogue"]["invalid_fields"]["epoch"] > 100


def test_elsets_without_satno_pass_and_the_returned_fields_are_listed() -> None:
    fake = _fake()
    fake.withhold = {"satNo"}
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    elsets = report.checks["elsets_43874"]
    assert elsets["passed"] is True
    assert "satNo" not in elsets["returned_fields_by_source"]["KBR"]
    assert elsets["returned_fields_by_source"]["KBR"]["idOnOrbit"] == 2
    assert elsets["kbr_field_types"]["satNo"] == {"missing": 2}
    assert report.checks["geo_catalogue"]["passed"] is True


def test_the_sepa_check_reads_a_mirrored_convention_and_reports_sensor_fill() -> None:
    fake = _fake()
    _consistent_sepa(fake.history, sign=-1.0)
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    sepa = report.checks["sepa_sign"]
    assert sepa["passed"] is True
    assert sepa["from_longitude"]["verdict"] == "mirrored"
    assert sepa["from_line_of_sight"]["verdict"] == "sensor position not filled"
    assert sepa["geometry_filled"]["senlat"] == 0.0


def test_the_sepa_check_agrees_with_the_line_of_sight_method_when_sensors_are_known() -> None:
    fake = _fake()
    for record in fake.history:
        when = datetime.fromisoformat(record["obTime"]).timestamp() * 1000
        jd = sepa_check.julian_date(np.array([when]))
        lon = np.deg2rad(record["geolon"])
        fixed = np.array([[42_164.0 * np.cos(lon), 42_164.0 * np.sin(lon), 0.0]])
        satellite = sepa_check.ecef_to_inertial(fixed, sepa_check.gmst_radians(jd))
        site = sepa_check.ecef_to_inertial(
            sepa_check.geodetic_to_ecef(np.array([-31.0]), np.array([115.0]), np.array([0.1])),
            sepa_check.gmst_radians(jd),
        )
        sight = satellite - site
        distance = float(np.linalg.norm(sight))
        j2000 = np.linalg.solve(sepa_check.precession_matrix(jd)[0], sight[0] / distance)
        record.update(
            senlat=-31.0,
            senlon=115.0,
            senalt=0.1,
            range=distance,
            ra=float(np.rad2deg(np.arctan2(j2000[1], j2000[0])) % 360.0),
            declination=float(np.rad2deg(np.arcsin(j2000[2]))),
            solarEqPhaseAngle=float(
                sepa_check.sepa_degrees(satellite, sepa_check.sun_position_km(jd), site)[0]
            ),
        )
    args = live_check._arguments(["--skip-cold", "--objects", "43874"])
    report = live_check.run(_factory(fake), args, NOW)
    sepa = report.checks["sepa_sign"]
    assert sepa["from_line_of_sight"]["verdict"] == "same convention"
    assert sepa["from_line_of_sight"]["median_abs_difference_deg"] < 0.01
    assert sepa["from_longitude"]["verdict"] == "same convention"


def test_sepa_comparison_says_when_it_cannot_tell() -> None:
    assert sepa_check.compare(np.array([1.0, np.nan]), np.array([1.0, 2.0]))["compared"] == 0
    noisy = sepa_check.compare(
        np.array([30.0, -30.0, 40.0, -40.0]), np.array([30.0, 30.0, -40.0, -40.0])
    )
    assert noisy["verdict"] == "unclear"


def test_the_check_loop_never_chains_commands_with_and() -> None:
    """`set -e` ignores a failure on the left of `&&`, which once passed a lint failure."""
    script = Path(__file__).resolve().parents[1] / "check.sh"
    commands = [
        line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("&&" in line for line in commands)
