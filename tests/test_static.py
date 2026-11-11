"""Static assets are served by allowlisted key only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lightbee.web.app import STATIC_ASSETS


def test_every_allowlisted_asset_is_served_with_its_media_type(client: TestClient) -> None:
    for key, asset in STATIC_ASSETS.items():
        response = client.get(f"/static/{key}")
        assert response.status_code == 200
        assert response.headers["content-type"] == asset.media_type
        assert response.content


@pytest.mark.parametrize(
    "key",
    ["index.html", "missing.css", "..%2F..%2Fasgi.py", "app.css%00.js", "APP.CSS"],
)
def test_anything_not_allowlisted_is_not_found(client: TestClient, key: str) -> None:
    assert client.get(f"/static/{key}").status_code == 404


def test_traversal_never_reaches_a_file(client: TestClient) -> None:
    response = client.get("/static/../web/app.py")
    assert response.status_code == 404
    assert "create_app" not in response.text
