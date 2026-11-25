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
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from lightbee.__main__ import server_options
from lightbee.config import Settings
from lightbee.logs import (
    LOG_CONFIG,
    REQUEST_FIELDS,
    JsonFormatter,
    StdoutHandler,
    clean_email,
    clean_path,
    clean_request_id,
    clean_user_agent,
    configure_logging,
    flatten,
    outcome,
    severity,
)
from lightbee.logs import REQUEST_ID as REQUEST_ID_VAR
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
    # uvicorn's access line prints the query string; found by the image probe.
    logging.getLogger("uvicorn.access").info('1.2.3.4:5 - "GET /api/x?satNo=2 HTTP/1.1" 200')
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


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_real_server_logs_no_query_string_no_probe_and_nothing_to_stderr() -> None:
    """The container's own entry point, over real HTTP: uvicorn's access log stays off.

    Found by the 1.1 image probe: uvicorn writes its access line, query string
    included, whenever its access logger can reach a handler.
    """
    port = _free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "lightbee"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx2.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                time.sleep(0.1)
        headers = {"X-Request-Id": "real-1", "X-Auth-Request-Email": EMAIL}
        assert (
            httpx2.get(f"{base}/api/session?password=QSTRSECRET", headers=headers).status_code
            == 200
        )
        assert httpx2.head(f"{base}/").status_code == 200
    finally:
        server.send_signal(signal.SIGTERM)
        out, err = server.communicate(timeout=30)
    assert err == ""
    parsed = lines(out)
    assert "QSTRSECRET" not in out
    assert all(line["attributes"]["code.namespace"] != "uvicorn.access" for line in parsed)
    requests = [line for line in parsed if line["body"] == "request"]
    assert [line["attributes"]["url.path"] for line in requests] == ["/api/session"]
    assert requests[0]["attributes"][ID_KEY] == "real-1"
    assert requests[0]["attributes"][EMAIL_KEY] == EMAIL
    assert any(line["body"].startswith("Uvicorn running on") for line in parsed)


