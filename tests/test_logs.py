"""The App Store logging standard: one JSON object per line, on stdout only.

The platform's check blocks uploads from 1 December 2026. These tests prove
the exact field set, the severity pairs, the request ID and email on every
line written while a request is handled (background runs included), and that
no credential, cookie, token or query string reaches any line.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import threading
import time
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from lightbee.__main__ import server_options
from lightbee.config import Settings
from lightbee.logs import (
    LOG_CONFIG,
    JsonFormatter,
    StdoutHandler,
    clean_email,
    clean_request_id,
    configure_logging,
    flatten,
    severity,
)
from lightbee.web.app import create_app
from lightbee.web.session import COOKIE_NAME
from tests.fake_world import NOW, PASSWORD, TARGET, USERNAME, factory, world

FIELDS = {"timestamp", "severity_text", "severity_number", "body", "attributes"}
OPTIONAL = {"trace_id", "span_id"}
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
PAIRS = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
REQUEST_ID = "req-7f3a9c41-test"
EMAIL = "analyst@example.test"
HEADERS = {"X-Light-Bee": "1", "X-Request-Id": REQUEST_ID, "X-Auth-Request-Email": EMAIL}
RUN = {"sat_no": TARGET, "change_date": "2026-11-10", "window_days": 7}
ID_KEY = "http.request.header.x-request-id"
EMAIL_KEY = "user.email"


def lines(text: str) -> list[dict[str, Any]]:
    """Every stdout line, each of which must be one JSON object of the standard shape."""
    parsed = []
    for raw in text.splitlines():
        line = json.loads(raw)
        assert isinstance(line, dict), raw
        assert FIELDS <= set(line) <= FIELDS | OPTIONAL, raw
        assert STAMP.fullmatch(line["timestamp"]), raw
        assert PAIRS[line["severity_text"]] == line["severity_number"], raw
        assert isinstance(line["body"], str), raw
        assert isinstance(line["attributes"], dict), raw
        for key, value in line["attributes"].items():
            assert isinstance(key, str), raw
            assert isinstance(value, str | int | float | bool), raw
        assert not {"service.name", "tenant.id", "app.id"} & set(line["attributes"]), raw
        parsed.append(line)
    return parsed


@pytest.fixture(autouse=True)
def json_logging() -> None:
    """The container's set-up: uvicorn's dictConfig, then the app's own call."""
    uvicorn.Config("asgi:app", **server_options({}))
    configure_logging()


def record(level: int, message: str = "m", exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord("lightbee.t", level, __file__, 1, message, None, exc_info)


@pytest.mark.parametrize(
    ("level", "text", "number"),
    [
        (logging.CRITICAL, "FATAL", 21),
        (logging.ERROR, "ERROR", 17),
        (logging.WARNING, "WARN", 13),
        (logging.INFO, "INFO", 9),
        (logging.DEBUG, "DEBUG", 5),
        (5, "TRACE", 1),
        (25, "INFO", 9),
        (60, "FATAL", 21),
    ],
)
def test_severity_pairs_follow_opentelemetry(level: int, text: str, number: int) -> None:
    assert severity(level) == (text, number)
    line = json.loads(JsonFormatter().format(record(level)))
    assert (line["severity_text"], line["severity_number"]) == (text, number)


def test_a_line_has_exactly_the_standard_fields() -> None:
    created = record(logging.INFO, "hello %s")
    created.created = 1794470400.1234
    line = json.loads(JsonFormatter().format(created))
    assert set(line) == FIELDS
    assert line["timestamp"] == "2026-11-12T08:00:00.123Z"
    assert line["body"] == "hello %s"
    assert line["attributes"]["code.namespace"] == "lightbee.t"
    assert ID_KEY not in line["attributes"]
    assert EMAIL_KEY not in line["attributes"]


def test_exceptions_become_the_three_opentelemetry_attributes() -> None:
    def fail() -> None:
        raise ValueError("bad value")

    with pytest.raises(ValueError, match="bad value") as caught:
        fail()
    error = caught.value
    created = record(logging.ERROR, "failed", exc_info=(ValueError, error, error.__traceback__))
    line = json.loads(JsonFormatter().format(created))
    attributes = line["attributes"]
    assert attributes["exception.type"] == "ValueError"
    assert attributes["exception.message"] == "bad value"
    assert "Traceback" in attributes["exception.stacktrace"]
    assert line["body"] == "failed"
    assert "\n" not in JsonFormatter().format(created)


def test_nested_attributes_are_flattened_to_dotted_scalars() -> None:
    flat = flatten({"a": {"b": 1, "c": {"d": True}}, "e": None, "f": 1.5})
    assert flat == {"a.b": 1, "a.c.d": True, "e": "None", "f": 1.5}


def test_request_ids_and_emails_are_bounded_plain_labels() -> None:
    assert clean_request_id(REQUEST_ID) == REQUEST_ID
    for bad in (None, "", "a b", "x" * 129, "id\nforged", '{"severity_text":"FATAL"}'):
        fresh = clean_request_id(bad)
        assert re.fullmatch(r"[0-9a-f]{32}", fresh)
    assert clean_email(EMAIL) == EMAIL
    for bad in (None, "", "not-an-email", "a@b\nc", "x" * 70 + "@example.test", "a b@c.d"):
        assert clean_email(bad) is None


def test_the_container_set_up_writes_json_to_stdout_and_nothing_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_app(Settings())
    create_app(Settings())  # safe twice: still one handler
    uvicorn.Config("asgi:app", **server_options({}))
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logging.getLogger("lightbee.audit").info("run done")
    logging.getLogger("lightbee").debug("not shown at INFO")
    logging.getLogger("httpx2").info("HTTP Request: GET https://udl.test/x?satNo=1")
    thread = threading.Thread(target=lambda: 1 / 0)
    thread.start()
    thread.join()
    out, err = capsys.readouterr()
    assert err == ""
    parsed = lines(out)
    bodies = [line["body"] for line in parsed]
    assert "Started server process [1]" in bodies
    assert "run done" in bodies
    assert "not shown at INFO" not in bodies
    assert "satNo" not in out
    crash = [line for line in parsed if line["body"] == "uncaught exception"]
    assert crash[0]["severity_text"] == "FATAL"
    assert crash[0]["attributes"]["exception.type"] == "ZeroDivisionError"
    root = logging.getLogger()
    assert sum(isinstance(h, StdoutHandler) for h in root.handlers) == 1
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "lightbee"):
        for handler in logging.getLogger(name).handlers:
            # Only ours prints; pytest's own capture handler writes to memory.
            stream = getattr(handler, "stream", None)
            assert isinstance(handler, StdoutHandler) or stream not in (sys.stdout, sys.stderr)


