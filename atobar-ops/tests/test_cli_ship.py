import argparse
import subprocess

import pytest

from atobar_ops import cli, verify
from atobar_ops.cli import build_pr_body


@pytest.fixture(autouse=True)
def _gitleaks_installed(monkeypatch):
    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/usr/local/bin/gitleaks")


def test_ship_refuses_body_without_fixes():
    with pytest.raises(ValueError, match="Fixes #"):
        build_pr_body("just some notes, no linkage", issue=None)


def test_ship_injects_fixes_line_when_issue_given():
    body = build_pr_body("some context", issue=307)
    assert "Fixes #307" in body
    assert "some context" in body


def test_ship_accepts_body_that_already_has_fixes():
    body = build_pr_body("Fixes #307\n\ndetails", issue=307)
    assert body.count("Fixes #307") == 1


def test_ship_refuses_when_issue_missing_and_body_lacks_fixes():
    with pytest.raises(ValueError):
        build_pr_body(None, issue=None)


def test_ship_case_insensitive_fixes_detection():
    body = build_pr_body("fixes #12 already there", issue=None)
    assert "fixes #12" in body.lower()


def test_ship_rejects_unpopulated_fixes_placeholder():
    with pytest.raises(ValueError, match="Fixes #"):
        build_pr_body("## Fixes #\n\nbody", issue=None)


def test_ship_requires_template_audit_sections_in_structured_bodies():
    with pytest.raises(ValueError, match="Adversarial Self-Review"):
        build_pr_body("Fixes #3\n\n## Changes\n- x", issue=None)
    full = (
        "Fixes #3\n\n## Adversarial Self-Review: Corner Cases\n### 1. Per-Detail Corner-Case Audit\n|a|\n"
        "### 2. Assumption & Simplification Audit\n- none\n"
    )
    assert build_pr_body(full, issue=None) == full


def test_ship_skips_pr_create_when_pr_already_exists(monkeypatch):
    calls = []

    def fake_run(cmd, *, timeout):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")
        if cmd and "gitleaks" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/310"}\n', stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":101,"status":"completed","conclusion":"success","name":"Python CI"}]\n',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("must not create a second PR when one already exists")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.timebox, "run", fake_run)

    args = argparse.Namespace(
        issue=309, title=None, body=None, body_file=None, merge=False, timeout=30, json=False, verbose=False, max_chars=None
    )
    rc = cli.cmd_ship(args)

    assert rc == cli.EXIT_OK
    assert any(c[:3] == ["gh", "pr", "view"] for c in calls)
    assert any(c[:3] == ["gh", "run", "list"] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)


