"""Settings, read from the environment only, validated, with code defaults.

The App Store environment tab should normally be empty: the defaults here are
the deployed configuration, and anything typed into the tab overrides them. A
value that is present but invalid stops the app at boot with a message naming
the variable, rather than running on a silently substituted default.

The UDL base URL is the host that receives analysts' credentials, so it is held
to a strict shape (a bare https origin, plain ASCII hostname, no userinfo) and
to an allowlist of UDL domains.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

DEFAULT_UDL_BASE_URL: Final = "https://unifieddatalibrary.com"
DEFAULT_SESSION_HOURS: Final = 8
DEFAULT_MAX_SESSIONS: Final = 16

# Credentials may only ever be sent to the UDL itself or one of its subdomains.
UDL_DOMAINS: Final = ("unifieddatalibrary.com",)

_SESSION_HOURS_RANGE: Final = (1, 24)
_MAX_SESSIONS_RANGE: Final = (1, 64)
# Longer digit strings are refused before int() so Python's digit limit never
# raises an unlabelled error.
_MAX_DIGITS: Final = 6

# Printable ASCII with no space: anything else (prose, control characters,
# bidirectional overrides, full-width or homoglyph letters) is refused outright.
_PRINTABLE_NO_SPACE: Final = re.compile(r"[!-~]+")
# Letters, digits and hyphens per label, at least two labels; IDN only as xn--.
_HOSTNAME: Final = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
)


class ConfigError(ValueError):
    """A configuration value is present but not acceptable."""


@dataclass(frozen=True, slots=True)
class Settings:
    """The validated runtime configuration."""

    udl_base_url: str = DEFAULT_UDL_BASE_URL
    session_hours: int = DEFAULT_SESSION_HOURS
    max_sessions: int = DEFAULT_MAX_SESSIONS


def _is_udl_host(hostname: str) -> bool:
    return any(hostname == domain or hostname.endswith("." + domain) for domain in UDL_DOMAINS)


def _https_base_url(name: str, raw: str) -> str:
    value = raw.strip()
    if not _PRINTABLE_NO_SPACE.fullmatch(value):
        raise ConfigError(f"{name} must be plain printable ASCII with no spaces")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a well-formed URL") from exc
    if parts.scheme != "https" or "@" in parts.netloc:
        raise ConfigError(f"{name} must be an https URL with no credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ConfigError(f"{name} must be a bare origin with no path, query or fragment")
    hostname = parts.hostname or ""
    if not _HOSTNAME.fullmatch(hostname) or parts.netloc.endswith(":"):
        raise ConfigError(f"{name} must name a plain DNS hostname")
    if not _is_udl_host(hostname):
        raise ConfigError(f"{name} must be a UDL host ({', '.join(UDL_DOMAINS)})")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"{name} has an invalid port")
    return f"https://{hostname}" + (f":{port}" if port is not None else "")


def _bounded_int(name: str, raw: str, bounds: tuple[int, int]) -> int:
    value = raw.strip()
    if not (value.isascii() and value.isdigit()) or len(value) > _MAX_DIGITS:
        raise ConfigError(f"{name} must be a whole number")
    number = int(value)
    low, high = bounds
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the environment, falling back to code defaults."""
    env = os.environ if environ is None else environ
    base_url = DEFAULT_UDL_BASE_URL
    session_hours = DEFAULT_SESSION_HOURS
    max_sessions = DEFAULT_MAX_SESSIONS
    if raw := env.get("LIGHTBEE_UDL_BASE_URL"):
        base_url = _https_base_url("LIGHTBEE_UDL_BASE_URL", raw)
    if raw := env.get("LIGHTBEE_SESSION_HOURS"):
        session_hours = _bounded_int("LIGHTBEE_SESSION_HOURS", raw, _SESSION_HOURS_RANGE)
    if raw := env.get("LIGHTBEE_MAX_SESSIONS"):
        max_sessions = _bounded_int("LIGHTBEE_MAX_SESSIONS", raw, _MAX_SESSIONS_RANGE)
    return Settings(
        udl_base_url=base_url,
        session_hours=session_hours,
        max_sessions=max_sessions,
    )
