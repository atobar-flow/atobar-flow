from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from atobar_ops import ci_minutes, cli


def _job(
    sec: int | None,
    *,
    name: str = "test",
    conclusion: str = "success",
    steps: list[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    base = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)
    if sec is None:
        return {"name": name, "conclusion": conclusion, "started_at": None, "completed_at": None, "steps": []}
    step_objs = []
    for s_name, s_sec in steps or []:
        step_objs.append(
            {
                "name": s_name,
                "conclusion": "success",
                "started_at": base.isoformat().replace("+00:00", "Z"),
                "completed_at": (base + timedelta(seconds=s_sec)).isoformat().replace("+00:00", "Z"),
            }
        )
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": base.isoformat().replace("+00:00", "Z"),
        "completed_at": (base + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z"),
        "steps": step_objs,
    }


def test_wp5_billing_rounds_up_per_job() -> None:
    payload = {
        "jobs": [
            _job(7, name="j7"),
            _job(59, name="j59"),
            _job(60, name="j60"),
            _job(61, name="j61"),
            _job(0, name="jskip", conclusion="skipped"),
            _job(None, name="jincomplete"),
        ]
    }
    res = ci_minutes.summarize([payload])
    assert res["jobs"]["j7"]["billed_minutes"] == 1
    assert res["jobs"]["j59"]["billed_minutes"] == 1
    assert res["jobs"]["j60"]["billed_minutes"] == 1
    assert res["jobs"]["j61"]["billed_minutes"] == 2
    assert res["jobs"]["jskip"]["billed_minutes"] == 0
    assert res["jobs"]["jincomplete"]["n"] == 0
    assert res["jobs"]["jincomplete"]["incomplete"] == 1
    assert res["incomplete"] == 1
    assert res["total_billed_minutes"] == 5


def test_wp5_danger_band_and_cliff_counts() -> None:
    payloads = [
        {"jobs": [_job(54)]},
        {"jobs": [_job(55)]},
        {"jobs": [_job(59)]},
        {"jobs": [_job(61)]},
    ]
    res = ci_minutes.summarize(payloads)
    job = res["jobs"]["test"]
    assert job["danger_band"] == 2
    assert job["over_60s"] == 1
    assert job["cliff"] == 1
    assert res["danger_band"] == 2
    assert res["over_60s"] == 1


def test_wp5_percentiles_small_n() -> None:
    res1 = ci_minutes.summarize([{"jobs": [_job(42, steps=[("pytest", 19), ("mypy", 5)])]}])
    assert res1["jobs"]["test"]["p50_s"] == 42
    assert res1["jobs"]["test"]["p90_s"] == 42
    assert res1["jobs"]["test"]["slowest_step"] == {"name": "pytest", "median_s": 19}

    res2 = ci_minutes.summarize([{"jobs": [_job(40)]}, {"jobs": [_job(50)]}])
    assert res2["jobs"]["test"]["p50_s"] == 40
    assert res2["jobs"]["test"]["p90_s"] == 50

    res20 = ci_minutes.summarize([{"jobs": [_job(s)]} for s in range(31, 51)])
    assert res20["jobs"]["test"]["p50_s"] == 40
    assert res20["jobs"]["test"]["p90_s"] == 48
    assert res20["jobs"]["test"]["max_s"] == 50


def test_wp5_next_hint_thresholds() -> None:
    res_ok = ci_minutes.summarize([{"jobs": [_job(45)]}])
    assert res_ok["next"] == "OK"

    res_warn = ci_minutes.summarize([{"jobs": [_job(51)]}])
    assert "WARN" in res_warn["next"]
    assert "J45 WP-6" in res_warn["next"]

    res_cliff = ci_minutes.summarize([{"jobs": [_job(51)]}, {"jobs": [_job(61)]}])
    assert "CLIFF" in res_cliff["next"]


def test_wp5_timeout_returns_partial(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [{"databaseId": i, "status": "completed", "conclusion": "success", "name": "Python CI"} for i in range(1, 11)]
    job_calls = 0

    class _FakeDeadline:
        def __init__(self, _timeout: int) -> None:
            pass

        def expired(self) -> bool:
            return job_calls >= 3

        def remaining(self) -> float:
            return 10.0

    def fake_json_or_empty(gh_args: list[str], *, timeout: float = 30) -> Any:
        nonlocal job_calls
        if gh_args[:2] == ["run", "list"]:
            return runs
        if gh_args[0] == "api":
            job_calls += 1
            return {"jobs": [_job(40, steps=[("Test (pytest)", 18)])]}
        raise AssertionError(f"unexpected gh call: {gh_args}")

    monkeypatch.setattr(cli.timebox, "Deadline", _FakeDeadline)
    monkeypatch.setattr(cli.gh, "json_or_empty", fake_json_or_empty)

    args = argparse.Namespace(
        repo="atobar-flow/atobar-flow",
        run_id=None,
        limit=10,
        workflow="ci.yml",
        minutes=True,
        timeout=30,
        json=False,
        verbose=False,
        max_chars=None,
    )
    rc = cli.cmd_ci(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert job_calls == 3
    assert "TRUNCATED: analyzed 3/10 runs before timeout" in out
    assert "test: n=3" in out


def test_wp5_cli_registered() -> None:
    parsed = cli.build_parser().parse_args(["ci", "o/r", "--minutes"])
    assert parsed.repo == "o/r"
    assert parsed.minutes is True


def test_wp5_skipped_jobs_do_not_dilute_p90_and_empty_is_not_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 9 skipped runs + 1 slow 55s run: p90 must reflect the 55s execution, not 0s
    payloads = [{"jobs": [_job(0, name="build", conclusion="skipped")]} for _ in range(9)]
    payloads.append({"jobs": [_job(55, name="build")]})
    res = ci_minutes.summarize(payloads)
    assert res["jobs"]["build"]["n"] == 1
    assert res["jobs"]["build"]["skipped"] == 9
    assert res["jobs"]["build"]["p90_s"] == 55
    assert res["jobs"]["build"]["billed_minutes"] == 1
    assert "WARN" in res["next"]

    # Empty / failed API fetch must not report false-green OK
    empty_res = ci_minutes.summarize([])
    assert empty_res["next"].startswith("NO_DATA")

    # Pagination across multiple pages + failed fetch marks truncated
    runs = [
        {"databaseId": 1, "status": "completed", "conclusion": "success", "name": "CI"},
        {"databaseId": 2, "status": "completed", "conclusion": "success", "name": "CI"},
    ]

    def fake_json_or_empty(gh_args: list[str], *, timeout: float = 30) -> Any:
        if gh_args[:2] == ["run", "list"]:
            return runs
        if gh_args[0] == "api" and "runs/1/jobs?per_page=100" in gh_args[1]:
            if "page=2" in gh_args[1]:
                return {"total_count": 2, "jobs": [_job(45, name="page2_job")]}
            return {"total_count": 2, "jobs": [_job(40, name="page1_job")]}
        if gh_args[0] == "api" and "runs/2/jobs" in gh_args[1]:
            return []  # simulate API error
        raise AssertionError(f"unexpected gh call: {gh_args}")

    monkeypatch.setattr(cli.gh, "json_or_empty", fake_json_or_empty)
    args = argparse.Namespace(
        repo="atobar-flow/atobar-flow",
        run_id=None,
        limit=5,
        workflow="ci.yml",
        minutes=True,
        timeout=30,
        json=False,
        verbose=False,
        max_chars=None,
    )
    rc = cli.cmd_ci(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "TRUNCATED: analyzed 1/2 runs" in out
    assert "page1_job: n=1" in out
    assert "page2_job: n=1" in out
