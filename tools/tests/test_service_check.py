"""The service check, run end to end against the fake UDL world on a real local server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import service_check

from tests.fake_world import NOW, PASSWORD, TARGET, USERNAME, factory, world


def _main(tmp_path: Path, password: str = PASSWORD, *extra: str) -> tuple[int, dict[str, Any]]:
    out = tmp_path / "report.json"
    waited: list[bool] = []
    code = service_check.main(
        ["--objects", str(TARGET), "--port", "0", "--out", str(out), *extra],
        ask=lambda _: USERNAME,
        ask_secret=lambda _: password,
        client_factory=factory(world()),
        clock=lambda: NOW,
        wait=lambda: waited.append(True),
    )
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    if "--serve" in extra:
        assert waited == [True]
    return code, report


def test_the_whole_service_is_driven_and_timed_without_storing_credentials(
    tmp_path: Path,
) -> None:
    code, report = _main(tmp_path, PASSWORD, "--serve")
    names = {check["check"]: check for check in report["checks"]}
    assert code == 0, report["failures"]
    assert names[f"cold_{TARGET}"]["flag_s"] is not None
    assert names[f"cold_{TARGET}"]["observations"] > 0
    assert names[f"verdict_{TARGET}"]["outcome"]
    assert names[f"warm_{TARGET}"]["from_cache"] is True
    scan = names[f"scan_whole_window_{TARGET}"]
    assert scan["neighbours"] >= 1
    assert scan["instants"] <= 4_000
    assert names[f"figure_causes_raw_{TARGET}"]["points"] > 0
    text = json.dumps(report)
    assert PASSWORD not in text
    assert USERNAME not in text


def test_a_wrong_password_is_reported_and_nothing_else_runs(tmp_path: Path) -> None:
    code, report = _main(tmp_path, PASSWORD + "-wrong")
    assert code == 1
    assert report["failures"] == ["sign_in"]


def test_figure_helpers_count_points_and_times() -> None:
    figure = {"data": [{"x": [1, 2], "customdata": [5, 3]}, {"z": [[1, 2], [3]], "x": [0]}]}
    assert service_check.points(figure) == 6
    assert service_check.observation_times(figure) == [3, 5]


def test_the_report_carries_the_markings_of_the_data_it_describes(tmp_path: Path) -> None:
    _, report = _main(tmp_path)
    assert report["markings"]
    assert all(marking.startswith("UNCLASSIFIED") for marking in report["markings"])


def test_a_busy_port_is_found_before_the_password_is_asked_for(tmp_path: Path) -> None:
    import socket

    asked: list[str] = []

    def record(prompt: str) -> str:
        asked.append(prompt)
        return ""

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code = service_check.main(
            ["--port", str(port), "--out", str(tmp_path / "r.json")],
            ask=record,
            ask_secret=record,
            client_factory=factory(world()),
            clock=lambda: NOW,
        )
    assert code == 2
    assert asked == []


def test_ctrl_c_writes_a_partial_report_and_signs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(service_check, "check_object", interrupted)
    code, report = _main(tmp_path, PASSWORD)
    assert code == 1
    assert report["failures"] == ["interrupted"]
    assert report["checks"][0]["check"] == "sign_in"


def test_a_run_past_the_time_limit_is_cancelled_so_the_next_object_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_check, "RUN_LIMIT_S", -1.0)
    out = tmp_path / "report.json"
    service_check.main(
        ["--objects", str(TARGET), str(TARGET), "--port", "0", "--out", str(out)],
        ask=lambda _: USERNAME,
        ask_secret=lambda _: PASSWORD,
        client_factory=factory(world()),
        clock=lambda: NOW,
    )
    checks = json.loads(out.read_text(encoding="utf-8"))["checks"]
    cold = [c for c in checks if c["check"] == f"cold_{TARGET}"]
    assert len(cold) == 2
    assert all("stage" in c for c in cold)  # timed out on the limit, not refused with a 409


def test_a_report_path_that_cannot_be_written_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    service_check.main(
        ["--objects", str(TARGET), "--port", "0", "--out", str(blocker / "report.json")],
        ask=lambda _: USERNAME,
        ask_secret=lambda _: PASSWORD + "-wrong",
        client_factory=factory(world()),
        clock=lambda: NOW,
    )
    assert '"failures"' in capsys.readouterr().out
