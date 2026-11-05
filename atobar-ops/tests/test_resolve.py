import json
import subprocess
from pathlib import Path

from atobar_ops import resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_telemetry_status_parsing_complete(monkeypatch):
    comment = _load("telemetry_complete.json")
    monkeypatch.setattr(resolve.gh, "json_or_empty", lambda *a, **k: [comment])
    info = resolve.find_telemetry("arsenyspb/ollama-monitor", 43)
    assert info is not None
    assert info.status == "✅ Complete"
    assert info.comment_id == comment["id"]


def test_telemetry_status_parsing_running(monkeypatch):
    comment = _load("telemetry_running.json")
    monkeypatch.setattr(resolve.gh, "json_or_empty", lambda *a, **k: [comment])
    info = resolve.find_telemetry("arsenyspb/ollama-monitor", 43)
    assert info.status == "🟢 Running"


def test_telemetry_status_parsing_halted(monkeypatch):
    comment = _load("telemetry_halted.json")
    monkeypatch.setattr(resolve.gh, "json_or_empty", lambda *a, **k: [comment])
    info = resolve.find_telemetry("arsenyspb/ollama-monitor", 43)
    assert "Halted" in info.status


def test_telemetry_picks_latest_matching_comment(monkeypatch):
    running = _load("telemetry_running.json")
    complete = _load("telemetry_complete.json")
    monkeypatch.setattr(resolve.gh, "json_or_empty", lambda *a, **k: [running, complete])
    info = resolve.find_telemetry("arsenyspb/ollama-monitor", 43)
    assert info.comment_id == complete["id"]


def test_no_telemetry_comment_returns_none(monkeypatch):
    monkeypatch.setattr(resolve.gh, "json_or_empty", lambda *a, **k: [{"id": 1, "body": "unrelated"}])
    assert resolve.find_telemetry("arsenyspb/ollama-monitor", 43) is None


def test_instance_name_resolution(monkeypatch):
    aws_output = json.dumps(
        [
            [
                ["i-devABC", "running", "flow-developer-arsenyspb__ollama-monitor-43"],
                ["i-otherxyz", "running", "flow-developer-arsenyspb__ollama-monitor-99"],
            ]
        ]
    )

    def fake_run(cmd, *, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=aws_output, stderr="")

    monkeypatch.setattr(resolve.timebox, "run", fake_run)
    instances = resolve.find_instances("arsenyspb/ollama-monitor", 43)
    assert len(instances) == 1
    assert instances[0].instance_id == "i-devABC"
    assert instances[0].role == "developer"
    assert instances[0].state == "running"


def test_instance_name_resolution_no_match(monkeypatch):
    aws_output = json.dumps([[["i-xyz", "running", "some-unrelated-name"]]])

    def fake_run(cmd, *, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=aws_output, stderr="")

    monkeypatch.setattr(resolve.timebox, "run", fake_run)
    assert resolve.find_instances("arsenyspb/ollama-monitor", 43) == []


def test_matches_issue_pr_by_head_ref():
    pr = {"headRefName": "flow/issue-43", "title": "feat: thing", "body": ""}
    assert resolve.matches_issue_pr(pr, 43)
    assert not resolve.matches_issue_pr(pr, 44)


def test_matches_issue_pr_by_flow_wip_developer_pattern():
    pr = {"headRefName": "flow-wip/developer/43", "title": "wip", "body": ""}
    assert resolve.matches_issue_pr(pr, 43)


def test_matches_issue_pr_ignores_non_flow_branches():
    pr = {"headRefName": "main", "title": "closes #43", "body": ""}
    assert not resolve.matches_issue_pr(pr, 43)
