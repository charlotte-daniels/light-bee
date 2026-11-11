"""Every response carries the security headers; the policy only tightens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lightbee.config import Settings
from lightbee.web.app import CONTENT_SECURITY_POLICY, create_app


def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    for path in ("/", "/healthz", "/readyz", "/static/app.css", "/static/nope"):
        headers = client.get(path).headers
        assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-frame-options"] == "DENY"
        assert headers["strict-transport-security"].startswith("max-age=")


def test_policy_forbids_inline_script_and_third_party_hosts() -> None:
    directives = dict(
        part.strip().split(" ", 1) for part in CONTENT_SECURITY_POLICY.split(";") if part.strip()
    )
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


def test_dynamic_responses_are_not_cached(client: TestClient) -> None:
    assert client.get("/readyz").headers["cache-control"] == "no-store"


def test_a_trailing_slash_is_not_redirected_so_no_host_is_reflected(client: TestClient) -> None:
    response = client.get("/healthz/", headers={"Host": "evil.test"}, follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_an_unhandled_error_is_generic_and_still_carries_the_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings())

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("internal detail that must not leak")

    response = TestClient(app, raise_server_exceptions=False).get("/explode")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert "internal detail" not in response.text
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"
    assert "internal detail that must not leak" in caplog.text
