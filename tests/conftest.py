"""Shared fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lightbee.config import Settings
from lightbee.web.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()))
