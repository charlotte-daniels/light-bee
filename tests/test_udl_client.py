"""The UDL client: GET only, allowlisted, no redirects, polite under pressure, never leaks."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from urllib.parse import quote

import httpx2
import pytest

from lightbee.config import Settings
from lightbee.udl.client import (
    ALLOWED_PATHS,
    MAX_BODY_BYTES,
    MAX_RETRY_AFTER_SECONDS,
    RATE_LIMIT_ATTEMPTS,
    SERVER_ERROR_ATTEMPTS,
    Credentials,
    Throttle,
    UdlAuthError,
    UdlError,
    UdlReadClient,
    UdlRequestError,
    UdlSliceTooLargeError,
)

CREDENTIALS = Credentials("analyst", "not-real-pw")
PATH = "/udl/eoobservation/tuple"


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response], sleeps: list[float] | None = None
) -> UdlReadClient:
    return UdlReadClient(
        Settings(),
        CREDENTIALS,
        transport=httpx2.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _ok(body: object = ()) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda request: httpx2.Response(200, json=list(body))  # type: ignore[call-overload]  # test body of any shape


def test_a_read_returns_the_records_and_sends_basic_auth_over_https() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=[{"obTime": "2026-11-01T00:00:00.000Z"}])

    with _client(handler) as client:
        assert client.read(PATH, {"satNo": "43874"}) == [{"obTime": "2026-11-01T00:00:00.000Z"}]
    (request,) = seen
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "unifieddatalibrary.com"
    assert request.headers["authorization"] == CREDENTIALS.authorization()
    assert request.content == b""


def test_paths_off_the_allowlist_are_refused_before_any_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request should be sent")

    client = _client(handler)
    for path in (
        "/udl/eoobservation",
        "/udl/elset/createBulk",
        "/filedrop/udl-eo",
        "/udl/eoobservation/queryhelp",
        "https://evil.test/x",
    ):
        with pytest.raises(UdlRequestError):
            client.read(path, {})
    with pytest.raises(UdlRequestError, match="parameter"):
        client.read(PATH, {"satNo": "1", "orderBy": "x"})


def test_every_allowlisted_path_is_a_read_endpoint() -> None:
    # The plain on-orbit path is the UDL's query endpoint for that collection,
    # read with GET like the others; it is named exactly, not by pattern.
    plain_queries = {"/udl/onorbit"}
    for path in ALLOWED_PATHS:
        assert path.startswith("/udl/")
        assert path in plain_queries or path.rsplit("/", 1)[-1] in {"tuple", "history"}


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_refused_not_followed(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(status, headers={"Location": "https://evil.test/steal"})

    with pytest.raises(UdlRequestError, match="redirect"):
        _client(handler).read(PATH, {})
    assert len(calls) == 1
    assert "evil.test" not in calls[0]


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_raise_an_auth_error(status: int) -> None:
    with pytest.raises(UdlAuthError):
        _client(lambda request: httpx2.Response(status)).read(PATH, {})


def test_a_429_waits_retries_and_halves_the_requests_in_flight() -> None:
    answers = iter([httpx2.Response(429), httpx2.Response(200, json=[{"a": 1}])])
    client = _client(lambda request: next(answers))
    assert client.read(PATH, {}) == [{"a": 1}]
    assert client.throttle.limit == 12


def test_persistent_429_gives_up_with_a_plain_error() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(429)

    client = _client(handler)
    with pytest.raises(UdlError, match="429"):
        client.read(PATH, {})
    assert calls == RATE_LIMIT_ATTEMPTS
    assert client.rate_limited == RATE_LIMIT_ATTEMPTS
    assert client.throttle.limit == 1


def test_retry_after_is_honoured_within_a_cap() -> None:
    answers = iter(
        [
            httpx2.Response(429, headers={"Retry-After": "7"}),
            httpx2.Response(429, headers={"Retry-After": "9999"}),
            httpx2.Response(429, headers={"Retry-After": "soon"}),
            httpx2.Response(200, json=[]),
        ]
    )
    sleeps: list[float] = []
    _client(lambda request: next(answers), sleeps).read(PATH, {})
    assert sleeps == [7.0, MAX_RETRY_AFTER_SECONDS, 5.0]


def test_a_burst_of_simultaneous_429s_halves_the_limit_once() -> None:
    barrier = threading.Barrier(24)
    seen = 0
    lock = threading.Lock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal seen
        with lock:
            seen += 1
            first_round = seen <= 24
        if first_round:
            barrier.wait(timeout=5)
            return httpx2.Response(429)
        return httpx2.Response(200, json=[])

    client = _client(handler)
    threads = [threading.Thread(target=client.read, args=(PATH, {})) for _ in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert client.rate_limited == 24
    assert client.throttle.limit == 12


def test_repeated_server_errors_ask_the_caller_to_divide_the_window() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500)

    with pytest.raises(UdlSliceTooLargeError):
        _client(handler).read(PATH, {})
    assert calls == SERVER_ERROR_ATTEMPTS


def test_a_server_error_that_clears_is_retried() -> None:
    answers = iter([httpx2.Response(503), httpx2.Response(200, json=[])])
    assert _client(lambda request: next(answers)).read(PATH, {}) == []


def test_a_timeout_asks_the_caller_to_divide_the_window() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(UdlSliceTooLargeError):
        _client(handler).read(PATH, {})


@pytest.mark.parametrize("status", [400, 404, 406, 418])
def test_other_client_errors_are_refusals(status: int) -> None:
    with pytest.raises(UdlRequestError, match=str(status)):
        _client(lambda request: httpx2.Response(status)).read(PATH, {})


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"records": []}),
        httpx2.Response(200, json=[1, 2]),
        httpx2.Response(200, text="[" * 100_000 + "]" * 100_000),
    ],
)
def test_a_body_that_is_not_a_list_of_records_is_an_error(response: httpx2.Response) -> None:
    with pytest.raises(UdlError):
        _client(lambda request: response).read(PATH, {})


def test_credentials_never_appear_in_errors_or_reprs() -> None:
    secret = Credentials("analyst", "not-real-but-distinctive-pw")
    token = secret.authorization().split()[1]

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"failed for {request.url} {request.headers}", request=request)

    client = UdlReadClient(Settings(), secret, transport=httpx2.MockTransport(handler))
    with pytest.raises(UdlError) as raised:
        client.read(PATH, {})
    message = str(raised.value) + repr(raised.value) + repr(secret) + repr(client._http)
    for leak in ("not-real-but-distinctive-pw", token, "analyst"):
        assert leak not in message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_an_oversized_body_asks_for_a_smaller_window() -> None:
    body = b"[" + b'{"a":1},' * (MAX_BODY_BYTES // 8) + b'{"a":1}]'
    with pytest.raises(UdlSliceTooLargeError, match="larger"):
        _client(lambda request: httpx2.Response(200, content=body)).read(PATH, {})


def test_more_records_than_were_asked_for_is_refused() -> None:
    page = [{"a": n} for n in range(11)]
    with pytest.raises(UdlError, match="more records"):
        _client(lambda request: httpx2.Response(200, json=page)).read(PATH, {"maxResults": "10"})
    with pytest.raises(UdlRequestError, match="maxResults"):
        _client(_ok()).read(PATH, {"maxResults": "999999"})


def test_proxy_and_certificate_settings_are_not_taken_from_the_environment() -> None:
    assert _client(_ok())._http.trust_env is False


@pytest.mark.parametrize(
    ("username", "password"), [("", "pw-value"), ("analyst", ""), ("a:b", "pw-value")]
)
def test_incomplete_or_malformed_credentials_are_refused(username: str, password: str) -> None:
    with pytest.raises(ValueError, match="UDL"):
        Credentials(username, password)


def test_the_throttle_never_lets_more_than_its_limit_run() -> None:
    throttle = Throttle(2)
    running = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal running, peak
        throttle.acquire()
        try:
            with lock:
                running += 1
                peak = max(peak, running)
            threading.Event().wait(0.05)
            with lock:
                running -= 1
        finally:
            throttle.release()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 2


def test_the_throttle_halves_once_per_level_and_its_floor_is_one() -> None:
    throttle = Throttle(8)
    level = throttle.acquire()
    throttle.release()
    throttle.back_off(level)
    throttle.back_off(level)
    assert throttle.limit == 4
    single = Throttle(1)
    single.back_off(1)
    assert single.limit == 1
    with pytest.raises(ValueError, match="at least 1"):
        Throttle(0)


@pytest.mark.parametrize("header", ["\u00b2", "\u0663", "-1", "1.5", ""])
def test_a_malformed_retry_after_falls_back_to_the_normal_wait(header: str) -> None:
    raw = [(b"Retry-After", header.encode("utf-8"))]
    answers = iter([httpx2.Response(429, headers=raw), httpx2.Response(200, json=[])])
    sleeps: list[float] = []
    _client(lambda request: next(answers), sleeps).read(PATH, {})
    assert sleeps == [1.0]


@pytest.mark.parametrize("value", ["\u00b2", "\u0663", "0", "25001", " 10"])
def test_max_results_must_be_plain_ascii_digits_in_range(value: str) -> None:
    with pytest.raises(UdlRequestError, match="maxResults"):
        _client(_ok()).read(PATH, {"maxResults": value})


@pytest.mark.parametrize("cap", [0, -1, 2 * 1024**3])
def test_the_body_cap_is_bounded(cap: int) -> None:
    with pytest.raises(ValueError, match="max_body_bytes"):
        UdlReadClient(Settings(), CREDENTIALS, max_body_bytes=cap)


def test_a_larger_body_cap_and_environment_proxies_are_opt_in() -> None:
    body = json.dumps([{"a": "x" * 20_000}] * 1_000).encode()  # about 20 MB, 1,000 records
    assert len(body) > MAX_BODY_BYTES
    client = UdlReadClient(
        Settings(),
        CREDENTIALS,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, content=body)),
        max_body_bytes=4 * MAX_BODY_BYTES,
        trust_env=True,
    )
    assert len(client.read(PATH, {})) > 1
    assert client._http.trust_env is True


def test_a_rejection_keeps_a_short_sanitised_explanation_out_of_the_message() -> None:
    body = "Invalid query parameter: source\x00\x1b[31m " + "x" * 5000
    with pytest.raises(UdlRequestError) as raised:
        _client(lambda request: httpx2.Response(400, text=body)).read(PATH, {})
    error = raised.value
    assert str(error) == "the UDL rejected the request (HTTP 400)"
    assert error.detail.startswith("Invalid query parameter: source [31m")
    assert len(error.detail) <= 300
    assert all(" " <= c <= "~" for c in error.detail)


def test_other_failures_carry_no_detail() -> None:
    with pytest.raises(UdlRequestError) as raised:
        _client(lambda request: httpx2.Response(302, headers={"Location": "/x"})).read(PATH, {})
    assert raised.value.detail == ""


def _echo(request: httpx2.Request) -> httpx2.Response:
    """An upstream that echoes the request, header and all, in its rejection."""
    header = request.headers["Authorization"]
    body = (
        f"Bad request; headers: Authorization={header}; url={request.url}; "
        # Deliberately fake credentials, echoed to prove they are redacted.
        f'{{"user": "analyst", "password": "not-real-pw", '  # secrets-gate: not-a-secret
        f'"token": "abc123xyz"}} '  # secrets-gate: not-a-secret
        f"Bearer fake-abc api_key=k-12345"  # secrets-gate: not-a-secret
    )
    return httpx2.Response(400, text=body)


def test_an_echoed_request_never_carries_credentials_into_the_detail() -> None:
    with pytest.raises(UdlRequestError) as raised:
        _client(_echo).read(PATH, {"satNo": "25544"})
    detail = raised.value.detail
    token = CREDENTIALS.authorization().removeprefix("Basic ")
    for leaked in (token, "not-real-pw", "analyst", "abc123xyz", "fake-abc", "k-12345"):
        assert leaked not in detail
    assert "[REDACTED]" in detail
    assert "satNo=25544" in detail  # the request's own non-secret detail stays useful


def test_a_secret_split_by_the_byte_limit_cannot_survive_in_part() -> None:
    token = CREDENTIALS.authorization().removeprefix("Basic ")
    filler = " " * (1024 - 10)
    with pytest.raises(UdlRequestError) as raised:
        _client(lambda request: httpx2.Response(400, text="Rejected" + filler + token)).read(
            PATH, {}
        )
    assert token[:6] not in raised.value.detail
    assert raised.value.detail == "Rejected"


TRICKY = Credentials("analyst-name", 'n0t/Real&pw"x?')  # secrets-gate: not-a-secret


def _echoed(transform: Callable[[httpx2.Request], str]) -> str:
    client = UdlReadClient(
        Settings(),
        TRICKY,
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(400, text=transform(request))
        ),
        sleep=lambda _: None,
    )
    with pytest.raises(UdlRequestError) as raised:
        client.read(PATH, {})
    return raised.value.detail


@pytest.mark.parametrize(
    "echo",
    [
        lambda r: "rejected " + quote("Authorization: " + r.headers["Authorization"]),
        lambda r: "rejected auth=" + quote(r.headers["Authorization"].split()[1], safe=""),
        lambda r: "rejected " + r.headers["Authorization"].split()[1].rstrip("="),
        lambda r: 'rejected {"pair": "analyst-name:n0t/Real&pw\\"x?"}',
        lambda r: "rejected <p>n0t/Real&amp;pw&quot;x?</p>",
        lambda r: "rejected " + quote(quote(r.headers["Authorization"], safe=""), safe=""),
    ],
    ids=["url-encoded", "percent-token", "unpadded", "json-escaped", "html-escaped", "double"],
)
def test_encoded_echoes_of_the_credentials_are_redacted(
    echo: Callable[[httpx2.Request], str],
) -> None:
    detail = _echoed(echo)
    token = TRICKY.authorization().split()[1]
    assert "[REDACTED]" in detail
    for fragment in (
        token[:8],
        token[8:16],
        token.rstrip("=")[-8:],
        "n0t/Real",
        "Real&pw",
        'pw"x?',
    ):
        assert fragment not in detail, (fragment, detail)


def test_a_useful_rejection_reason_survives_redaction() -> None:
    reason = '{"message":"Column line1 is not available to select","status":"error"}'
    assert _echoed(lambda r: reason) == reason


def test_a_go_style_or_backslash_escaped_echo_of_the_password_is_redacted() -> None:
    go_style = 'bad "analyst-name:n0t/Real\\u0026pw\\u0022x?"'  # secrets-gate: not-a-secret
    backslashed = 'bad analyst-name:n0t\\/Real\\&pw\\"x\\?'  # secrets-gate: not-a-secret
    for body in (go_style, backslashed):
        detail = _echoed(lambda r, text=body: text)
        assert "pw" not in detail, detail
        assert "Real" not in detail, detail
