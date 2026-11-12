"""The service end to end: sign-in, runs, figures, the flag and the scan, on a fake UDL."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lightbee.config import Settings
from lightbee.web import api as api_module
from lightbee.web import limits
from lightbee.web.app import create_app
from lightbee.web.session import COOKIE_NAME
from tests.fake_udl import FakeUdl
from tests.fake_world import NEIGHBOUR, NOW, PASSWORD, TARGET, USERNAME, factory, world

HEADERS = {"X-Light-Bee": "1"}
RUN = {"sat_no": TARGET, "change_date": "2026-11-10", "window_days": 7}


class Deferred:
    """A background runner the test releases by hand."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        self.jobs.append(job)

    def release(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


def make_client(
    fake: FakeUdl,
    submit: Callable[[Callable[[], None]], None] | None = None,
    settings: Settings | None = None,
) -> TestClient:
    app = create_app(
        settings or Settings(),
        client_factory=factory(fake),
        submit=submit or (lambda job: job()),
        clock=lambda: NOW,
    )
    return TestClient(app, base_url="https://testserver")


def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/session", json={"username": USERNAME, "password": PASSWORD}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture(scope="module")
def fake() -> FakeUdl:
    return world()


@pytest.fixture
def client(fake: FakeUdl) -> Iterator[TestClient]:
    with make_client(fake) as test_client:
        yield signed_in(test_client)


@pytest.fixture(scope="module")
def finished(fake: FakeUdl) -> Iterator[tuple[TestClient, str]]:
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        yield test_client, run["id"]


def test_sign_in_sets_a_strict_host_cookie_and_sign_out_ends_the_session(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        assert test_client.get("/api/session").json()["signed_in"] is False
        response = test_client.post(
            "/api/session", json={"username": USERNAME, "password": PASSWORD}, headers=HEADERS
        )
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            assert attribute.lower() in cookie.lower()
        assert "domain" not in cookie.lower()
        assert test_client.get("/api/session").json()["signed_in"] is True
        test_client.delete("/api/session", headers=HEADERS)
        assert test_client.get("/api/session").json()["signed_in"] is False
        assert test_client.post("/api/runs", json=RUN, headers=HEADERS).status_code == 401


def test_wrong_credentials_and_missing_headers_are_refused(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        wrong = {"username": USERNAME, "password": PASSWORD + "-wrong"}
        assert test_client.post("/api/session", json=wrong, headers=HEADERS).status_code == 401
        right = {"username": USERNAME, "password": PASSWORD}
        assert test_client.post("/api/session", json=right).status_code == 403
        assert test_client.get("/api/session").json()["signed_in"] is False
        signed_in(test_client)
        assert test_client.post("/api/runs", json=RUN).status_code == 403
        assert test_client.delete("/api/session").status_code == 403


def test_a_full_run_produces_both_figures_the_flag_and_markings(
    finished: tuple[TestClient, str], fake: FakeUdl
) -> None:
    client, run_id = finished
    state = client.get(f"/api/runs/{run_id}").json()
    assert state["stage"] == "done", state
    assert {"recent", "seasonal", "elsets", "approaches"} <= set(state["ready"])
    seasonal = client.get(f"/api/runs/{run_id}/figures/seasonal").json()
    kinds = [trace["type"] for trace in seasonal["data"]]
    assert kinds == ["scatter", "heatmap", "scatter"]  # recent panel, heatmap, longitude
    texts = " ".join(a["text"] for a in seasonal["layout"]["annotations"])
    assert "UNCLASSIFIED//PR-KBR-ELSETS/PR-MRKT" in texts
    assert "UNDER TEST AND EVALUATION" in texts
    assert "NORAD 43874" in texts
    causes = client.get(f"/api/runs/{run_id}/figures/causes").json()
    names = [trace.get("name", "") for trace in causes["data"]]
    assert "Day 0" in names
    assert any(name.startswith("NORAD ") for name in names)
    raw = client.get(f"/api/runs/{run_id}/figures/causes?basis=raw").json()
    assert "raw magnitude" in json.dumps(raw["layout"]["annotations"])
    # Ghost years: off unless asked for, then drawn from the history in memory.
    assert "1 year ago" not in names
    assert causes["ghosts"] is False
    calls = len(fake.requests)
    ghosts = client.get(f"/api/runs/{run_id}/figures/causes?ghosts=true&basis=raw").json()
    assert len(fake.requests) == calls  # nothing re-read from the UDL
    ghost_names = [t["name"] for t in ghosts["data"] if t.get("legendgroup") == "ghost"]
    assert ghost_names == ["1 year ago", "2 years ago"]
    assert ghosts["ghosts"] is True
    assert "years ago beneath" in json.dumps(ghosts["layout"]["annotations"])
    assert "raw magnitude" in json.dumps(ghosts["layout"]["annotations"])
    assert "Beneath the light curve, the same week 1 year ago" in ghosts["summary"]
    assert client.get(f"/api/runs/{run_id}/figures/causes?ghosts=maybe").status_code == 422
    flag = client.get(f"/api/runs/{run_id}/flag").json()
    assert flag["outcome"] in {"consistent", "marginal", "offset", "departs", "inconclusive"}
    assert flag["comparable_bins"] == len(flag["bins"]["sepa_centre"])


def test_a_selection_finds_the_neighbour_at_every_selected_time(
    finished: tuple[TestClient, str],
) -> None:
    client, run_id = finished
    start = int(NOW.timestamp() * 1000) - 2 * 86_400_000
    times = [start + k * 60_000 for k in range(120)] + [
        start + 86_400_000 + k * 60_000 for k in range(60)
    ]
    body = client.post(f"/api/runs/{run_id}/scan", json={"times_ms": times}, headers=HEADERS).json()
    assert len(body["times_ms"]) == 180
    first = body["neighbours"][0]
    assert first["sat_no"] == NEIGHBOUR
    assert 90.0 < first["min_km"] < 110.0
    assert first["inside"] is True
    assert len(first["separations_km"]) == 180


def test_a_selection_outside_the_window_or_malformed_is_refused(
    finished: tuple[TestClient, str],
) -> None:
    client, run_id = finished
    old = int(NOW.timestamp() * 1000) - 30 * 86_400_000
    path = f"/api/runs/{run_id}/scan"
    assert client.post(path, json={"times_ms": [old]}, headers=HEADERS).status_code == 400
    for body in (
        {"times_ms": []},
        {"times_ms": ["1"]},
        {"times_ms": [True]},
        {"times_ms": [1] * 50_001},
        {"times_ms": [old], "threshold_km": 0.5},
        {"times_ms": [old], "extra": 1},
    ):
        response = client.post(path, json=body, headers=HEADERS)
        assert response.status_code == 422, body
        assert "1" * 5 not in response.text  # values are never echoed


@pytest.mark.parametrize(
    "change",
    [
        {"sat_no": 0},
        {"sat_no": 100_000},
        {"sat_no": "43874"},
        {"sat_no": True},
        {"change_date": "2026-11-11"},
        {"change_date": "1999-12-31"},
        {"change_date": "2026-02-30"},
        {"change_date": "10/11/2026"},
        {"window_days": 0},
        {"window_days": 32},
        {"provider": "KBR; DROP"},
        {"provider": ""},
        {"history_years": 3},
        {"threshold_km": 5000},
        {"unexpected": 1},
    ],
)
def test_every_run_parameter_is_bounded(client: TestClient, change: dict[str, Any]) -> None:
    response = client.post("/api/runs", json={**RUN, **change}, headers=HEADERS)
    assert response.status_code == 422, (change, response.text)


def test_a_warm_rerun_reads_nothing_from_the_udl(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        signed_in(test_client)
        test_client.post("/api/runs", json=RUN, headers=HEADERS)
        before = len(fake.requests)
        again = test_client.post(
            "/api/runs", json={**RUN, "window_days": 3}, headers=HEADERS
        ).json()
        state = test_client.get(f"/api/runs/{again['id']}").json()
        assert state["stage"] == "done"
        assert state["warm"] is True
        assert len(fake.requests) == before


def test_one_analyst_cannot_see_another_analysts_run(
    finished: tuple[TestClient, str], fake: FakeUdl
) -> None:
    _, run_id = finished
    with make_client(fake) as other:
        signed_in(other)
        assert other.get(f"/api/runs/{run_id}").status_code == 404
        assert other.get(f"/api/runs/{run_id}/figures/seasonal").status_code == 404


def test_one_run_at_a_time_and_results_wait_for_their_stage(fake: FakeUdl) -> None:
    runner = Deferred()
    with make_client(fake, submit=runner) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        assert run["stage"] == "queued"
        assert test_client.post("/api/runs", json=RUN, headers=HEADERS).status_code == 409
        assert test_client.get(f"/api/runs/{run['id']}/figures/seasonal").status_code == 409
        path = f"/api/runs/{run['id']}/scan"
        assert test_client.post(path, json={"times_ms": [1]}, headers=HEADERS).status_code == 409
        runner.release()
        assert test_client.get(f"/api/runs/{run['id']}").json()["stage"] == "done"


def test_a_failed_read_ends_the_run_with_a_plain_message(fake: FakeUdl) -> None:
    broken = FakeUdl(history=fake.history, elsets=fake.elsets)
    broken.fail_once["/udl/eoobservation/history"] = 403
    with make_client(broken) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        state = test_client.get(f"/api/runs/{run['id']}").json()
        assert state["stage"] == "failed"
        assert "did not accept your credentials" in state["message"]
        response = test_client.get(f"/api/runs/{run['id']}/flag")
        assert response.status_code == 409


def test_rate_limits_answer_429(fake: FakeUdl, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        limits, "RUNS", limits.Limit(per_session=1, service_wide=5, period_seconds=60)
    )
    monkeypatch.setattr(
        limits, "SIGN_IN_FAILURES", limits.Limit(per_session=2, service_wide=50, period_seconds=60)
    )
    with make_client(fake) as test_client:
        signed_in(test_client)
        signed_in(test_client)  # successful sign-ins are not counted
        test_client.post("/api/runs", json=RUN, headers=HEADERS)
        assert test_client.post("/api/runs", json=RUN, headers=HEADERS).status_code == 429
        wrong = {"username": USERNAME, "password": PASSWORD + "-wrong"}
        codes = [
            test_client.post("/api/session", json=wrong, headers=HEADERS).status_code
            for _ in range(3)
        ]
        assert codes == [401, 401, 429]


def test_the_session_cap_is_enforced_and_a_new_sign_in_replaces_the_old_session(
    fake: FakeUdl,
) -> None:
    app = create_app(
        Settings(max_sessions=1),
        client_factory=factory(fake),
        submit=lambda j: j(),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="https://testserver") as first:
        signed_in(first)
        old = first.cookies.get(COOKIE_NAME)
        signed_in(first)  # the same browser again: replaces, does not add
        assert first.cookies.get(COOKIE_NAME) != old
        with TestClient(app, base_url="https://testserver") as second:
            refused = second.post(
                "/api/session", json={"username": USERNAME, "password": PASSWORD}, headers=HEADERS
            )
            assert refused.status_code == 503
            second.cookies.set(COOKIE_NAME, old or "", domain="testserver")
            assert second.get("/api/session").json()["signed_in"] is False


def test_credentials_never_appear_in_a_response_or_a_log(
    fake: FakeUdl, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bodies = []
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        bodies.extend(
            test_client.get(f"/api/runs/{run['id']}{path}").text
            for path in ("", "/flag", "/figures/seasonal", "/figures/causes")
        )
    text = "\n".join(bodies) + "\n".join(r.getMessage() for r in caplog.records)
    assert PASSWORD not in text
    audit = [r.__dict__["attributes"] for r in caplog.records if r.name == "lightbee.audit"]
    assert audit[-1]["event.name"] == "lightbee.run"
    assert audit[-1]["lightbee.outcome"] == "done"
    assert USERNAME not in json.dumps(audit)


def test_validation_errors_name_fields_not_values(client: TestClient) -> None:
    response = client.post("/api/runs", json={**RUN, "provider": "<script>x"}, headers=HEADERS)
    assert response.status_code == 422
    assert "<script>" not in response.text
    assert response.json() == {
        "error": "Choose an elset provider from the list.",
        "fields": ["provider"],
    }
    assert api_module.MAX_SELECTED_TIMES == 50_000


def test_a_scan_without_target_elsets_or_past_its_limit_is_refused(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        limits, "SCANS", limits.Limit(per_session=1, service_wide=5, period_seconds=60)
    )
    selection = {"times_ms": [int(NOW.timestamp() * 1000) - 86_400_000]}
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post(
            "/api/runs", json={**RUN, "provider": "NobodyFilesHere"}, headers=HEADERS
        ).json()
        path = f"/api/runs/{run['id']}/scan"
        response = test_client.post(path, json=selection, headers=HEADERS)
        assert response.status_code == 400
        assert "no elset" in response.json()["error"]
        assert test_client.post(path, json=selection, headers=HEADERS).status_code == 429


def test_an_unreachable_udl_at_sign_in_answers_502() -> None:
    import httpx2

    from lightbee.udl.client import UdlReadClient

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    app = create_app(
        Settings(),
        client_factory=lambda creds: UdlReadClient(
            Settings(), creds, transport=httpx2.MockTransport(refuse), sleep=lambda _: None
        ),
        submit=lambda job: job(),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="https://testserver") as test_client:
        response = test_client.post(
            "/api/session", json={"username": USERNAME, "password": PASSWORD}, headers=HEADERS
        )
        assert response.status_code == 502


def test_the_default_runner_runs_jobs_in_the_background() -> None:
    import threading

    from lightbee.web.app import _default_submit

    done = threading.Event()
    _default_submit()(done.set)
    assert done.wait(5)


class MovingClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> Any:
        return self.now


def test_a_warm_rerun_for_today_reuses_photometry_read_minutes_ago(fake: FakeUdl) -> None:
    from datetime import timedelta

    clock = MovingClock()
    app = create_app(Settings(), client_factory=factory(fake), submit=lambda j: j(), clock=clock)
    with TestClient(app, base_url="https://testserver") as test_client:
        signed_in(test_client)
        test_client.post("/api/runs", json=RUN, headers=HEADERS)
        clock.now = NOW + timedelta(minutes=20)
        before = len(fake.requests)
        again = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        state = test_client.get(f"/api/runs/{again['id']}").json()
        assert state["warm"] is True
        assert len(fake.requests) == before
        clock.now = NOW + timedelta(minutes=45)  # beyond the tolerance: read afresh
        third = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        assert test_client.get(f"/api/runs/{third['id']}").json()["warm"] is False


def test_a_wider_window_rereads_elsets_and_the_snapshot_but_not_photometry(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        signed_in(test_client)
        test_client.post("/api/runs", json={**RUN, "window_days": 1}, headers=HEADERS)
        before = len(fake.requests)
        wide = test_client.post(
            "/api/runs", json={**RUN, "window_days": 14}, headers=HEADERS
        ).json()
        state = test_client.get(f"/api/runs/{wide['id']}").json()
        assert state["warm"] is False
        paths = {request.url.path for request in fake.requests[before:]}
        assert paths == {"/udl/elset/tuple"}


def test_signing_out_stops_a_run_and_leaves_nothing_cached(fake: FakeUdl) -> None:
    runner = Deferred()
    with make_client(fake, submit=runner) as test_client:
        signed_in(test_client)
        session = next(iter(test_client.app.state.sessions._sessions.values()))  # type: ignore[attr-defined]  # the app behind TestClient
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        test_client.delete("/api/session", headers=HEADERS)
        before = len(fake.requests)
        runner.release()
        assert len(fake.requests) == before  # not one read with the signed-out credentials
        assert session.cache == {}
        assert session.run is None
        assert run["stage"] == "queued"


def test_the_seasonal_figure_and_flag_follow_the_chosen_magnitude_basis(
    finished: tuple[TestClient, str],
) -> None:
    client, run_id = finished
    normalised = client.get(f"/api/runs/{run_id}/figures/seasonal").json()
    raw = client.get(f"/api/runs/{run_id}/figures/seasonal?basis=raw").json()
    assert "range-normalised" in json.dumps(normalised["layout"]["annotations"])
    assert "range-normalised" not in json.dumps(raw["layout"]["annotations"])
    # Each basis gets limits fitted to its own magnitudes (about 7 mag apart at GEO).
    assert normalised["data"][1]["zmin"] < raw["data"][1]["zmin"] - 5
    # The date axis ends at the run's end, not at the next monthly tick.
    assert normalised["layout"]["yaxis2"]["range"][1] == int(NOW.timestamp() * 1000)
    flag_raw = client.get(f"/api/runs/{run_id}/flag?basis=raw").json()
    assert flag_raw["comparable_bins"] == 12  # the fake world's recent week covers 12 bins
    heat = normalised["data"][1]
    # Row 0 is the newest day and sits at the top (the latest date).
    assert heat["y"][0] == max(heat["y"])
    assert heat["y"][0] > int(NOW.timestamp() * 1000) - 86_400_000
    # The predecessor's seismic scale on negated magnitudes: faint navy, white
    # in the middle, dark red brightest, so the colour bar reads bright at the top.
    stops = dict(heat["colorscale"])
    assert (stops[0.0], stops[0.5], stops[1.0]) == ("#00004d", "#ffffff", "#800000")
    assert heat["zmax"] > heat["zmin"]
    row = heat["customdata"][0]
    assert all(z == -c for z, c in zip(heat["z"][0], row, strict=True) if c is not None)
    labels = heat["colorbar"]["ticktext"]
    assert all(float(t) == -v for t, v in zip(labels, heat["colorbar"]["tickvals"], strict=True))
    assert len(normalised["layout"]["yaxis2"]["tickvals"]) == 28
    assert client.get(f"/api/runs/{run_id}/flag?basis=other").status_code == 422


def test_oversized_bodies_are_refused_before_parsing(client: TestClient) -> None:
    big = b'{"username": "' + b"x" * (1024 * 1024) + b'"}'
    response = client.post(
        "/api/session", content=big, headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.headers["content-security-policy"].startswith("default-src 'none'")

    def stream() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 100_000

    chunked = client.post("/api/runs", content=stream(), headers=HEADERS)
    assert chunked.status_code == 413
    lying = client.post("/api/runs", content=b"{}", headers={**HEADERS, "Content-Length": "x"})
    assert lying.status_code in (400, 413)


def test_one_users_wrong_passwords_cannot_lock_out_another(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        limits, "SIGN_IN_FAILURES", limits.Limit(per_session=3, service_wide=50, period_seconds=60)
    )
    with make_client(fake) as test_client:
        bad = {"username": "someone.else", "password": PASSWORD + "-wrong"}
        codes = [
            test_client.post("/api/session", json=bad, headers=HEADERS).status_code
            for _ in range(5)
        ]
        assert codes[-1] == 429
        signed_in(test_client)  # a different username is unaffected


def test_a_flood_from_one_session_is_slowed_and_the_page_still_answers(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        limits, "REQUESTS", limits.Limit(per_session=5, service_wide=100, period_seconds=60)
    )
    with make_client(fake) as test_client:
        signed_in(test_client)
        codes = [test_client.get("/api/session").status_code for _ in range(7)]
        assert codes[:5] == [200] * 5
        assert codes[5:] == [429, 429]
        for path in ("/", "/healthz", "/static/app.css"):  # never limited
            assert test_client.get(path).status_code == 200


def test_anonymous_and_random_cookie_floods_cannot_lock_analysts_out(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        limits, "REQUESTS", limits.Limit(per_session=5, service_wide=5, period_seconds=60)
    )
    with make_client(fake) as analyst, make_client(fake) as stranger:
        for index in range(20):
            stranger.cookies.set(COOKIE_NAME, f"random-{index}")
            assert stranger.get("/api/session").status_code == 200
        assert stranger.get("/").status_code == 200
        signed_in(analyst)
        assert analyst.get("/api/session").json()["signed_in"] is True


def test_more_than_one_session_cookie_is_refused(client: TestClient) -> None:
    token = client.cookies.get(COOKIE_NAME)
    response = client.get(
        "/api/session",
        headers={"Cookie": f"{COOKIE_NAME}=decoy; {COOKIE_NAME}={token}"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "spelling",
    [
        "{name} ={token}",
        "{name}\t={token}",
        '{name}="{token}"',
        "{name}=junk; {name} ={token}",
    ],
)
def test_every_cookie_spelling_the_routes_accept_is_counted_or_refused(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    monkeypatch.setattr(
        limits, "REQUESTS", limits.Limit(per_session=3, service_wide=100, period_seconds=60)
    )
    with make_client(fake) as test_client:
        signed_in(test_client)
        token = test_client.cookies.get(COOKIE_NAME)
        test_client.cookies.clear()
        cookie = spelling.format(name=COOKIE_NAME, token=token)
        codes = [
            test_client.get("/api/session", headers={"Cookie": cookie}).status_code
            for _ in range(5)
        ]
    assert 200 not in codes[3:]


def test_random_usernames_cannot_lock_out_an_analyst_who_has_signed_in(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        limits, "SIGN_IN_FAILURES", limits.Limit(per_session=10, service_wide=5, period_seconds=60)
    )
    with make_client(fake) as test_client:
        signed_in(test_client)
        for index in range(6):
            bad = {"username": f"stranger.{index}", "password": "nope"}
            test_client.post("/api/session", json=bad, headers=HEADERS)
        newcomer = {"username": "new.analyst", "password": "nope"}
        assert test_client.post("/api/session", json=newcomer, headers=HEADERS).status_code == 429
        signed_in(test_client)  # a returning analyst still gets in


def test_odd_run_ids_and_loose_numbers_are_refused(finished: tuple[TestClient, str]) -> None:
    client, _ = finished
    assert client.get("/api/runs/%C3%A9").status_code == 404
    loose = client.post("/api/runs", json={**RUN, "history_years": "1.5"}, headers=HEADERS)
    assert loose.status_code == 422


def test_one_scan_at_a_time_per_session(finished: tuple[TestClient, str]) -> None:
    client, run_id = finished
    session = next(iter(client.app.state.sessions._sessions.values()))  # type: ignore[attr-defined]  # the app behind TestClient
    session.scanning = True
    try:
        times = {"times_ms": [int(NOW.timestamp() * 1000) - 86_400_000]}
        response = client.post(f"/api/runs/{run_id}/scan", json=times, headers=HEADERS)
        assert response.status_code == 409
    finally:
        session.scanning = False


def test_no_credential_form_leaks_on_any_path(
    fake: FakeUdl, caplog: pytest.LogCaptureFixture
) -> None:
    import base64

    caplog.set_level(logging.DEBUG)
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    broken = FakeUdl(history=fake.history, elsets=fake.elsets)
    broken.fail_once["/udl/eoobservation/history"] = 500
    texts = []
    with make_client(broken) as test_client:
        wrong = {"username": USERNAME, "password": PASSWORD + "-wrong"}
        texts.append(test_client.post("/api/session", json=wrong, headers=HEADERS).text)
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        texts.append(test_client.get(f"/api/runs/{run['id']}").text)
    text = "\n".join(texts) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in (PASSWORD, token, token[:16]):
        assert secret not in text
    audit = [
        json.dumps(r.__dict__["attributes"]) for r in caplog.records if r.name == "lightbee.audit"
    ]
    assert audit
    assert all(USERNAME not in line for line in audit)


def test_the_flag_is_published_before_the_elsets_are_read(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lightbee.udl.client import UdlError
    from lightbee.web import runs as runs_module

    def held(*args: Any, **kwargs: Any) -> Any:
        raise UdlError("held back for the test")

    monkeypatch.setattr(runs_module, "read_elsets", held)
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        state = test_client.get(f"/api/runs/{run['id']}").json()
        assert state["stage"] == "failed"
        assert "seasonal" in state["ready"]
        assert test_client.get(f"/api/runs/{run['id']}/flag").status_code == 200


def test_an_object_with_no_photometry_finishes_cold_and_warm(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        signed_in(test_client)
        for _ in range(2):
            run = test_client.post(
                "/api/runs", json={**RUN, "sat_no": 12345}, headers=HEADERS
            ).json()
            state = test_client.get(f"/api/runs/{run['id']}").json()
            assert state["stage"] == "done"
            assert "recent" in state["ready"]
            for figure in ("seasonal", "causes"):
                path = f"/api/runs/{run['id']}/figures/{figure}"
                assert test_client.get(path).status_code == 200
        assert state["warm"] is True


def test_a_run_released_for_memory_says_so(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        store = test_client.app.state.sessions  # type: ignore[attr-defined]  # the app behind TestClient
        session = store.get(test_client.cookies.get(COOKIE_NAME))
        store.enforce_budget(0, keep=session)
        state = test_client.get(f"/api/runs/{run['id']}").json()
        assert "run it again" in state["message"]
        response = test_client.get(f"/api/runs/{run['id']}/figures/seasonal")
        assert response.status_code == 410
        assert "run it again" in response.json()["error"]


def test_a_release_during_a_request_answers_410_not_500(fake: FakeUdl) -> None:
    with make_client(fake) as test_client:
        signed_in(test_client)
        run_id = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()["id"]
        store = test_client.app.state.sessions  # type: ignore[attr-defined]  # the app behind TestClient
        run = store.get(test_client.cookies.get(COOKIE_NAME)).run
        original = type(run).window_ms

        def released_now(self: Any) -> tuple[int, int]:
            self.release()  # lands after the route's first check
            return original(self)

        type(run).window_ms = released_now  # type: ignore[method-assign]  # inject the race
        try:
            response = test_client.get(f"/api/runs/{run_id}/figures/causes")
        finally:
            type(run).window_ms = original  # type: ignore[method-assign]  # restore
        assert response.status_code == 410
        assert "run it again" in response.json()["error"]


def test_unknown_body_keys_are_not_echoed(client: TestClient) -> None:
    response = client.post("/api/runs", json={**RUN, "<img src=x>": 1}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["fields"] == ["body"]
    assert "<img" not in response.text


def test_the_raw_toggle_applies_before_the_history_arrives(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lightbee.udl.client import UdlError
    from lightbee.web import runs as runs_module

    real = runs_module.read_observations
    calls: list[int] = []

    def recent_only(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise UdlError("history held back for the test")
        return real(*args, **kwargs)

    monkeypatch.setattr(runs_module, "read_observations", recent_only)
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        raw = test_client.get(f"/api/runs/{run['id']}/figures/seasonal?basis=raw").json()
        normalised = test_client.get(f"/api/runs/{run['id']}/figures/seasonal").json()
    assert "range-normalised" not in json.dumps(raw["layout"]["annotations"])
    assert "range-normalised" in json.dumps(normalised["layout"]["annotations"])


def test_the_server_tells_the_page_its_date(client: TestClient) -> None:
    assert client.get("/api/session").json()["today"] == "2026-11-10"


def test_the_flag_is_published_once_the_seasons_are_read_and_matches_the_full_flag(
    fake: FakeUdl, finished: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lightbee.udl.client import UdlError
    from lightbee.web import runs as runs_module

    full_client, full_id = finished
    full = full_client.get(f"/api/runs/{full_id}/flag").json()
    real = runs_module.read_observations
    calls: list[int] = []

    def seasons_only(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) > 3:  # the recent weeks, then one season for each earlier year
            raise UdlError("the rest of the history held back for the test")
        return real(*args, **kwargs)

    monkeypatch.setattr(runs_module, "read_observations", seasons_only)
    with make_client(fake) as test_client:
        signed_in(test_client)
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        state = test_client.get(f"/api/runs/{run['id']}").json()
        assert state["stage"] == "failed"
        assert "flag" in state["ready"]
        assert "seasonal" not in state["ready"]
        early = test_client.get(f"/api/runs/{run['id']}/flag").json()
        figure = test_client.get(f"/api/runs/{run['id']}/figures/seasonal").json()
    for key in ("outcome", "comparable_bins", "median_delta_mag", "median_robust_z"):
        assert early[key] == full[key]
    assert early["headline"] in json.dumps(figure["layout"]["annotations"])


def test_one_failed_span_stops_its_siblings_at_once(
    fake: FakeUdl, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as clock

    from lightbee.udl.client import UdlError
    from lightbee.web import runs as runs_module

    real = runs_module.read_observations
    calls: list[int] = []

    def one_fails_one_lingers(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return real(*args, **kwargs)  # the recent weeks
        if len(calls) == 2:
            raise UdlError("one season failed")
        for _ in range(200):  # a slow sibling, reporting progress as it reads
            kwargs["on_progress"](1, 0, 1)
            clock.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(runs_module, "read_observations", one_fails_one_lingers)
    with make_client(fake) as test_client:
        signed_in(test_client)
        began = clock.monotonic()
        run = test_client.post("/api/runs", json=RUN, headers=HEADERS).json()
        state = test_client.get(f"/api/runs/{run['id']}").json()
    assert clock.monotonic() - began < 3  # not the sibling's 10 s
    assert state["stage"] == "failed"
    assert "one season failed" in state["message"]