def test_ac1_ship_refuses_when_mypy_fails(monkeypatch, capsys):
    def fake_run(cmd, *, timeout):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"] and "mypy" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="error: type mismatch\n", stderr="")
        if cmd[:2] == ["git", "push"]:
            raise AssertionError("must not push when mypy fails")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(cli.timebox, "run", fake_run)
    args = argparse.Namespace(
        issue=333, title="t", body="Fixes #333", body_file=None, merge=False, timeout=30, json=False, verbose=False, max_chars=None
    )
    rc = cli.cmd_ship(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_ERROR
    assert "REFUSED: verification failing, not shipping" in out


def test_ac2_ship_body_file_and_updates_existing_open_pr(tmp_path, monkeypatch, capsys):
    body_path = tmp_path / "pr_body.md"
    body_path.write_text(
        "Refs #333\n\n## Adversarial Self-Review: Corner Cases\n"
        "### 1. Per-Detail Corner-Case Audit\n|a|\n"
        "### 2. Assumption & Simplification Audit\n- none\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, *, timeout):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"] or (cmd and "gitleaks" in cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/334"}\n', stderr="")
        if cmd[:3] == ["gh", "pr", "edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/o/r/pull/334\n", stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":102,"status":"completed","conclusion":"success","name":"Python CI"}]\n',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.timebox, "run", fake_run)

    # Mutually exclusive --body and --body-file
    rc_mutex = cli.cmd_ship(
        argparse.Namespace(
            issue=333,
            title="New Title",
            body="Fixes #333",
            body_file=str(body_path),
            merge=False,
            timeout=30,
            json=False,
            verbose=False,
            max_chars=None,
        )
    )
    assert rc_mutex == cli.EXIT_ERROR
    assert "mutually exclusive" in capsys.readouterr().out

    # --body-file + --title on open PR calls `gh pr edit` and watches checks
    rc_ok = cli.cmd_ship(
        argparse.Namespace(
            issue=333,
            title="Updated Title",
            body=None,
            body_file=str(body_path),
            merge=False,
            timeout=30,
            json=False,
            verbose=False,
            max_chars=None,
        )
    )
    out = capsys.readouterr().out
    assert rc_ok == cli.EXIT_OK
    assert any(c[:3] == ["gh", "pr", "edit"] and "--title" in c and "--body" in c for c in calls)
    assert "PR updated (rc=0)" in out
    assert "checks: rc=0" in out



def _ship_args(**overrides):
    base = dict(
        issue=None, title=None, body=None, body_file=None, merge=False, timeout=30, json=False, verbose=False, max_chars=None
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_ship_run(calls, *, merge_rc=0):
    def fake_run(cmd, *, timeout):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"] or (cmd and "gitleaks" in cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/9"}\n', stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":103,"status":"completed","conclusion":"success","name":"Python CI"}]\n',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(cmd, merge_rc, stdout="", stderr="merge blocked" if merge_rc else "")
        if cmd[:2] in (["git", "checkout"], ["git", "pull"]):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


def test_ship_existing_open_pr_needs_no_body_or_issue(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.timebox, "run", _fake_ship_run(calls))
    rc = cli.cmd_ship(_ship_args(merge=True))
    assert rc == cli.EXIT_OK
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert not any(c[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"]) for c in calls)


def test_ship_merge_failure_returns_error_and_stays_on_branch(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.timebox, "run", _fake_ship_run(calls, merge_rc=1))
    rc = cli.cmd_ship(_ship_args(merge=True))
    assert rc == cli.EXIT_ERROR
    assert "gh pr merge failed" in capsys.readouterr().out
    assert not any(c[:2] == ["git", "checkout"] for c in calls)


def test_ship_refuses_without_gitleaks(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli.timebox, "run", _fake_ship_run(calls))
    rc = cli.cmd_ship(_ship_args())
    assert rc == cli.EXIT_ERROR
    assert "gitleaks: MISSING" in capsys.readouterr().out
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_wp1_ship_waits_on_ci_only_and_shows_devin_as_informational(monkeypatch, capsys):
    calls = []
    run_polls = {"count": 0}
    monkeypatch.setattr(cli.timebox.Deadline, "sleep", lambda self, s: None)

    def fake_run(cmd, *, timeout):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"] or (cmd and "gitleaks" in cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/359"}\n', stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            run_polls["count"] += 1
            if run_polls["count"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":777,"status":"completed","conclusion":"success","name":"Python CI"}]\n',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "checks"]:
            assert "--watch" not in cmd
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"name":"renamed-ci-job","state":"SUCCESS","bucket":"pass","link":"https://github.com/o/r/actions/runs/777/job/1"},{"name":"Devin Review","state":"IN_PROGRESS","bucket":"pending","link":"https://app.devin.ai/review/1"}]\n',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.timebox, "run", fake_run)
    rc = cli.cmd_ship(_ship_args())
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert run_polls["count"] == 2
    assert "Python CI #777: success" in out
    assert "renamed-ci-job" not in out
    assert "Devin Review: pending — run 'atobar-ops review <pr> --wait' if you want it" in out


def test_wp1_ship_rejects_skipped_ci_and_retries_transient_run_list_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.timebox.Deadline, "sleep", lambda self, s: None)
    polls = {"n": 0}

    def fake_run(cmd, *, timeout):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["uv", "run"] or (cmd and "gitleaks" in cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/359"}\n', stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            polls["n"] += 1
            if polls["n"] == 1:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 502 Bad Gateway")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":779,"status":"completed","conclusion":"skipped","name":"Python CI"}]\n',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.timebox, "run", fake_run)
    monkeypatch.setattr(cli, "_append_failed_ci_tail", lambda *a, **k: None)
    rc = cli.cmd_ship(_ship_args(merge=True))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_ERROR
    assert polls["n"] == 2
    assert "Python CI #779: skipped (expected success)" in out


def test_wp5_ship_allows_unstaged_jumbo_bookkeeping_but_blocks_other_dirty_files(monkeypatch, capsys):
    def fake_run_jumbo_only(cmd, *, timeout):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M JUMBO_STATUS.md\n M issues/J47-ergonomics.md\n", stderr=""
            )
        if cmd[:2] == ["uv", "run"] or (cmd and "gitleaks" in cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc1234567890\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/359"}\n', stderr="")
        if cmd[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='[{"databaseId":778,"status":"completed","conclusion":"success","name":"Python CI"}]\n',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.timebox, "run", fake_run_jumbo_only)
    rc = cli.cmd_ship(_ship_args())
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "note: uncommitted Jumbo bookkeeping (not part of this PR)" in out

    # Staged Jumbo file or other tracked file still blocks
    monkeypatch.setattr(
        cli.timebox,
        "run",
        lambda cmd, *, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout="M  JUMBO_STATUS.md\n M atobar-ops/src/atobar_ops/cli.py\n", stderr=""
        ),
    )
    rc_block = cli.cmd_ship(_ship_args())
    out_block = capsys.readouterr().out
    assert rc_block == cli.EXIT_ERROR
    assert "REFUSED: uncommitted tracked changes" in out_block
