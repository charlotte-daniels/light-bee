"""The JSON API the interface uses. Everything under /api needs a session.

Every request body is validated at the boundary with explicit bounds and
unknown fields refused. Every call that changes state must carry the
`X-Light-Bee: 1` header, which a cross-site form cannot send and a cross-site
script cannot send without a preflight this app never answers; with the
SameSite=Strict cookie that closes cross-site request forgery. Errors are short
and generic; nothing from the UDL beyond a sanitised reason ever reaches the
browser, and credentials never do.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

from lightbee.analysis.orbit import orbit_series
from lightbee.analysis.screen import (
    DEFAULT_SCREEN_KM,
    MAX_SCAN_TIMES,
    SGP4_UNCERTAINTY_KM,
    ScreenError,
    scan,
)
from lightbee.analysis.series import Series
from lightbee.analysis.triage import summary
from lightbee.figures.board import board_figure
from lightbee.figures.causes import CausesInputs, causes_figure
from lightbee.figures.common import Caption, rounded
from lightbee.figures.seasonal import seasonal_figure
from lightbee.udl.client import Credentials, UdlAuthError, UdlError
from lightbee.udl.queries import DEFAULT_ELSET_SOURCE
from lightbee.udl.records import MAX_SAT_NO
from lightbee.web import limits
from lightbee.web.runs import (
    RELEASED_MESSAGE,
    ClientFactory,
    Run,
    RunCancelledError,
    RunReleasedError,
    RunRequest,
    add_names,
    analysis,
    audit_scan,
    board_of,
    execute,
    flag_of,
    markings,
)
from lightbee.web.session import COOKIE_NAME, Session, SessionLimitError, SessionStore

CSRF_HEADER: Final = "x-light-bee"
EARLIEST_DATE: Final = date(2000, 1, 1)
MAX_SELECTED_TIMES: Final = 50_000
MAX_NEIGHBOURS_SHOWN: Final = 50
SCAN_TIME_LIMIT_S: Final = 15.0
# A scan waits this long at most for its neighbours' names; they are display only.
SCAN_NAMES_WAIT_S: Final = 5.0
_NAME_POOL: Final = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lightbee-scan-names")
_LOG: Final = logging.getLogger("lightbee.api")
# Runs with a scan's names read in flight; at most one each.
_NAMING: Final[set[str]] = set()
_NAMING_LOCK: Final = threading.Lock()
# Cached photometry and analyses across all sessions; the oldest go first.
MEMORY_BUDGET_BYTES: Final = 900 * 1024 * 1024
PROVIDER_PATTERN: Final = r"^[A-Za-z0-9 _.\-]{1,64}$"

Submit = Callable[[Callable[[], None]], None]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SignIn(_Body):
    username: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[!-9;-~]+$")]
    password: Annotated[StrictStr, Field(min_length=1, max_length=256)]


class RunBody(_Body):
    sat_no: Annotated[StrictInt, Field(ge=1, le=MAX_SAT_NO)]
    change_date: Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    window_days: Annotated[StrictInt, Field(ge=1, le=31)] = 7
    provider: Annotated[StrictStr, Field(pattern=PROVIDER_PATTERN)] = DEFAULT_ELSET_SOURCE
    history_years: Annotated[StrictFloat | StrictInt, Field(ge=0.25, le=2.0)] = 2.0
    threshold_km: Annotated[StrictFloat | StrictInt, Field(ge=1.0, le=2000.0)] = DEFAULT_SCREEN_KM
    # The approximate change time on the change date, HH:MM UTC; blank for no board.
    change_time: Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] | None = None


class ScanBody(_Body):
    times_ms: Annotated[
        list[Annotated[StrictInt, Field(ge=0, le=4_102_444_800_000)]],
        Field(min_length=1, max_length=MAX_SELECTED_TIMES),
    ]
    threshold_km: Annotated[StrictFloat | StrictInt, Field(ge=1.0, le=2000.0)] = DEFAULT_SCREEN_KM


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class Services:
    """What the routes need, held on app.state so handlers stay plain functions."""

    store: SessionStore
    make_client: ClientFactory
    submit: Submit
    clock: Callable[[], datetime]
    runs: limits.RateLimiter = field(default_factory=lambda: limits.RateLimiter(limits.RUNS))
    scans: limits.RateLimiter = field(default_factory=lambda: limits.RateLimiter(limits.SCANS))
    sign_in_failures: limits.RateLimiter = field(
        default_factory=lambda: limits.RateLimiter(limits.SIGN_IN_FAILURES)
    )
    # Hashes of usernames that have signed in successfully since the process
    # started; they are not held back by the shared failure tier.
    known_users: set[str] = field(default_factory=set)


def install(app: FastAPI, services: Services) -> None:
    """Add the API routes and their error handling to the app."""
    app.state.services = services
    # Starlette types every handler as taking Exception; each here is registered
    # only for its own class, so the narrower parameter is safe.
    app.add_exception_handler(ApiError, _api_error)  # type: ignore[arg-type]
    app.add_exception_handler(RunReleasedError, _released)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _invalid)  # type: ignore[arg-type]
    app.include_router(router)


def _api_error(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse({"error": error.message}, status_code=error.status)


def _released(request: Request, error: RunReleasedError) -> JSONResponse:
    # A release that lands while a request is being answered.
    return JSONResponse({"error": RELEASED_MESSAGE}, status_code=410)


FIELD_MESSAGES: Final = {
    "sat_no": "Enter a NORAD ID from 1 to 99999.",
    "change_date": "Choose a change date no later than today (UTC).",
    "window_days": "The window must be 1 to 31 days.",
    "provider": "Choose an elset provider from the list.",
    "history_years": "History must be a quarter of a year to two years.",
    "threshold_km": "The screening distance must be 1 to 2,000 km.",
    "change_time": "Enter the change time as HH:MM (UTC), no later than now, or leave it blank.",
    "times_ms": "Select between 1 and 50,000 observations inside the window.",
}
BOARD_MIN_WINDOW_DAYS: Final = 3
BOARD_WINDOW_MESSAGE: Final = "With a change time, the window must be at least 3 days."
SIGN_IN_MESSAGE: Final = "Enter your UDL username and password."
MAX_KNOWN_USERS: Final = 1024
UNDERSTOOD: Final = "The request was not understood."


def _invalid(request: Request, error: RequestValidationError) -> JSONResponse:
    # Field names and fixed sentences only: the rejected values are never echoed.
    sign_in_fields = set(SignIn.model_fields)
    known = set(FIELD_MESSAGES) | sign_in_fields
    # Only the models' own field names go back; any other key becomes "body".
    fields = sorted({_field_name(e.get("loc", ()), known) for e in error.errors()})
    message = " ".join(
        FIELD_MESSAGES.get(f, SIGN_IN_MESSAGE if f in sign_in_fields else UNDERSTOOD)
        for f in fields[:2]
    )
    return JSONResponse({"error": message or UNDERSTOOD, "fields": fields}, 422)


def _field_name(location: Any, known: set[str]) -> str:
    name = str(location[1]) if len(location) > 1 else ""
    return name if name in known else "body"


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def _guarded(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != "1":
        raise ApiError(403, "Request refused.")


def _session(request: Request) -> Session:
    session = _services(request).store.get(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise ApiError(401, "Please sign in.")
    if request.method != "GET":
        _guarded(request)
    return session


Current = Annotated[Session, Depends(_session)]
router = APIRouter(prefix="/api")


@router.get("/session")
def who(request: Request) -> dict[str, Any]:
    services = _services(request)
    signed_in = services.store.get(request.cookies.get(COOKIE_NAME)) is not None
    # The server's date, so the form's default and limit never disagree with
    # the server near midnight UTC.
    return {"signed_in": signed_in, "today": services.clock().astimezone(UTC).date().isoformat()}


@router.post("/session", dependencies=[Depends(_guarded)])
def sign_in(body: SignIn, request: Request, response: Response) -> dict[str, Any]:
    services = _services(request)
    # Failed attempts are counted per username (hashed) and across the
    # service, which bounds the UDL checks a stranger can cause. A username
    # that has already signed in is held only by its own count, so a flood of
    # random usernames cannot lock returning analysts out.
    user_key = hashlib.sha256(body.username.encode()).hexdigest()
    known = user_key in services.known_users
    if services.sign_in_failures.blocked(user_key, shared=not known):
        raise ApiError(429, "Too many sign-in attempts; wait a few minutes.")
    # The username pattern already excludes the colon Basic auth cannot carry.
    credentials = Credentials(body.username, body.password)
    try:
        _check_credentials(services.make_client, credentials, services.clock())
    except ApiError as error:
        if error.status == 401:
            services.sign_in_failures.record(user_key)
        raise
    if len(services.known_users) < MAX_KNOWN_USERS:
        services.known_users.add(user_key)
    # A new sign-in replaces the session this browser already had.
    services.store.remove(request.cookies.get(COOKIE_NAME))
    try:
        token, _ = services.store.create(credentials)
    except SessionLimitError as error:
        raise ApiError(503, "Light Bee is at its session limit; try again later.") from error
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=True, samesite="strict", path="/")
    return {"signed_in": True}


@router.delete("/session", dependencies=[Depends(_guarded)])
def sign_out(request: Request, response: Response) -> dict[str, Any]:
    _services(request).store.remove(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return {"signed_in": False}


@router.post("/runs", status_code=202)
def start(body: RunBody, session: Current, request: Request) -> dict[str, Any]:
    services = _services(request)
    run_request = _run_request(body, services.clock())
    run = Run(secrets.token_urlsafe(12), run_request, session.audit_id, time.monotonic())
    with session.lock:
        current = session.run
        if current is not None and not current.finished:
            raise ApiError(409, "A run is already in progress.")
        if not services.runs.admit(session.key):
            raise ApiError(429, "Too many runs this hour; wait before starting another.")
        session.run = run
    cached = session.recall(run_request.cache_key)

    def job() -> None:
        data = execute(run, session.credentials, services.make_client, cached, services.clock())
        if data is not None:
            session.remember(run_request.cache_key, data)
        services.store.enforce_budget(MEMORY_BUDGET_BYTES, session)

    services.submit(job)
    return run.snapshot()


def _run_of(run_id: str, session: Session, *, with_data: bool = True) -> Run:
    run = session.run
    if run is None or not run_id.isascii() or not secrets.compare_digest(run.id, run_id[:64]):
        raise ApiError(404, "No such run.")
    if with_data and run.released:
        raise ApiError(410, RELEASED_MESSAGE)
    return run


@router.get("/runs/{run_id}")
def state(run_id: str, session: Current) -> dict[str, Any]:
    return _run_of(run_id, session, with_data=False).snapshot()


Basis = Literal["normalised", "raw"]


@router.get("/runs/{run_id}/flag")
def flag(run_id: str, session: Current, basis: Basis = "normalised") -> dict[str, Any]:
    run = _run_of(run_id, session)
    found = flag_of(run, "auto" if basis == "normalised" else "raw")
    if found is None:
        if run.released:
            raise RunReleasedError
        if run.stage == "failed":
            raise ApiError(409, run.message or "The run failed.")
        raise ApiError(409, "Still reading the same season of earlier years.")
    return _flag_body(found)


@router.get("/runs/{run_id}/figures/seasonal")
def seasonal(
    run_id: str, session: Current, request: Request, basis: Basis = "normalised"
) -> dict[str, Any]:
    run = _run_of(run_id, session)
    recent = _result(run, "recent")
    _, end = run.window_ms()
    caption = _caption(run, _services(request).clock())
    found = analysis(run, "auto" if basis == "normalised" else "raw")
    if found is None:
        early = flag_of(run, "auto" if basis == "normalised" else "raw")
        data = run.data
        if basis == "raw" and data is not None and data.photometry is not None:
            # Before the history arrives, the recent panel still honours the toggle.
            recent = Series.of(data.photometry, normalised=False)
        return seasonal_figure(recent, None, early, caption, end)
    return seasonal_figure(found.series, found.grid, found.flag, caption, end)


@router.get("/runs/{run_id}/figures/causes")
def causes(
    run_id: str,
    session: Current,
    request: Request,
    basis: Basis = "normalised",
    ghosts: bool = False,
) -> dict[str, Any]:
    run = _run_of(run_id, session)
    _result(run, "recent")
    names, marking = _names_and_marking(run)
    caption = _caption(run, _services(request).clock(), marking)
    # Ghost years come from the history already in memory; nothing is re-read.
    return causes_figure(_causes_inputs(run, basis, names), caption, ghosts=ghosts)


@router.get("/runs/{run_id}/board")
def triage_board(run_id: str, session: Current, request: Request) -> dict[str, Any]:
    """The anchored triage board: headline, verdict cards and the 72-hour strip."""
    run = _run_of(run_id, session)
    if run.request.anchor is None:
        raise ApiError(404, "This run has no change time, so it has no board.")
    waiting = "The close approaches are still being screened."
    _result(run, "causes", waiting=waiting)
    found = board_of(run)
    if found is None:
        if run.released:
            raise RunReleasedError
        raise ApiError(409, waiting)
    names, marking = _names_and_marking(run)
    caption = _caption(run, _services(request).clock(), marking)
    return {
        "headline": found.headline,
        "lead": found.lead,
        "summary": summary(found.sensors, found.anchor_ms),
        "cards": [
            {"key": c.key, "state": c.state, "title": c.title, "body": c.body} for c in found.cards
        ],
        "figure": board_figure(found, caption, names),
        "marking": marking,
    }


@router.post("/runs/{run_id}/scan")
def neighbourhood(
    run_id: str, body: ScanBody, session: Current, request: Request
) -> dict[str, Any]:
    run = _run_of(run_id, session)
    waiting = "Elsets and the GEO snapshot are still being read."
    targets = _result(run, "elsets", waiting=waiting)
    data = run.data
    if data is None and run.released:
        raise RunReleasedError
    # Ready once the close approaches are screened and named, while the rest of
    # the history is still being read: the scan needs only elsets and the snapshot.
    with run.lock:
        screened = "causes" in run.results
    if not screened or data is None:
        raise ApiError(409, waiting)
    start_ms, end_ms = run.window_ms()
    if any(not start_ms <= t <= end_ms for t in body.times_ms):
        raise ApiError(400, "The selection must lie inside the run's window.")
    with session.lock:
        if session.scanning:
            raise ApiError(409, "A scan is already running.")
        if not _services(request).scans.admit(session.key):
            raise ApiError(429, "Too many scans this hour; wait before scanning again.")
        session.scanning = True
    began = time.monotonic()
    try:
        result = scan(
            body.times_ms,
            targets,
            data.catalogue,
            threshold_km=float(body.threshold_km),
            deadline=began + SCAN_TIME_LIMIT_S,
        )
        listed = {n.sat_no for n in result.neighbours[:MAX_NEIGHBOURS_SHOWN]}
        _name_neighbours(request, session, run, listed - data.names.keys(), began)
    except ScreenError as error:
        raise ApiError(400, str(error)) from error
    finally:
        with session.lock:
            session.scanning = False
    audit_scan(
        session.audit_id,
        run,
        int(result.times_ms.size),
        len(result.neighbours),
        time.monotonic() - began,
    )
    # The marking covers every record read so far, the names just read included.
    names, marking = _names_and_marking(run)
    return {**_scan_body(result, names), "marking": marking}


def _name_neighbours(
    request: Request, session: Session, run: Run, missing: set[int], began: float
) -> None:
    """Names for a scan's listed neighbours not already held: one small read.

    The scan waits for it at most SCAN_NAMES_WAIT_S, and never past its own
    time limit. A slow read finishes in the background (two at most at once)
    and its names are there for the next scan; this one answers without them.
    """
    data = run.data
    wait = min(SCAN_NAMES_WAIT_S, began + SCAN_TIME_LIMIT_S - time.monotonic())
    if not missing or data is None or wait <= 0:
        return
    with _NAMING_LOCK:
        if run.id in _NAMING:  # one names read per run at a time; the queue cannot grow
            return
        _NAMING.add(run.id)
    make_client = _services(request).make_client
    credentials = session.credentials

    def read() -> None:
        try:
            # Sign-out or expiry cancels the run: a read not yet started never
            # starts, and one under way stops before its next request.
            if not run.cancel.is_set():
                with make_client(credentials) as client:
                    add_names(data, run, client, missing, run.check)
        finally:
            with _NAMING_LOCK:
                _NAMING.discard(run.id)

    future = _NAME_POOL.submit(contextvars.copy_context().run, read)
    try:
        future.result(timeout=wait)
    except FutureTimeoutError:
        _LOG.info("neighbour names were not ready; the scan answers without them")
    except RunCancelledError:
        _LOG.info("the session ended while neighbour names were being read")


def _check_credentials(make_client: ClientFactory, credentials: Credentials, now: datetime) -> None:
    """One small read, so a wrong password is caught at sign-in, not mid-run."""
    window = f"{(now - timedelta(days=1)):%Y-%m-%dT%H:%M:%S.000Z}..{now:%Y-%m-%dT%H:%M:%S.000Z}"
    params = {"columns": "classificationMarking,satNo", "epoch": window, "maxResults": "1"}
    try:
        with make_client(credentials) as client:
            client.read("/udl/elset/tuple", params)
    except UdlAuthError as error:
        raise ApiError(401, "The UDL did not accept those credentials.") from error
    except UdlError as error:
        raise ApiError(502, "The UDL could not be reached to check those credentials.") from error


def _run_request(body: RunBody, now: datetime) -> RunRequest:
    try:
        change = date.fromisoformat(body.change_date)
    except ValueError as error:
        raise ApiError(422, FIELD_MESSAGES["change_date"]) from error
    if not EARLIEST_DATE <= change <= now.astimezone(UTC).date():
        raise ApiError(422, FIELD_MESSAGES["change_date"])
    at = None if body.change_time is None else dt_time.fromisoformat(body.change_time)
    made = RunRequest(
        body.sat_no,
        change,
        body.window_days,
        body.provider,
        body.history_years,
        body.threshold_km,
        at,
    )
    if made.anchor is not None and made.anchor > now:
        raise ApiError(422, FIELD_MESSAGES["change_time"])
    # The board looks 24 hours before the time and 48 after, and the window ends
    # 48 hours after it, so a shorter window would leave the strip part-screened.
    if made.anchor is not None and made.window_days < BOARD_MIN_WINDOW_DAYS:
        raise ApiError(422, BOARD_WINDOW_MESSAGE)
    return made


def _result(run: Run, name: str, waiting: str = "Still reading.") -> Any:
    with run.lock:
        value = run.results.get(name)
    if value is None:
        if run.released:
            raise RunReleasedError
        if run.stage == "failed":
            raise ApiError(409, run.message or "The run failed.")
        raise ApiError(409, waiting)
    return value


def _caption(run: Run, now: datetime, marking: str | None = None) -> Caption:
    start, end = run.window_ms()
    first = datetime.fromtimestamp(start / 1000, UTC)
    last = datetime.fromtimestamp((end - 1) / 1000, UTC)
    return Caption(
        marking=markings(run.data) if marking is None else marking,
        sat_no=run.request.sat_no,
        window=f"{first:%Y-%m-%d} to {last:%Y-%m-%d} ({run.request.window_days} days)",
        source=f"UDL EO observations; {run.request.provider} elsets",
        generated=now,
    )


def _names_and_marking(run: Run) -> tuple[dict[int, str], str]:
    """A copy of the run's names and the marking that covers them, taken together.

    A names read finishing in the background merges under the run's lock, so
    a name is never shown under a banner that lacks its record's controls.
    """
    with run.lock:
        data = run.data
        return (dict(data.names) if data is not None else {}), markings(data)


def _causes_inputs(run: Run, basis: Basis, names: Mapping[int, str]) -> CausesInputs:
    with run.lock:
        results = dict(run.results)
    found = analysis(run, "auto" if basis == "normalised" else "raw")
    light: Series = found.series if found is not None else _result(run, "recent")
    start, end = run.window_ms()
    elsets = results.get("elsets")
    orbit = (
        orbit_series(elsets, run.request.provider, start_ms=start, end_ms=end) if elsets else None
    )
    return CausesInputs(
        light=light,
        history=found.series if found is not None else None,
        orbit=orbit,
        approaches=results.get("approaches", []),
        window=(start, end),
        threshold_km=run.request.threshold_km,
        approaches_note=results.get("_approaches_note", ""),
        names=names,
    )


def _flag_body(flag: Any) -> dict[str, Any]:
    context = flag.context
    return {
        "outcome": flag.outcome,
        "headline": flag.headline,
        "detail": flag.detail,
        "marginal": flag.marginal,
        "comparable_bins": flag.comparable_bins,
        "median_delta_mag": rounded([flag.median_delta_mag], 3)[0],
        "median_robust_z": rounded([flag.median_robust_z], 2)[0],
        "years_used": list(context.years_used),
        "recent_longitude_deg": rounded([context.recent_longitude_deg], 2)[0],
        "reference_longitude_deg": rounded([context.reference_longitude_deg], 2)[0],
        "recent_nights": context.recent_nights,
        "reference_nights": context.reference_nights,
        "bins": {
            "sepa_centre": rounded(flag.bins.sepa_centre, 1),
            "recent": rounded(flag.bins.recent, 3),
            "reference": rounded(flag.bins.reference, 3),
            "robust_z": rounded(flag.bins.robust_z, 2),
        },
    }


def _scan_body(result: Any, names: Mapping[int, str]) -> dict[str, Any]:
    return {
        "times_ms": [int(t) for t in result.times_ms],
        "thinned": result.thinned,
        "max_times": MAX_SCAN_TIMES,
        "uncertainty_km": SGP4_UNCERTAINTY_KM,
        "threshold_km": result.threshold_km,
        "screened": result.screened,
        "neighbours": [
            {
                "sat_no": n.sat_no,
                "name": names.get(n.sat_no, ""),
                "min_km": round(n.min_km, 1),
                "min_at_ms": n.min_at_ms,
                "inside": n.inside,
                "borderline": n.borderline,
                "elset_age_hours": round(n.elset_age_hours, 1),
                "separations_km": rounded(n.separations_km, 1),
            }
            for n in result.neighbours[:MAX_NEIGHBOURS_SHOWN]
        ],
        "more": max(0, len(result.neighbours) - MAX_NEIGHBOURS_SHOWN),
    }
