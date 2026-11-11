"""Light Bee service check: the whole app, run on your machine against the real UDL.

Read-only. It starts Light Bee locally (on 127.0.0.1 only), signs in with your
UDL login, and drives the same API the page uses, timing each step against the
brief's targets:

1. Cold run, 2 years of history, for each reference object: when the first
   panel, the seasonal flag (target 3 minutes), the heatmap and the whole run
   are ready.
2. The seasonal verdict, its comparable bins and median offset, for you to
   compare with what you expect for each object.
3. Each figure: build time, size and points sent to the browser.
4. Neighbourhood scans on the real GEO catalogue: the last 6 hours of
   observations (a typical drag), and the whole window (the heaviest case,
   thinned to 4,000 instants). Target 15 s.
5. A warm re-run of the same object over a shorter window (target 10 s).

Nothing is stored except a report of counts, timings and verdicts: no
credentials and no observation data. With --serve the app keeps running
afterwards so you can open it in Edge, look at the real figures, export PNGs
and read browser memory (Shift+Esc, the Browser task manager; target 500 MB).

From the repository root (Windows PowerShell shown):

    python -m venv .venv
    .\\.venv\\Scripts\\python.exe -m pip install -r requirements-runtime.txt
    $env:PYTHONPATH = "src"
    .\\.venv\\Scripts\\python.exe tools\\service_check.py --serve

Options: --objects 43874 58204 63157   --change-date YYYY-MM-DD   --port 8080
         --out FILE   --serve   --use-proxy (only if your machine needs a proxy)
"""

from __future__ import annotations

import argparse
import getpass
import html
import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx2
import uvicorn

from lightbee.config import load_settings
from lightbee.udl.client import Credentials, UdlReadClient
from lightbee.web.app import create_app
from lightbee.web.session import COOKIE_NAME

REFERENCE_OBJECTS: Final = (43874, 58204, 63157)
COLD_TARGET_S: Final = 180.0
WARM_TARGET_S: Final = 10.0
SCAN_TARGET_S: Final = 15.0
WINDOW_DAYS: Final = 7
WARM_WINDOW_DAYS: Final = 3
DRAG_HOURS: Final = 6
POLL_S: Final = 0.5
RUN_LIMIT_S: Final = 900.0
HEADERS: Final = {"X-Light-Bee": "1"}

ClientFactory = Callable[[Credentials], UdlReadClient]


@dataclass
class Report:
    # The markings of the data behind the verdicts and figures this report
    # describes; the report carries them, so they travel with it if shared.
    markings: list[str]
    started: str
    change_date: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def mark(self, marking: str) -> None:
        if marking and marking not in self.markings:
            self.markings.append(marking)

    def record(self, name: str, passed: bool, **details: Any) -> None:
        self.checks.append({"check": name, "passed": passed, **details})
        if not passed:
            self.failures.append(name)
        mark = "PASS" if passed else "FAIL"
        summary = ", ".join(f"{k}={v}" for k, v in details.items() if not isinstance(v, dict))
        print(f"  {mark}  {name}: {summary}")


class Service:
    """Light Bee running in this process on 127.0.0.1, and a client for its API."""

    def __init__(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.cookie = ""
        self.base = ""
        # Signs in again; used to cancel a run that outlives the time limit.
        self.renew: Callable[[], None] = lambda: None

    def _serve(self) -> None:
        try:
            self.server.run()
        except SystemExit:
            return  # uvicorn exits when it cannot bind; start() reports it

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("Light Bee did not start; is the port already in use?")
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any, int, float]:
        """Status, parsed JSON, response size and seconds taken."""
        headers = dict(HEADERS)
        if self.cookie:
            # Sent by hand: the cookie is Secure, and this is plain http on loopback.
            headers["Cookie"] = f"{COOKIE_NAME}={self.cookie}"
        began = time.monotonic()
        with httpx2.Client(base_url=self.base, timeout=120, trust_env=False) as client:
            response = client.request(method, path, json=body, headers=headers)
        seconds = time.monotonic() - began
        token = response.cookies.get(COOKIE_NAME)
        if token:
            self.cookie = token
        payload = response.json() if response.content else None
        return response.status_code, payload, len(response.content), seconds


def points(figure: dict[str, Any]) -> int:
    total = 0
    for trace in figure.get("data", []):
        for axis in ("x", "z"):
            values = trace.get(axis)
            if isinstance(values, list):
                total += sum(len(v) if isinstance(v, list) else 1 for v in values)
    return total


def observation_times(figure: dict[str, Any]) -> list[int]:
    times = {
        value
        for trace in figure.get("data", [])
        for value in trace.get("customdata") or []
        if isinstance(value, int)
    }
    return sorted(times)


