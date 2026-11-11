"""The version is stated in pyproject.toml and the package, and readiness reports it."""

from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

import asgi

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def test_the_deployed_entry_point_reports_the_project_version() -> None:
    body = TestClient(asgi.app).get("/readyz").json()
    assert body["version"] == _project_version()
