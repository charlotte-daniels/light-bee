"""The server entry point reads PORT and HOST, validates them, and runs one worker."""

from __future__ import annotations

from typing import Any

import pytest

from lightbee.__main__ import DEFAULT_HOST, DEFAULT_PORT, main, server_options
from lightbee.logs import LOG_CONFIG, StdoutHandler


def test_defaults_are_port_8080_on_loopback() -> None:
    options = server_options({})
    assert options["port"] == DEFAULT_PORT == 8080
    assert options["host"] == DEFAULT_HOST
    assert options["workers"] == 1


def test_the_platform_port_and_container_host_are_honoured() -> None:
    options = server_options({"PORT": "9090", "HOST": "0.0.0.0"})  # noqa: S104 - container bind under test
    assert options["port"] == 9090
    assert options["host"] == "0.0.0.0"  # noqa: S104 - container bind under test


@pytest.mark.parametrize("value", ["0", "65536", "eighty", "-1", "80.5", "\u00b2", "9" * 5000])
def test_an_invalid_port_stops_the_boot(value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        server_options({"PORT": value})
    assert stopped.value.code == 2


def test_main_runs_the_asgi_app_with_those_options() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(target: str, **kwargs: Any) -> None:
        calls.append((target, kwargs))

    main({"PORT": "8081"}, run=fake_run)
    assert calls == [
        (
            "asgi:app",
            {
                "host": "127.0.0.1",
                "port": 8081,
                "workers": 1,
                "access_log": False,
                "timeout_graceful_shutdown": 30,
                "server_header": False,
                "log_config": LOG_CONFIG,
            },
        )
    ]


def test_audit_lines_reach_standard_output_under_the_real_server_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import logging

    import uvicorn

    from lightbee.config import Settings
    from lightbee.web.app import create_app

    # Exactly what uvicorn.run builds, logging set-up included, with no test
    # fixture lowering any level. Built before and after the app, as in the
    # container (uvicorn configures logging first, then imports the app).
    uvicorn.Config("asgi:app", **server_options({}))
    create_app(Settings())
    create_app(Settings())  # safe to build twice: still one handler
    uvicorn.Config("asgi:app", **server_options({}))
    audit = logging.getLogger("lightbee.audit")
    audit.info("run done")
    out, err = capsys.readouterr()
    assert out.count('"body":"run done"') == 1
    assert err == ""
    assert sum(isinstance(h, StdoutHandler) for h in logging.getLogger().handlers) == 1