def wait_for(service: Service, run_id: str, report: Report, name: str) -> dict[str, Any]:
    """Poll a run to its end, noting when each result first appears.

    A run still going at RUN_LIMIT_S is recorded as failed and cancelled by
    signing out and in again, so the next object can start.
    """
    began = time.monotonic()
    first_seen: dict[str, float] = {}
    stages: dict[str, float] = {}
    while True:
        status, state, _, _ = service.call("GET", f"/api/runs/{run_id}")
        if status != 200:
            raise RuntimeError(f"run state answered {status}")
        elapsed = round(time.monotonic() - began, 1)
        stages.setdefault(state["stage"], elapsed)
        for ready in state["ready"]:
            first_seen.setdefault(ready, elapsed)
        if state["stage"] in ("done", "failed") or elapsed > RUN_LIMIT_S:
            state["first_seen_s"] = first_seen
            state["stage_started_s"] = stages
            if state["stage"] != "done":
                report.record(name, False, stage=state["stage"], message=state["message"])
            if state["stage"] not in ("done", "failed"):
                service.renew()
            return dict(state)
        time.sleep(POLL_S)


def check_object(service: Service, report: Report, sat_no: int, change_date: str) -> None:
    body = {"sat_no": sat_no, "change_date": change_date, "window_days": WINDOW_DAYS}
    status, run, _, _ = service.call("POST", "/api/runs", body)
    if status != 202:
        report.record(f"cold_{sat_no}", False, status=status, message=(run or {}).get("error"))
        return
    state = wait_for(service, run["id"], report, f"cold_{sat_no}")
    if state["stage"] != "done":
        return
    seen = state["first_seen_s"]
    report.record(
        f"cold_{sat_no}",
        (seen.get("flag") or COLD_TARGET_S + 1) <= COLD_TARGET_S,
        seconds=state["seconds"],
        target_s=COLD_TARGET_S,
        observations=state["records"],
        first_panel_s=seen.get("recent"),
        flag_s=seen.get("flag"),
        heatmap_s=seen.get("seasonal"),
        stage_started_s=state["stage_started_s"],
    )
    # Both modes drawn: the brief's original measure, reported beside the flag's.
    report.record(
        f"whole_run_{sat_no}",
        state["seconds"] <= COLD_TARGET_S,
        seconds=state["seconds"],
        target_s=COLD_TARGET_S,
    )
    run_path = f"/api/runs/{run['id']}"
    _, flag, _, flag_s = service.call("GET", f"{run_path}/flag")
    report.record(
        f"verdict_{sat_no}",
        flag is not None and "outcome" in flag,
        outcome=flag.get("outcome"),
        headline=flag.get("headline"),
        comparable_bins=flag.get("comparable_bins"),
        median_delta_mag=flag.get("median_delta_mag"),
        median_robust_z=flag.get("median_robust_z"),
        years_used=flag.get("years_used"),
        longitude_now_deg=flag.get("recent_longitude_deg"),
        longitude_then_deg=flag.get("reference_longitude_deg"),
        seconds=round(flag_s, 2),
    )
    causes = check_figures(service, report, sat_no, run_path)
    if causes is not None:
        check_scans(service, report, sat_no, run_path, observation_times(causes))
    warm = {**body, "window_days": WARM_WINDOW_DAYS}
    status, again, _, _ = service.call("POST", "/api/runs", warm)
    if status != 202:
        report.record(f"warm_{sat_no}", False, status=status)
        return
    state = wait_for(service, again["id"], report, f"warm_{sat_no}")
    if state["stage"] == "done":
        report.record(
            f"warm_{sat_no}",
            state["warm"] and state["seconds"] <= WARM_TARGET_S,
            seconds=state["seconds"],
            target_s=WARM_TARGET_S,
            from_cache=state["warm"],
        )


def check_figures(
    service: Service, report: Report, sat_no: int, run_path: str
) -> dict[str, Any] | None:
    causes = None
    for name in ("seasonal", "causes"):
        for basis in ("normalised", "raw"):
            status, figure, size, seconds = service.call(
                "GET", f"{run_path}/figures/{name}?basis={basis}"
            )
            report.record(
                f"figure_{name}_{basis}_{sat_no}",
                status == 200,
                seconds=round(seconds, 2),
                kilobytes=round(size / 1024),
                points=points(figure) if status == 200 else 0,
            )
            if status == 200 and name == "causes" and basis == "normalised":
                causes = figure
            if status == 200:
                notes = figure["layout"].get("annotations") or [{}]
                report.mark(html.unescape(str(notes[0].get("text", ""))))
    return causes


