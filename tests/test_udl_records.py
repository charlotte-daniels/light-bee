"""Boundary validation of UDL records: fail closed, refuse anything not UNCLASSIFIED."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest

from lightbee.udl.records import (
    Elset,
    Tally,
    bounded,
    controls_of,
    elset,
    elset_or_problem,
    is_unclassified,
    observation,
    parse_time,
    sat_no_of,
)
from tests.fake_udl import elset_record
from tests.fake_udl import observation as make_observation

WHEN = datetime(2026, 11, 1, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "marking", ["U", "u", " UNCLASSIFIED ", "Unclassified", "U//PR-MRKT", "UNCLASSIFIED//pr-mrkt"]
)
def test_unclassified_markings_pass(marking: str) -> None:
    assert is_unclassified({"classificationMarking": marking})


def test_accepted_controls_are_returned_and_name_the_banner() -> None:
    assert controls_of({"classificationMarking": "U"}) == frozenset()
    assert controls_of({"classificationMarking": "U//PR-MRKT"}) == {"PR-MRKT"}
    for control in ("PR-EXO-OBS", "DS-NSFCTC-OBS", "PR-KBR-ELSETS"):
        assert controls_of({"classificationMarking": f"U//{control}"}) == {control}
    assert controls_of({"classificationMarking": "U//PR-EXO-OBS/PR-MRKT"}) == {
        "PR-EXO-OBS",
        "PR-MRKT",
    }
    assert controls_of({"classificationMarking": "U//PR-NORTHSTAR"}) is None
    tally = Tally()
    assert tally.banner() == "UNCLASSIFIED"
    other = Tally(controls={"PR-MRKT"})
    tally.add(other)
    assert tally.banner() == "UNCLASSIFIED//PR-MRKT"
    tally.add(Tally(controls={"PR-EXO-OBS", "DS-NSFCTC-OBS"}))
    assert tally.banner() == "UNCLASSIFIED//DS-NSFCTC-OBS/PR-EXO-OBS/PR-MRKT"


@pytest.mark.parametrize(
    "marking",
    [
        None,
        "",
        "S",
        "SECRET",
        "U//FOUO",
        "CUI",
        "C",
        "TS//SCI",
        0,
        ["U"],
        "S//PR-MRKT",
        "U//",
        "U//PR-MRKT/",
        "U//PR-MRKT/FOUO",
        "U//PR-MRKT//REL TO USA",
        "PR-MRKT",
    ],
)
def test_every_other_marking_or_none_is_refused(marking: object) -> None:
    assert not is_unclassified({"classificationMarking": marking})


def test_timestamps_are_parsed_strictly_as_utc() -> None:
    assert parse_time("2026-11-01T03:04:05.000Z") == WHEN
    assert parse_time("2026-11-01T03:04:05Z") == WHEN
    for bad in (
        "2026-11-01 03:04:05",
        "2026-11-01T03:04:05+01:00",
        "2026-13-01T00:00:00Z",
        5,
        None,
    ):
        assert parse_time(bad) is None


def test_numbers_must_be_finite_real_and_in_range() -> None:
    assert bounded(11.5, (0, 20)) == 11.5
    assert bounded(3, (0, 20)) == 3.0
    for bad in (True, "11.5", None, math.inf, math.nan, 25.0):
        assert math.isnan(bounded(bad, (0, 20)))


def test_a_good_observation_is_kept_with_its_fields() -> None:
    item = observation(make_observation(43874, WHEN, 7, geolon=270.0), 43874)
    assert item is not None
    assert item.epoch_ms == int(WHEN.timestamp() * 1000)
    assert item.mag_norm == 11.2
    assert item.geolon == -90.0
    assert item.sensor == "EX03"


@pytest.mark.parametrize(
    "overrides",
    [
        {"obTime": "yesterday"},
        {"dataMode": "TEST"},
        {"dataMode": "SIMULATED"},
        {"satNo": 99999},
        {"satNo": True},
        {"satNo": "43874"},
        {"mag": None, "magNormRange": None},
        {"mag": "bright", "magNormRange": 99.0},
    ],
)
def test_bad_observations_are_dropped(overrides: dict[str, Any]) -> None:
    assert observation(make_observation(43874, WHEN, 1, **overrides), 43874) is None


def test_an_empty_magnormrange_is_computed_from_georange_as_the_udl_defines_it() -> None:
    item = observation(
        make_observation(43874, WHEN, 1, mag=12.0, magNormRange=None, georange=38_000.0), 43874
    )
    assert item is not None
    assert item.mag_norm == pytest.approx(12.0 - 5.0 * math.log10(0.038))


@pytest.mark.parametrize("georange", [None, 0.0, -5.0, "38000", 5e6, math.inf])
def test_without_a_usable_georange_the_normalised_magnitude_stays_empty(georange: object) -> None:
    item = observation(
        make_observation(43874, WHEN, 1, mag=12.0, magNormRange=None, georange=georange), 43874
    )
    assert item is not None
    assert item.mag == 12.0
    assert math.isnan(item.mag_norm)


def test_missing_optional_fields_become_nan_or_unknown() -> None:
    item = observation(
        make_observation(43874, WHEN, 1, solarEqPhaseAngle=None, geolon=None, idSensor="<script>"),
        43874,
    )
    assert item is not None
    assert math.isnan(item.sepa)
    assert math.isnan(item.geolon)
    assert item.sensor == "unknown"


def test_catalogue_numbers_are_five_digit_integers_only() -> None:
    assert sat_no_of(43874) == 43874
    for bad in (0, 100_000, True, "43874", 4.5, None):
        assert sat_no_of(bad) is None


def test_a_good_elset_is_kept_and_bad_ones_dropped() -> None:
    good = elset(elset_record(43874, WHEN))
    assert good is not None
    assert good.source == "KBR"
    assert good.mean_motion == pytest.approx(1.0027)
    assert (good.arg_of_perigee, good.mean_anomaly, good.b_star) == (90.0, 270.0, 0.0)
    for overrides in (
        {"epoch": "soon"},
        {"satNo": "43874"},
        {"satNo": True},
        {"source": "KBR; DROP"},
        {"dataMode": "TEST"},
        {"dataMode": None},
        {"ephemType": 4},
        {"ephemType": None},
        {"ephemType": False},
        {"ephemType": "0"},
        {"inclination": 200.0},
        {"eccentricity": 1.0},
        {"meanAnomaly": None},
        {"bStar": "small"},
        {"period": "1436"},
    ):
        assert elset(elset_record(43874, WHEN, **overrides)) is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"satNo": None}, "satNo"),
        ({"epoch": "2026-11-01T03:04:05"}, "epoch"),
        ({"dataMode": "TEST"}, "dataMode"),
        ({"source": None}, "source"),
        ({"ephemType": 2}, "ephemType"),
        ({"meanMotion": "1.0027"}, "meanMotion"),
        ({"meanMotion": 0.0}, "meanMotion"),
        ({"eccentricity": None}, "eccentricity"),
        ({"bStar": 5.0}, "bStar"),
        ({"meanMotionDDot": "0"}, "meanMotionDDot"),
        ({"period": -1.0}, "period"),
    ],
)
def test_an_invalid_elset_names_the_field_that_failed(
    overrides: dict[str, Any], field: str
) -> None:
    assert elset_or_problem(elset_record(43874, WHEN, **overrides)) == field


def test_an_elset_without_satno_takes_the_object_the_read_asked_for() -> None:
    record = elset_record(43874, WHEN)
    del record["satNo"]
    del record["idOnOrbit"]
    item = elset_or_problem(record, 43874)
    assert isinstance(item, Elset)
    assert item.sat_no == 43874
    assert elset_or_problem(record) == "satNo"


@pytest.mark.parametrize(
    ("id_on_orbit", "expected"),
    [("40001", 40001), ("4e6a1f2c-uuid", None), ("123456", None), ("0", None), (40001, None)],
)
def test_a_catalogue_elset_without_satno_is_identified_by_id_on_orbit(
    id_on_orbit: object, expected: int | None
) -> None:
    record = elset_record(40001, WHEN, idOnOrbit=id_on_orbit)
    del record["satNo"]
    item = elset_or_problem(record)
    assert (item.sat_no if isinstance(item, Elset) else None) == expected


def test_a_satno_that_is_present_must_be_valid_and_agree() -> None:
    assert elset_or_problem(elset_record(43874, WHEN), 43875) == "satNo"
    assert elset_or_problem(elset_record(43874, WHEN, satNo="43874"), 43874) == "satNo"
    assert elset_or_problem(elset_record(43874, WHEN, satNo=None, idOnOrbit="43874")) == "satNo"


def test_absent_drag_terms_are_zero_and_an_absent_period_is_derived() -> None:
    record = elset_record(43874, WHEN)
    for name in ("bStar", "meanMotionDot", "meanMotionDDot", "period"):
        del record[name]
    item = elset(record)
    assert item is not None
    assert (item.b_star, item.mean_motion_dot, item.mean_motion_ddot) == (0.0, 0.0, 0.0)
    assert item.period == pytest.approx(1440.0 / 1.0027)


@pytest.mark.parametrize("mean_motion", [5e-324, 1e-300, 1e-9])
def test_a_derived_period_is_bounded_like_a_supplied_one(mean_motion: float) -> None:
    record = elset_record(43874, WHEN, meanMotion=mean_motion)
    del record["period"]
    # 1440 / 5e-324 is infinite and 1440 / 1e-9 is over the 1e6 minute bound.
    assert elset_or_problem(record) == "period"
    record["period"] = 1436.0
    item = elset_or_problem(record)
    assert isinstance(item, Elset)
    assert item.period == 1436.0


def test_refused_markings_are_counted_under_short_safe_labels() -> None:
    tally = Tally()
    for marking in ("S", "S", None, 7, "  ", "U//X\x00\x1b[31m" + "y" * 100):
        tally.refuse(marking)
    assert tally.refused_classification == 6
    assert tally.refused_markings["S"] == 2
    assert tally.refused_markings["(none)"] == 1
    assert tally.refused_markings["(not text)"] == 1
    assert tally.refused_markings["(empty)"] == 1
    long_label = next(label for label in tally.refused_markings if label.startswith("U//X"))
    assert len(long_label) <= 40
    assert all(" " <= c <= "~" for c in long_label)


def test_refused_labels_are_bounded_however_many_distinct_markings_arrive() -> None:
    tally = Tally()
    for k in range(100):
        tally.refuse(f"X{k}")
    other = Tally()
    other.refuse("NEW")
    tally.add(other)
    assert len(tally.refused_markings) == 17
    assert tally.refused_markings["(other)"] == 100 - 16 + 1
    assert sum(tally.refused_markings.values()) == tally.refused_classification


def test_the_tally_reports_and_adds_every_count() -> None:
    tally = Tally(received=5, kept=3, refused_classification=1, invalid=1)
    tally.add(Tally(received=2, kept=1, duplicates=1))
    assert tally.as_dict() == {
        "received": 7,
        "kept": 4,
        "refused_classification": 1,
        "invalid": 1,
        "duplicates": 1,
    }


def test_every_identity_key_present_must_parse_and_agree() -> None:
    def identity(expected: int | None = None, **keys: object) -> object:
        record = elset_record(40001, WHEN)
        del record["satNo"], record["idOnOrbit"]
        record.update(keys)
        item = elset_or_problem(record, expected)
        return item.sat_no if isinstance(item, Elset) else item

    assert identity(**{"onOrbit.satNo": 40001}) == 40001
    assert identity(**{"onOrbit.satNo": 40001, "idOnOrbit": "40001"}) == 40001
    assert identity(40001) == 40001  # no key at all: the number the read asked for
    for bad in (0, 123456, "40001", True, None):
        assert identity(40001, **{"onOrbit.satNo": bad}) == "satNo"
    assert identity(**{"onOrbit.satNo": 40001, "idOnOrbit": "40002"}) == "satNo"
    assert identity(**{"onOrbit.satNo": 40001, "idOnOrbit": "a-uuid"}) == "satNo"
    assert identity(40002, **{"onOrbit.satNo": 40001}) == "satNo"
    assert identity(**{"satNo": 40001, "onOrbit.satNo": 40002}) == "satNo"


@pytest.mark.parametrize(
    "marking",
    [
        "UNCLA\u017f\u017fIFIED",
        "UNCLA\u00dfIFIED",
        "UNCLASS\u0131F\u0131ED",
        "\u3000U\u3000",
        "U\u2028",
        "U\t//PR-MRKT\n",
    ],
)
def test_non_ascii_or_unusual_whitespace_markings_are_refused(marking: str) -> None:
    assert not is_unclassified({"classificationMarking": marking})


def test_the_udl_sepa_is_flipped_to_the_reference_orientation() -> None:
    """Measured live: the UDL's sign is the mirror of the predecessor's plots."""
    item = observation(make_observation(43874, WHEN, 1, solarEqPhaseAngle=30.0), 43874)
    assert item is not None
    assert item.sepa == -30.0


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"satNo": 43874, "commonName": "TJS-3"}, (43874, "TJS-3")),
        ({"satNo": "43874", "commonName": "  INTELSAT   901 "}, (43874, "INTELSAT 901")),
        ({"satNo": 25544, "commonName": "ISS (ZARYA)"}, (25544, "ISS (ZARYA)")),
        (
            {"satNo": 1, "commonName": "SL-3 R/B [DEB] #2, A&B: 'X'"},
            (1, "SL-3 R/B [DEB] #2, A&B: 'X'"),
        ),
        ({"satNo": 43874, "commonName": ""}, None),
        ({"satNo": 43874, "commonName": "<b>X</b>"}, None),
        ({"satNo": 43874, "commonName": "TJS" + chr(0x2011) + "3"}, None),  # non-ASCII hyphen
        ({"satNo": 43874, "commonName": "X" * 41}, None),
        ({"satNo": 43874, "commonName": 7}, None),
        ({"satNo": 43874}, None),
        ({"satNo": True, "commonName": "TJS-3"}, None),
        ({"satNo": "4387a", "commonName": "TJS-3"}, None),
        ({"satNo": 0, "commonName": "TJS-3"}, None),
    ],
)
def test_a_satellite_name_is_shown_exactly_or_not_at_all(
    record: dict[str, Any], expected: tuple[int, str] | None
) -> None:
    from lightbee.udl.records import satellite_name

    assert satellite_name(record) == expected
