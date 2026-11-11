"""Configuration comes from the environment, validated, failing closed."""

from __future__ import annotations

import pytest

from lightbee.config import (
    DEFAULT_MAX_SESSIONS,
    DEFAULT_SESSION_HOURS,
    DEFAULT_UDL_BASE_URL,
    ConfigError,
    load_settings,
)


def test_an_empty_environment_gives_the_code_defaults() -> None:
    settings = load_settings({})
    assert settings.udl_base_url == DEFAULT_UDL_BASE_URL
    assert settings.session_hours == DEFAULT_SESSION_HOURS
    assert settings.max_sessions == DEFAULT_MAX_SESSIONS


def test_the_process_environment_is_read_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIGHTBEE_SESSION_HOURS", "4")
    assert load_settings().session_hours == 4


def test_valid_overrides_are_honoured() -> None:
    settings = load_settings(
        {
            "LIGHTBEE_UDL_BASE_URL": "https://test.unifieddatalibrary.com:8443/",
            "LIGHTBEE_SESSION_HOURS": " 12 ",
            "LIGHTBEE_MAX_SESSIONS": "4",
        }
    )
    assert settings.udl_base_url == "https://test.unifieddatalibrary.com:8443"
    assert settings.session_hours == 12
    assert settings.max_sessions == 4


@pytest.mark.parametrize(
    "url",
    [
        "http://unifieddatalibrary.com",
        "ftp://unifieddatalibrary.com",
        "https://",
        "https://analyst@unifieddatalibrary.com",
        "https://@unifieddatalibrary.com",
        "https://unifieddatalibrary.com/udl",
        "https://unifieddatalibrary.com?x=1",
        "https://unifieddatalibrary.com#frag",
        "unifieddatalibrary.com",
        "https://unifieddatalibrary.com (production)",
        "https://unifieddatalibrary.com:abc",
        "https://unifieddatalibrary.com:0",
        "https://unifieddatalibrary.com:99999",
        "https://unifieddatalibrary.com:",
        "https://unifieddatalibrary.com\\evil.test",
        "https://unifieddatalibrary.com%00.evil.test",
        "https://evil%40unifieddatalibrary.com",
        "https://unifieddatalibrary.com\u202e",
        "https://\uff55nifieddatalibrary.com",
        "https://\u0443nifieddatalibrary.com",
        "https://169.254.169.254",
        "https://[::1]",
        "https://ho\nst.unifieddatalibrary.com",
        "https://evil.test",
        "https://unifieddatalibrary.com.evil.test",
        "https://notunifieddatalibrary.com",
        "https://[unifieddatalibrary.com]",
    ],
)
def test_an_unacceptable_base_url_stops_the_boot(url: str) -> None:
    with pytest.raises(ConfigError, match="LIGHTBEE_UDL_BASE_URL"):
        load_settings({"LIGHTBEE_UDL_BASE_URL": url})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LIGHTBEE_SESSION_HOURS", "0"),
        ("LIGHTBEE_SESSION_HOURS", "25"),
        ("LIGHTBEE_SESSION_HOURS", "eight"),
        ("LIGHTBEE_MAX_SESSIONS", "0"),
        ("LIGHTBEE_MAX_SESSIONS", "65"),
        ("LIGHTBEE_MAX_SESSIONS", "1.5"),
        ("LIGHTBEE_MAX_SESSIONS", "\u00b2"),
        ("LIGHTBEE_SESSION_HOURS", "9" * 5000),
    ],
)
def test_an_out_of_range_number_stops_the_boot(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({name: value})
