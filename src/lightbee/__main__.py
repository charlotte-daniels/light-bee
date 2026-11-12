"""Run the server: `python -m lightbee`.

The container's command, in exec form, so no shell is needed in the image and
SIGTERM reaches the server directly. The port comes from `PORT` (the platform
injects it; the default is 8080) and the interface from `HOST`, which the
Dockerfile sets for the container. Outside a container the default is the
loopback interface, so a developer's run is never exposed by accident.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Final

import uvicorn

from lightbee.logs import LOG_CONFIG, configure_logging

DEFAULT_PORT: Final = 8080
DEFAULT_HOST: Final = "127.0.0.1"


def server_options(environ: Mapping[str, str]) -> dict[str, Any]:
    """The uvicorn options for this environment, validated."""
    raw_port = environ.get("PORT", "").strip() or str(DEFAULT_PORT)
    valid = raw_port.isascii() and raw_port.isdigit() and len(raw_port) <= 5
    if not valid or not 1 <= int(raw_port) <= 65535:
        # Logged as JSON on stdout like every other line, never printed to stderr.
        configure_logging()
        logging.getLogger("lightbee").critical(
            "PORT must be a whole number between 1 and 65535, got %r", raw_port[:32]
        )
        raise SystemExit(2)
    return {
        "host": environ.get("HOST", "").strip() or DEFAULT_HOST,
        "port": int(raw_port),
        # One worker: sessions and their credentials live in this process.
        "workers": 1,
        "access_log": False,
        "timeout_graceful_shutdown": 30,
        "server_header": False,
        "log_config": LOG_CONFIG,
    }


def main(
    environ: Mapping[str, str] | None = None,
    run: Callable[..., None] = uvicorn.run,
) -> None:
    """Start the one-worker server for `asgi:app`."""
    run("asgi:app", **server_options(os.environ if environ is None else environ))


if __name__ == "__main__":  # pragma: no cover - the container's entry point
    main()