def test_a_handler_printing_to_stderr_is_removed_and_info_is_restored() -> None:
    printer = logging.StreamHandler(sys.stderr)
    named = logging.getLogger("uvicorn.error")
    named.addHandler(printer)
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(logging.WARNING)
    configure_logging()
    assert printer not in named.handlers
    assert not [
        h
        for h in root.handlers
        if not isinstance(h, StdoutHandler) and getattr(h, "stream", None) is sys.stderr
    ]
    assert root.level == logging.INFO


def test_uvicorn_is_given_the_json_configuration() -> None:
    assert server_options({})["log_config"] is LOG_CONFIG


def test_an_invalid_port_is_a_fatal_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        server_options({"PORT": "eighty"})
    assert stopped.value.code == 2
    out, err = capsys.readouterr()
    assert err == ""
    (line,) = lines(out)
    assert line["severity_text"] == "FATAL"
    assert "PORT must be" in line["body"]


def wait_until_finished(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state: dict[str, Any] = client.get(f"/api/runs/{run_id}", headers=HEADERS).json()
        if state["stage"] in ("done", "failed"):
            return state
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_request_lines_carry_the_request_id_and_email_and_no_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = world()
    # The real background pool, so the run's own thread is exercised.
    app = create_app(Settings(), client_factory=factory(fake), clock=lambda: NOW)
    capsys.readouterr()
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    with TestClient(app, base_url="https://testserver") as client:
        signed = client.post(
            "/api/session", json={"username": USERNAME, "password": PASSWORD}, headers=HEADERS
        )
        assert signed.status_code == 200
        cookie = client.cookies.get(COOKIE_NAME)
        start_headers = {**HEADERS, "X-Request-Id": "req-start-run"}
        run = client.post("/api/runs", json=RUN, headers=start_headers).json()
        assert wait_until_finished(client, run["id"])["stage"] == "done"
        client.get(f"/api/runs/{run['id']}/flag?basis=normalised", headers=HEADERS)
        client.get("/api/session", headers={"X-Request-Id": "bad id with spaces"})
        client.get("/healthz", headers=HEADERS)
    out, err = capsys.readouterr()
    assert err == ""
    parsed = lines(out)
    requests = [line for line in parsed if line["body"] == "request"]
    assert len(requests) >= 4
    for line in requests:
        attributes = line["attributes"]
        assert ID_KEY in attributes
        assert attributes["url.path"].startswith("/api/")
        assert "?" not in attributes["url.path"]
    tagged = [line for line in requests if line["attributes"][ID_KEY] == REQUEST_ID]
    assert tagged
    assert all(line["attributes"][EMAIL_KEY] == EMAIL for line in tagged)
    # The run's audit line, written on the pool thread, carries the starting request's ID.
    audit = [line for line in parsed if line["attributes"].get("event.name") == "lightbee.run"]
    assert audit[-1]["attributes"]["lightbee.outcome"] == "done"
    assert audit[-1]["attributes"][ID_KEY] == "req-start-run"
    assert audit[-1]["attributes"][EMAIL_KEY] == EMAIL
    # An unusable request ID is replaced, never copied into the log.
    assert "bad id with spaces" not in out
    # Probes are not logged.
    assert all(line["attributes"]["url.path"] != "/healthz" for line in requests)
    for secret in (PASSWORD, token, token[:16], cookie or "missing-cookie", USERNAME):
        assert secret not in out
    for forbidden in ("authorization", "cookie", "basis=", "satNo", "service.name"):
        assert forbidden not in out.lower()
