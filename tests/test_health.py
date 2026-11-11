"""The platform contract: health paths answer 200, unauthenticated, touching nothing."""

from __future__ import annotations

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


def test_readiness_reports_state_and_version_in_the_body(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body == {"status": "ready", "version": __version__}


def test_no_interactive_api_documentation_is_served(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