def check_scans(
    service: Service, report: Report, sat_no: int, run_path: str, times: list[int]
) -> None:
    if not times:
        report.record(f"scan_{sat_no}", True, note="no observations in the window to scan")
        return
    drag = [t for t in times if t >= times[-1] - DRAG_HOURS * 3_600_000]
    for label, selection in (("drag_6h", drag), ("whole_window", times)):
        status, result, _, seconds = service.call(
            "POST", f"{run_path}/scan", {"times_ms": selection[-50_000:]}
        )
        neighbours = (result or {}).get("neighbours", [])
        report.record(
            f"scan_{label}_{sat_no}",
            status == 200 and seconds <= SCAN_TARGET_S,
            status=status,
            seconds=round(seconds, 2),
            target_s=SCAN_TARGET_S,
            selected=len(selection),
            instants=len((result or {}).get("times_ms", [])),
            neighbours=len(neighbours),
            inside=sum(1 for n in neighbours if n.get("inside")),
        )


def _arguments(argv: list[str] | None, today: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Light Bee end to end on the real UDL.")
    parser.add_argument("--objects", type=int, nargs="+", default=list(REFERENCE_OBJECTS))
    parser.add_argument("--change-date", default=today)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--out", type=Path, default=Path("service-check-report.json"))
    parser.add_argument("--serve", action="store_true", help="keep the app running afterwards")
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="honour this machine's proxy settings (the deployed app never does)",
    )
    return parser.parse_args(argv)


def run(
    service: Service, args: argparse.Namespace, credentials: Credentials, report: Report
) -> None:
    body = {"username": credentials.username, "password": credentials.password}
    status, answer, _, seconds = service.call("POST", "/api/session", body)
    report.record("sign_in", status == 200, status=status, seconds=round(seconds, 2))
    if status != 200:
        print(f"  Sign-in refused: {(answer or {}).get('error')}")
        return

    def renew() -> None:
        service.call("DELETE", "/api/session")  # cancels the session's run
        service.call("POST", "/api/session", body)

    service.renew = renew
    for sat_no in args.objects:
        print(f"\nNORAD {sat_no}")
        try:
            check_object(service, report, sat_no, args.change_date)
        except (RuntimeError, httpx2.HTTPError) as error:
            report.record(f"object_{sat_no}", False, error=type(error).__name__)


def main(
    argv: list[str] | None = None,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    client_factory: ClientFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    wait: Callable[[], None] | None = None,
) -> int:
    now = (clock or (lambda: datetime.now(UTC)))()
    args = _arguments(argv, now.date().isoformat())
    settings = load_settings()

    def real_client(creds: Credentials) -> UdlReadClient:
        return UdlReadClient(settings, creds, trust_env=args.use_proxy)

    app = create_app(settings, client_factory=client_factory or real_client, clock=clock)
    service = Service(app, args.port)
    try:
        service.start()  # before the password is asked for, so a busy port costs nothing
    except RuntimeError:
        print(f"Port {args.port} is in use on this machine; choose another with --port 8090.")
        return 2
    report = Report(
        markings=[], started=now.isoformat(timespec="seconds"), change_date=args.change_date
    )
    try:
        username = ask("UDL username: ").strip()
        credentials = Credentials(username, ask_secret("UDL password (not shown): "))
        run(service, args, credentials, report)
    except KeyboardInterrupt:
        report.record("interrupted", False, note="stopped with Ctrl+C; the report is partial")
    finally:
        _finish(service, args.out, report)
    try:
        if args.serve and "interrupted" not in report.failures:
            print(
                f"\nLight Bee is running at {service.base.replace('127.0.0.1', 'localhost')}"
                " (this machine only).\nOpen it in Edge, sign in, and run the objects to look at"
                " the real figures and export PNGs. Shift+Esc opens Edge's Browser task manager:"
                " note the memory of the Light Bee tab after both modes are drawn."
                "\nPress Ctrl+C to stop."
            )
            (wait or _wait_forever)()
    finally:
        service.stop()
    return 1 if report.failures else 0


def _finish(service: Service, out: Path, report: Report) -> None:
    """Sign out (which cancels any run still reading) and write whatever was measured."""
    try:
        service.call("DELETE", "/api/session")
    except httpx2.HTTPError:
        print("  The local server did not answer the sign-out; it stops with this script.")
    text = json.dumps(report.__dict__, indent=2)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError:
        print(f"  Could not write {out}; the report follows instead.\n{text}")
    passed = len(report.checks) - len(report.failures)
    marking = " and ".join(report.markings) or "no data read"
    print(f"\n{passed} of {len(report.checks)} checks passed; report written to {out}")
    print(f"The report describes data marked {marking}; handle it accordingly.")


def _wait_forever() -> None:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