def test_a_start_up_failure_is_a_fatal_json_line_not_a_traceback_on_stderr() -> None:
    env = {
        **os.environ,
        "PORT": str(_free_port()),
        "HOST": "127.0.0.1",
        "PYTHONPATH": os.pathsep.join(sys.path),
        "LIGHTBEE_MAX_SESSIONS": "zz",
    }
    done = subprocess.run(
        [sys.executable, "-m", "lightbee"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode != 0
    assert done.stderr == ""
    fatal = [line for line in lines(done.stdout) if line["severity_text"] == "FATAL"]
    assert fatal
    assert fatal[0]["attributes"]["exception.type"] == "ConfigError"


def test_a_repeated_identity_header_is_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    app = create_app(Settings())
    capsys.readouterr()
    with TestClient(app, base_url="https://testserver") as client:
        client.get(
            "/api/session",
            headers=[
                ("X-Request-Id", "first-id"),
                ("X-Request-Id", "second-id"),
                ("X-Auth-Request-Email", "forged@example.test"),
                ("X-Auth-Request-Email", EMAIL),
            ],
        )
    out = capsys.readouterr().out
    (line,) = [line for line in lines(out) if line["body"] == "request"]
    assert EMAIL_KEY not in line["attributes"]
    assert line["attributes"][ID_KEY] not in ("first-id", "second-id")
    assert "forged@" not in out


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
        client.get("/healthz")
    out, err = capsys.readouterr()
    assert err == ""
    parsed = lines(out)
    requests = [line for line in parsed if line["body"] == "request"]
    assert len(requests) >= 4
    for line in requests:
        attributes = line["attributes"]
        assert ID_KEY in attributes
        assert attributes["url.path"].startswith("/api/") or attributes["url.path"] == "/healthz"
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
    # A probe is logged only when it carries a request ID.
    probes = [line for line in requests if line["attributes"]["url.path"] == "/healthz"]
    assert [line["attributes"][ID_KEY] for line in probes] == [REQUEST_ID]
    for secret in (PASSWORD, token, token[:16], cookie or "missing-cookie", USERNAME):
        assert secret not in out
    for forbidden in ("authorization", "cookie", "basis=", "satNo", "service.name"):
        assert forbidden not in out.lower()


def test_requests_carry_the_request_id_and_email_but_probes_do_not_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mirrors DRIFTER's test: a tagged request is logged anywhere, even to `/`."""
    app = create_app(Settings())
    capsys.readouterr()
    with TestClient(app, base_url="https://testserver") as client:
        client.get(
            "/api/session?secret=QSTRSECRET",
            headers={
                "X-Request-Id": "req-one",
                "X-Auth-Request-Email": EMAIL,
                "Authorization": "Basic Zm9vOmJhcg==",
            },
        )
        client.get("/", headers={"X-Request-Id": "req-two"})
        client.get("/healthz")
        client.get("/static/app.js")
    out, err = capsys.readouterr()
    assert err == ""
    parsed = lines(out)
    ids = [line["attributes"].get(ID_KEY) for line in parsed]
    assert "req-one" in ids
    assert "req-two" in ids
    one = next(line for line in parsed if line["attributes"].get(ID_KEY) == "req-one")
    attributes = one["attributes"]
    assert attributes[EMAIL_KEY] == EMAIL
    assert attributes["url.path"] == "/api/session"
    assert attributes["http.request.method"] == "GET"
    assert attributes["user_agent.original"] == "testclient"
    assert attributes["event.outcome"] == "success"
    assert attributes["http.response.status_code"] == 200
    assert attributes["http.route"] == "/api/session"
    two = next(line for line in parsed if line["attributes"].get(ID_KEY) == "req-two")
    assert two["attributes"]["http.route"] == "/"
    assert "QSTRSECRET" not in out
    assert "Zm9vOmJhcg" not in out
    quiet = {"/healthz", "/static/app.js"}
    assert not any(line["attributes"].get("url.path") in quiet for line in parsed)


def test_every_request_line_names_the_request_and_lines_stay_short(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mirrors DRIFTER's test for the 2.0.3 advisory: user_agent.original was missing."""
    app = create_app(Settings())
    capsys.readouterr()
    with TestClient(app, base_url="https://testserver") as client:
        client.get("/api/runs/no-such-run", headers={"X-Request-Id": "rq-401"})
        client.get("/api/session", headers={"X-Request-Id": "rq-agent", "User-Agent": "probe/1.0"})
        client.get("/api/session", headers={"X-Request-Id": "rq-none", "User-Agent": "bad\x7f"})
    parsed = lines(capsys.readouterr().out)
    tagged = [line for line in parsed if line["attributes"].get(ID_KEY) == "rq-401"]
    assert tagged
    for line in tagged:
        attributes = line["attributes"]
        assert attributes["url.path"] == "/api/runs/no-such-run"
        assert attributes["http.request.method"] == "GET"
        assert attributes["user_agent.original"] == "testclient"
    audit = tagged[-1]["attributes"]
    assert audit["event.outcome"] == "failure"
    assert audit["http.response.status_code"] == 401
    assert audit["http.route"] == "/api/runs/{run_id}"
    agents = {
        line["attributes"][ID_KEY]: line["attributes"]["user_agent.original"]
        for line in parsed
        if line["attributes"].get(ID_KEY) in ("rq-agent", "rq-none")
    }
    assert agents == {"rq-agent": "probe/1.0", "rq-none": "unknown"}
    assert clean_user_agent(None) == "unknown"
    assert outcome(0) == "unknown"
    assert outcome(302) == "success"
    assert REQUEST_FIELDS.get() is None, "context reset after the request"


def test_a_line_stays_under_64_kib_and_keeps_the_required_fields() -> None:
    """Long values are cut, never the request fields, even when escaping inflates them."""
    fields = {
        "url.path": "/api/x",
        "http.request.method": "GET",
        "user_agent.original": "a" * 256,
    }
    id_token = REQUEST_ID_VAR.set("rq-long")
    fields_token = REQUEST_FIELDS.set(fields)
    try:
        long_record = record(logging.INFO, "x" * 100_000)
        # Non-ASCII grows sixfold when escaped: 15,000 characters become 90 KB.
        long_record.attributes = {"lightbee.note": "é" * 15_000, "lightbee.other": "b" * 9}
        text = JsonFormatter().format(long_record)
    finally:
        REQUEST_FIELDS.reset(fields_token)
        REQUEST_ID_VAR.reset(id_token)
    assert len(text.encode()) < 64 * 1024
    # Parsed directly: a hand-built record has no function name, which `lines` refuses.
    line = json.loads(text)
    assert set(line) == FIELDS
    attributes = line["attributes"]
    assert attributes[ID_KEY] == "rq-long"
    assert attributes["user_agent.original"] == "a" * 256
    assert attributes["url.path"] == "/api/x"
    assert attributes["lightbee.other"] == "b" * 9
    assert attributes["lightbee.note"].endswith("...[cut]")
    assert line["body"].endswith("...[cut]")
    assert len(line["body"]) <= 8_000


def _format_in_a_request(created: logging.LogRecord) -> dict[str, Any]:
    fields = {"url.path": "/api/x", "http.request.method": "GET", "user_agent.original": "ua"}
    id_token = REQUEST_ID_VAR.set("rq-fit")
    fields_token = REQUEST_FIELDS.set(fields)
    try:
        text = JsonFormatter().format(created)
    finally:
        REQUEST_FIELDS.reset(fields_token)
        REQUEST_ID_VAR.reset(id_token)
    assert len(text.encode()) < 64 * 1024
    line: dict[str, Any] = json.loads(text)
    assert set(line) == FIELDS
    return line


def test_a_body_that_overflows_only_once_escaped_is_cut() -> None:
    created = record(logging.INFO, "é" * 8_000)
    created.attributes = {"lightbee.note": "é" * 5_000}
    line = _format_in_a_request(created)
    assert line["body"].endswith("...[cut]")
    assert line["attributes"][ID_KEY] == "rq-fit"


def test_many_short_attributes_keep_only_the_required_fields() -> None:
    created = record(logging.INFO, "short")
    created.attributes = {f"lightbee.a{n}": "é" * 8 for n in range(5_000)}
    attributes = _format_in_a_request(created)["attributes"]
    assert attributes["lightbee.attributes_dropped"] is True
    assert attributes[ID_KEY] == "rq-fit"
    assert attributes["url.path"] == "/api/x"
    assert not any(key.startswith("lightbee.a") for key in attributes)


def test_a_caller_cannot_replace_or_inflate_a_request_field() -> None:
    created = record(logging.INFO, "m")
    created.attributes = {
        "url.path": "/evil?q=QSTRSECRET",
        "http.route": "é" * 16_000,
        "lightbee.note": "é" * 16_000,
    }
    attributes = _format_in_a_request(created)["attributes"]
    assert attributes["url.path"] == "/api/x"
    assert len(attributes["http.route"]) <= 256


def test_the_logged_path_is_printable_ascii(capsys: pytest.CaptureFixture[str]) -> None:
    override = chr(0x202E)  # right-to-left override, built so the source stays plain
    assert clean_path(f"/api/a\nb{override}c") == "/api/a?b?c"
    assert clean_path("/x" * 200) == ("/x" * 200)[:256]
    app = create_app(Settings())
    capsys.readouterr()
    with TestClient(app, base_url="https://testserver") as client:
        client.get("/api/%0A%E2%80%AE/x", headers={"X-Request-Id": "rq-path"})
    (line,) = [
        x for x in lines(capsys.readouterr().out) if x["attributes"].get(ID_KEY) == "rq-path"
    ]
    assert line["attributes"]["url.path"] == "/api/??/x"
