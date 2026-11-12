"""End-to-end reads against the fake UDL, and the run-time proof that Light Bee only reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from lightbee.config import Settings
from lightbee.udl.client import ALLOWED_PATHS, Credentials, UdlReadClient
from lightbee.udl.photometry import PhotometryChunk, chunk_from_records, combine
from lightbee.udl.queries import (
    ELSET_COLUMNS,
    LIVE_OVERLAP,
    OBSERVATION_COLUMNS,
    read_elsets,
    read_geo_catalogue,
    read_observations,
)
from lightbee.udl.records import Tally
from tests.fake_udl import FakeUdl, dense_history, elset_record, iso, observation

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SAT = 43874


def _client(fake: FakeUdl) -> UdlReadClient:
    return UdlReadClient(
        Settings(),
        Credentials("analyst", "not-real-pw"),
        transport=fake.transport(),
        sleep=lambda _: None,
    )


def test_two_years_of_photometry_are_read_whole_as_compact_columns() -> None:
    start = NOW - timedelta(days=730)
    history = dense_history(SAT, start, days=730, per_day=40)
    live_from = iso(NOW - LIVE_OVERLAP)
    live = [r for r in history if r["obTime"] >= live_from]
    fresh = [observation(SAT, NOW - timedelta(minutes=5), 999_999)]
    fake = FakeUdl(history=history, live=live + fresh)
    progress: list[tuple[int, int, int]] = []
    result = read_observations(
        _client(fake), SAT, (start, NOW), now=NOW, on_progress=lambda *a: progress.append(a)
    )
    assert len(result) == len(history) + 1
    assert result.tally.duplicates == len(live)
    assert np.all(np.diff(result.epoch_ms) >= 0)
    assert result.mag_norm.dtype == np.float32
    assert result.sensors == ("EX00", "EX01", "EX02", "EX03")
    assert result.nbytes < 30 * len(result)
    assert progress
    for request in fake.requests:
        assert request.url.params["columns"] == ",".join(OBSERVATION_COLUMNS)
        assert request.url.params["dataMode"] == "REAL"
        assert request.url.params["satNo"] == str(SAT)


def test_classified_and_invalid_records_are_refused_and_counted() -> None:
    when = NOW - timedelta(days=10)
    fake = FakeUdl(
        history=[
            observation(SAT, when, 1),
            observation(SAT, when + timedelta(minutes=1), 2, classificationMarking="S"),
            observation(SAT, when + timedelta(minutes=2), 3, classificationMarking=None),
            observation(SAT, when + timedelta(minutes=3), 4, mag=None, magNormRange=None),
        ]
    )
    result = read_observations(
        _client(fake), SAT, (NOW - timedelta(days=30), NOW - timedelta(days=5)), now=NOW
    )
    assert result.tally.as_dict() == {
        "received": 4,
        "kept": 1,
        "refused_classification": 2,
        "invalid": 1,
        "duplicates": 0,
    }


def test_accepted_controls_are_kept_and_carried_for_the_banner() -> None:
    when = NOW - timedelta(days=10)
    fake = FakeUdl(
        history=[
            observation(SAT, when, 1, classificationMarking="U//PR-MRKT"),
            observation(SAT, when + timedelta(minutes=1), 2),
            observation(SAT, when + timedelta(minutes=2), 3, classificationMarking="U//FOUO"),
        ],
        elsets=[elset_record(SAT, NOW - timedelta(days=1), classificationMarking="U//PR-MRKT")],
    )
    window = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    photometry = read_observations(_client(fake), SAT, window, now=NOW)
    assert len(photometry) == 2
    assert photometry.tally.refused_classification == 1
    assert photometry.tally.banner() == "UNCLASSIFIED//PR-MRKT"
    elsets = read_elsets(_client(fake), SAT, (NOW - timedelta(days=2), NOW))
    assert elsets.tally.kept == 1
    assert elsets.tally.banner() == "UNCLASSIFIED//PR-MRKT"


def test_magnitudes_are_normalised_only_when_nearly_all_have_it() -> None:
    when = NOW - timedelta(days=10)
    rows = [observation(SAT, when + timedelta(minutes=k), k, mag=12.0) for k in range(10)]
    fake = FakeUdl(history=rows)
    window = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    values, basis = read_observations(_client(fake), SAT, window, now=NOW).magnitudes()
    assert basis == "range-normalised"
    assert values.tolist() == pytest.approx([11.2] * 10)
    for row in rows[:2]:
        row["magNormRange"] = None
    values, basis = read_observations(_client(fake), SAT, window, now=NOW).magnitudes()
    assert basis == "raw"
    assert values.tolist() == pytest.approx([12.0] * 10)
    assert read_observations(_client(FakeUdl()), SAT, window, now=NOW).magnitudes()[1] == "raw"


def test_records_without_their_marking_are_all_refused() -> None:
    """If the service ever omitted the marking, nothing would be shown: fail closed."""
    when = NOW - timedelta(days=10)
    fake = FakeUdl(history=[observation(SAT, when + timedelta(minutes=k), k) for k in range(5)])
    for record in fake.history:
        del record["classificationMarking"]
    result = read_observations(
        _client(fake), SAT, (NOW - timedelta(days=30), NOW - timedelta(days=5)), now=NOW
    )
    assert len(result) == 0
    assert result.tally.refused_classification == 5


def test_an_old_window_does_not_touch_the_live_store() -> None:
    fake = FakeUdl(history=dense_history(SAT, NOW - timedelta(days=100), days=10, per_day=5))
    read_observations(
        _client(fake), SAT, (NOW - timedelta(days=100), NOW - timedelta(days=90)), now=NOW
    )
    assert {request.url.path for request in fake.requests} == {"/udl/eoobservation/history"}


def test_naive_times_and_bad_catalogue_numbers_are_refused() -> None:
    client = _client(FakeUdl())
    with pytest.raises(ValueError, match="timezone-aware"):
        read_observations(client, SAT, (datetime(2026, 1, 1), NOW), now=NOW)  # noqa: DTZ001 - a naive date, refused
    for bad in (0, 100_000, True):
        with pytest.raises(ValueError, match="catalogue number"):
            read_observations(client, bad, (NOW - timedelta(days=1), NOW), now=NOW)
        with pytest.raises(ValueError, match="catalogue number"):
            read_elsets(client, bad, (NOW - timedelta(days=1), NOW))


def test_combining_no_chunks_gives_an_empty_set() -> None:
    empty = combine([])
    assert len(empty) == 0
    assert empty.sensors == ()
    assert combine([chunk_from_records([], SAT)]).tally.kept == 0


def test_combining_remaps_sensors_across_chunks() -> None:
    when = NOW - timedelta(days=1)
    first = chunk_from_records([observation(SAT, when, 1, idSensor="B")], SAT)
    later = when + timedelta(seconds=1)
    second = chunk_from_records([observation(SAT, later, 2, idSensor="A")], SAT)
    merged = combine([first, second])
    assert [merged.sensors[code] for code in merged.sensor] == ["B", "A"]
    assert isinstance(first, PhotometryChunk)
    assert isinstance(merged.tally, Tally)


def test_elsets_are_read_for_one_object_and_one_provider() -> None:
    epochs = [NOW - timedelta(hours=6 * k) for k in range(40)]
    fake = FakeUdl(
        elsets=[elset_record(SAT, e) for e in epochs]
        + [
            elset_record(SAT, epochs[0], source="OTHER"),
            elset_record(SAT, epochs[1], classificationMarking="S"),
            elset_record(SAT, epochs[2], ephemType=4),
            elset_record(SAT, epochs[3], dataMode="TEST"),
        ]
    )
    window = (NOW - timedelta(days=10), NOW + timedelta(minutes=1))
    result = read_elsets(_client(fake), SAT, window)
    assert result.tally.kept == 40
    # The record marked S is refused and its marking counted; the SGP4-XP elset
    # is invalid; the other provider's and the TEST record are never returned.
    assert result.tally.refused_classification == 1
    assert result.tally.refused_markings == {"S": 1}
    assert result.tally.invalid == 1
    assert result.tally.invalid_fields == {"ephemType": 1}
    assert [e.epoch for e in result.elsets] == sorted(epochs)
    assert all(e.source == "KBR" for e in result.elsets)
    for request in fake.requests:
        assert request.url.params["columns"] == ",".join(ELSET_COLUMNS)
        assert request.url.params["dataMode"] == "REAL"
        assert "line1" not in request.url.params["columns"]


def test_elsets_returned_without_satno_are_still_read() -> None:
    fake = FakeUdl(
        elsets=[elset_record(SAT, NOW - timedelta(hours=k)) for k in range(1, 4)]
        + [elset_record(40000 + k, NOW - timedelta(hours=2)) for k in range(5)]
        + [elset_record(40099, NOW - timedelta(hours=2), idOnOrbit="not-a-number")],
        withhold={"satNo"},
    )
    result = read_elsets(_client(fake), SAT, (NOW - timedelta(days=1), NOW))
    assert result.tally.kept == 3
    assert {e.sat_no for e in result.elsets} == {SAT}
    catalogue, tally = read_geo_catalogue(_client(fake), NOW)
    assert set(catalogue) == {SAT, *(40000 + k for k in range(5))}
    assert tally.invalid_fields == {"satNo": 1}
    columns = fake.requests[-1].url.params["columns"].split(",")
    assert "idOnOrbit" in columns


def test_the_geo_catalogue_keeps_the_nearest_elset_per_geo_object() -> None:
    fake = FakeUdl(
        elsets=[
            elset_record(40001, NOW - timedelta(hours=5)),
            elset_record(40001, NOW - timedelta(hours=1)),
            elset_record(40001, NOW + timedelta(hours=4)),  # later, but farther away
            elset_record(40002, NOW + timedelta(hours=2)),
            elset_record(25544, NOW, meanMotion=15.5),  # low Earth orbit, not GEO
            elset_record(40003, NOW - timedelta(hours=20)),  # outside the 12-hour span
        ]
    )
    catalogue, tally = read_geo_catalogue(_client(fake), NOW)
    assert set(catalogue) == {40001, 40002}
    assert catalogue[40001].epoch == NOW - timedelta(hours=1)
    assert tally.kept == 5


def test_a_full_run_only_ever_reads() -> None:
    """The binding read-only control: inspect every request a complete set of reads makes."""
    start = NOW - timedelta(days=60)
    fake = FakeUdl(
        history=dense_history(SAT, start, days=60, per_day=50),
        live=dense_history(SAT, NOW - timedelta(days=2), days=2, per_day=50),
        elsets=[elset_record(SAT, NOW - timedelta(hours=h)) for h in range(0, 48, 6)]
        + [elset_record(40001, NOW)],
        fail_once={"/udl/eoobservation/history": 503},
    )
    with _client(fake) as client:
        read_observations(client, SAT, (start, NOW), now=NOW)
        read_elsets(client, SAT, (NOW - timedelta(days=7), NOW))
        read_geo_catalogue(client, NOW)
    paths = {request.url.path for request in fake.requests}
    assert paths == {"/udl/eoobservation/history", "/udl/eoobservation/tuple", "/udl/elset/tuple"}
    for request in fake.requests:
        assert request.method == "GET"
        assert request.content == b""
        assert request.url.scheme == "https"
        assert request.url.host == "unifieddatalibrary.com"
        assert request.url.path in ALLOWED_PATHS
        assert request.url.port in (None, 443)


@pytest.mark.no_cover  # coverage tracing makes the memory measurement slow, not wrong
def test_raw_records_never_accumulate_while_reading() -> None:
    """Peak memory tracks the slices in flight, not the whole history."""
    import tracemalloc

    from lightbee.udl.client import Throttle

    start = NOW - timedelta(days=64)
    fake = FakeUdl(history=dense_history(SAT, start, days=64, per_day=1000))
    client = UdlReadClient(
        Settings(),
        Credentials("analyst", "not-real-pw"),
        transport=fake.transport(),
        throttle=Throttle(2),
        sleep=lambda _: None,
    )
    tracemalloc.start()
    try:
        result = read_observations(client, SAT, (start, NOW - timedelta(days=4)), now=NOW)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result) == 60_000
    # Holding every raw record at once was measured at about 46 MB here.
    assert peak < 20 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"


def test_an_exact_duplicate_is_found_even_when_a_near_twin_sorts_between() -> None:
    when = NOW - timedelta(days=1)
    a = observation(SAT, when, 1, solarEqPhaseAngle=10.5, geolon=100.0)
    b = observation(SAT, when, 2, solarEqPhaseAngle=10.5, geolon=101.0)
    history = chunk_from_records([a, b], SAT)
    live = chunk_from_records([dict(a)], SAT)
    merged = combine([history, live])
    assert len(merged) == 2
    assert merged.tally.duplicates == 1


def test_an_elset_epoch_outside_the_requested_window_is_refused() -> None:
    from lightbee.udl.queries import _validated_elsets

    window = (NOW - timedelta(days=1), NOW)
    raw = [
        elset_record(SAT, NOW - timedelta(hours=1)),
        elset_record(SAT, datetime(9999, 12, 31, tzinfo=UTC)),
        elset_record(SAT, NOW - timedelta(days=3)),
        elset_record(SAT, NOW - timedelta(hours=2), source="OTHER"),
    ]
    result = _validated_elsets(raw, "KBR", SAT, window)
    assert result.tally.kept == 1
    assert result.tally.invalid_fields == {"epoch": 2, "source": 1}


def _named(sat_no: object, name: object, marking: object = "U") -> dict[str, object]:
    return {"satNo": sat_no, "commonName": name, "classificationMarking": marking}


def test_names_are_read_in_batches_validated_and_their_controls_carried() -> None:
    from lightbee.udl.queries import NAME_BATCH, ONORBIT, read_names

    wanted = list(range(40000, 40000 + NAME_BATCH + 5))
    onorbit = [_named(n, f"GEO {n}") for n in wanted[:-3]]
    onorbit[0] = _named(40000, "TJS-3", "U//PR-MRKT")
    onorbit[1] = _named(40001, "SECRET SAT", "S")  # refused for its marking
    onorbit[2] = _named(40002, "<script>")  # a name outside the allowed characters
    onorbit.append(_named(40003, "SECOND RECORD"))  # a duplicate number
    onorbit.append(_named("40004", "TEXT NUMBER"))  # a number written as text: kept once
    fake = FakeUdl(onorbit=onorbit)
    with _client(fake) as client:
        names, tally = read_names(client, wanted)
    assert names[40000] == "TJS-3"
    assert 40001 not in names
    assert 40002 not in names
    assert names[40003] == "GEO 40003"
    assert wanted[-1] not in names  # no record: no name, no error
    assert tally.refused_classification == 1
    assert tally.invalid_fields["commonName"] == 1
    assert tally.duplicates == 2
    assert tally.controls == {"PR-MRKT"}
    # Two reads for 105 numbers, each a GET to the on-orbit tuple endpoint with a comma list.
    assert [r.url.path for r in fake.requests] == [ONORBIT, ONORBIT]
    assert all(r.method == "GET" for r in fake.requests)
    first = dict(fake.requests[0].url.params)
    assert first["columns"] == "classificationMarking,satNo,commonName"
    assert first["satNo"].count(",") == NAME_BATCH - 1


def test_a_name_for_a_number_not_asked_for_is_not_used() -> None:
    from lightbee.udl.queries import _take_name

    names: dict[int, str] = {}
    tally = Tally()
    _take_name(_named(40009, "STRAY"), {40001}, names, tally)
    assert names == {}
    assert tally.invalid_fields["satNo"] == 1


def test_a_cancelled_run_stops_asking_for_names() -> None:
    from lightbee.udl.queries import read_names

    class CancelledError(Exception):
        pass

    def cancel() -> None:
        raise CancelledError

    fake = FakeUdl(onorbit=[_named(n, f"GEO {n}") for n in range(1, 301)])
    with _client(fake) as client, pytest.raises(CancelledError):
        read_names(client, range(1, 301), check=cancel)
    assert fake.requests == []


@pytest.mark.parametrize("bad", [5.5, "5", True, 0, 100_000])
def test_only_whole_catalogue_numbers_reach_the_names_query(bad: object) -> None:
    from lightbee.udl.queries import read_names

    with _client(FakeUdl()) as client, pytest.raises(ValueError, match="catalogue number"):
        read_names(client, [bad])  # type: ignore[list-item]
