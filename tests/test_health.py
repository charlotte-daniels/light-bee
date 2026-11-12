"""The platform contract: health paths answer 200, unauthenticated, touching nothing."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lightbee import __version__


@pytest.mark.parametrize("path", ["/", "/healthz", "/readyz", "/livez", "/ping"])
def test_contract_paths_answer_200_without_credentials(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200


def test_root_is_html_not_a_redirect(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.headers["content-type"].startswith("text/html")
    assert "Light Bee" in response.text


def test_readiness_reports_state_version_start_and_peak_memory(client: TestClient) -> None:
    before = time.time()
    body = client.get("/readyz").json()
    assert set(body) == {"status", "version", "started_unix", "uptime_seconds", "peak_memory_mb"}
    assert body["status"] == "ready"
    assert body["version"] == __version__
    # Numbers only, so a restart (a later start, a fresh peak) shows over HTTP.
    assert isinstance(body["started_unix"], int)
    assert 0 < body["started_unix"] <= before
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
    assert isinstance(body["peak_memory_mb"], float)
    assert body["peak_memory_mb"] > 0


def test_head_on_the_root_answers_200_with_no_body(client: TestClient) -> None:
    response = client.head("/", follow_redirects=False)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/", "/healthz", "/readyz", "/livez", "/ping"])
def test_contract_paths_open_no_session(client: TestClient, path: str) -> None:
    store = client.app.state.sessions  # type: ignore[attr-defined]
    before = len(store)
    response = client.get(path)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert len(store) == before


def test_no_interactive_api_documentation_is_served(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
