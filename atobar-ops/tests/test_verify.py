from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

from atobar_ops import cli, output, verify


REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "changed": False,
        "timeout": 60,
        "json": False,
        "verbose": False,
        "max_chars": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_ac1_ci_yml_test_job_commands_parity_with_verify() -> None:
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ci_doc = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    test_job = ci_doc["jobs"]["test"]
    steps = test_job["steps"]

    assert (test_job.get("env") or {}).get("GITLEAKS_VERSION") == verify.GITLEAKS_VERSION

    verify_cmds = [" ".join(cmd) for _, cmd, _ in verify.ci_test_commands()]
    expected_gl_prefix = " ".join(["gitleaks", *verify.gitleaks_base_argv("gitleaks")[1:]])

    for step in steps:
        run_script = step.get("run")
        if not run_script:
            continue
        name = str(step.get("name") or "")
        if name.lower().startswith("install "):
            continue
        if name == "Secret scan (gitleaks)":
            gl_lines = [ln.strip() for ln in run_script.splitlines() if ln.strip().startswith("gitleaks ")]
            assert len(gl_lines) == 1, f"Expected one gitleaks command in {name!r}, got {gl_lines}"
            assert gl_lines[0].startswith(expected_gl_prefix), (
                f"{name!r} command {gl_lines[0]!r} must start with {expected_gl_prefix!r}"
            )
            assert "--log-opts" in gl_lines[0]
            continue
        for raw_line in run_script.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            normalized_verify_cmds = [c.replace(" --tb=short", "") for c in verify_cmds]
            assert line in verify_cmds or line in normalized_verify_cmds, (
                f"ci.yml step {name!r} command {line!r} not in verify.ci_test_commands()"
            )


def test_wp2_ci_test_commands_has_single_mypy_over_all_packages() -> None:
    mypy_steps = [(name, cmd) for name, cmd, _ in verify.ci_test_commands() if "mypy" in name]
    assert len(mypy_steps) == 1
    name, cmd = mypy_steps[0]
    assert name == "mypy"
    expected_srcs = [f"{p}/src" for p in verify.WORKSPACE_PACKAGES]
    src_positions = [cmd.index(s) for s in expected_srcs]
    assert src_positions == sorted(src_positions)
    assert "--ignore-missing-imports" in cmd


def test_wp2_mypy_package_list_matches_workspace_dirs() -> None:
    discovered = {
        p.name
        for p in REPO_ROOT.iterdir()
        if p.is_dir() and (p / "src").is_dir() and (p / "pyproject.toml").is_file()
    }
    assert set(verify.WORKSPACE_PACKAGES) == discovered


def test_wp1_gitleaks_base_argv_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert verify.gitleaks_base_argv("gitleaks") == ["gitleaks", "git", "--no-banner", "--redact"]

    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    assert verify.gitleaks_base_argv("gitleaks") == [
        "gitleaks",
        "git",
        "--no-banner",
        "--redact",
        "--config",
        ".gitleaks.toml",
    ]

    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/usr/local/bin/gitleaks")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd == ["/usr/local/bin/gitleaks", "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="8.24.2\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(verify.timebox, "run", fake_run)
    rc, lines, _ = verify.run_verify(_make_args())
    assert rc == verify.EXIT_OK
    assert verify.gitleaks_base_argv("/usr/local/bin/gitleaks") + ["--log-opts=origin/main..HEAD"] in calls
    assert any("gitleaks: NOTE: local 8.24.2 != CI 8.24.3" in ln for ln in lines)


def test_ac1_verify_skips_gitleaks_explicitly_when_binary_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        verify.timebox,
        "run",
        lambda cmd, *, timeout: subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr=""),
    )
    args = _make_args()
    rc = verify.cmd_verify(args)
    out = capsys.readouterr().out
    assert rc == verify.EXIT_OK
    assert "gitleaks: SKIPPED: gitleaks not installed" in out
    assert "gitleaks: rc=0" not in out


def test_ac1_verify_fails_when_mypy_or_any_step_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(verify.shutil, "which", lambda _name: "/usr/local/bin/gitleaks")

    def fake_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if "mypy" in cmd and "flow-engine/src" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="flow-engine/src/x.py:1: error: boom\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(verify.timebox, "run", fake_run)
    rc = verify.cmd_verify(_make_args())
    out = capsys.readouterr().out
    assert rc == verify.EXIT_ERROR
    assert "VERIFY FAILED: mypy" in out
    assert "flow-engine/src/x.py:1: error: boom" in out


def test_verify_changed_targets_modified_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="contracts/src/atobar_contracts/goal.py\n", stderr="")
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="tests/test_boundaries.py\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(verify.timebox, "run", fake_run)
    rc, lines, data = verify.run_verify(_make_args(changed=True), changed=True)
    assert rc == verify.EXIT_OK
    assert data["ok"] is True
    assert any("contracts/tests" in c for c in calls if "pytest" in c)
    assert any("tests" in c for c in calls if "pytest" in c), "untracked root tests must be targeted"
    assert "VERIFY OK (changed-only)" in lines[0]


def test_ac3_jumbo_new_refuses_overwrite_and_duplicates_and_supports_from_file_and_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "issues").mkdir()
    (tmp_path / "issues" / "J39-existing.md").write_text("# J39 existing\n", encoding="utf-8")
    (tmp_path / "JUMBO_STATUS.md").write_text("- J40: Existing in status (#326)\n", encoding="utf-8")

    gh_calls: list[list[str]] = []

    def fake_gh_json(args: list[str], *, timeout: float = 30) -> Any:
        gh_calls.append(args)
        return [{"number": 333, "title": "[Backlog / Harness] J41: Harness Guardrails", "url": "https://x/333"}]

    def fake_gh_run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        gh_calls.append(args)
        if args[:2] == ["issue", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/atobar-flow/atobar-flow/issues/350\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.gh, "json_or_empty", fake_gh_json)
    monkeypatch.setattr(cli.gh, "run", fake_gh_run)

    # 1. Refuses overwriting existing local J39 file
    rc_local = cli.cmd_jumbo_new(
        _make_args(number="39", slug="clobber", title="Clobber attempt", context=None, from_file=None)
    )
    assert rc_local == cli.EXIT_ERROR
    assert "REFUSED: local Jumbo file for J39 already exists" in capsys.readouterr().out
    assert (tmp_path / "issues" / "J39-existing.md").read_text(encoding="utf-8") == "# J39 existing\n"

    # 2. Refuses J40 already in JUMBO_STATUS.md
    rc_status = cli.cmd_jumbo_new(
        _make_args(number="40", slug="dup-status", title="Dup status", context=None, from_file=None)
    )
    assert rc_status == cli.EXIT_ERROR
    assert "REFUSED: J40 already appears in JUMBO_STATUS.md" in capsys.readouterr().out

    # 3. Refuses J41 already in open GitHub jumbo issue
    rc_remote = cli.cmd_jumbo_new(
        _make_args(number="41", slug="dup-remote", title="Dup remote", context=None, from_file=None)
    )
    assert rc_remote == cli.EXIT_ERROR
    assert "REFUSED: J41 already exists in open GitHub issue #333" in capsys.readouterr().out

    # 4. Creates J42 from --from-file
    draft = tmp_path / "draft.md"
    draft.write_text("# J42 custom body\n\nDetailed spec.\n", encoding="utf-8")
    rc_ok = cli.cmd_jumbo_new(
        _make_args(number="J42", slug="custom-spec", title="Custom Spec", context=None, from_file=str(draft))
    )
    assert rc_ok == cli.EXIT_OK
    created = tmp_path / "issues" / "J42-custom-spec.md"
    assert created.read_text(encoding="utf-8") == "# J42 custom body\n\nDetailed spec.\n"
    assert "J42: Custom Spec (https://github.com/atobar-flow/atobar-flow/issues/350)" in (
        tmp_path / "JUMBO_STATUS.md"
    ).read_text(encoding="utf-8")
    capsys.readouterr()

    # 5. jumbo sync 42 pushes updated local file via `gh issue edit 350 --body-file`
    created.write_text("# J42 updated body\n", encoding="utf-8")
    rc_sync = cli.cmd_jumbo_sync(_make_args(number="42"))
    assert rc_sync == cli.EXIT_OK
    assert any(c[:3] == ["issue", "edit", "350"] and "--body-file" in c for c in gh_calls)


def test_ac4_review_renders_13_threads_within_default_max_chars_and_handles_truncation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    threads_nodes = [
        {
            "isResolved": (i % 3 == 0),
            "isOutdated": (i % 2 == 0),
            "path": f"flow-engine/src/flow_engine/file_{i}.py",
            "line": 100 + i,
            "originalLine": 100 + i,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "arsenyspb"},
                        "body": f"**P1 #{i} — detailed finding.** " + ("Explanation text with repro steps. " * 25),
                        "createdAt": "2026-10-20T10:26:21Z",
                    }
                ]
            },
        }
        for i in range(1, 14)
    ]
    gql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 332,
                    "title": "feat(conductor): implement SEQ-05 outer goal loop (#204)",
                    "state": "MERGED",
                    "url": "https://github.com/atobar-flow/atobar-flow/pull/332",
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": "arsenyspb"},
                                "state": "COMMENTED",
                                "submittedAt": "2026-10-20T10:26:21Z",
                                "body": "## Adversarial review — #332\n" + ("Summary table row. " * 150),
                            }
                        ]
                    },
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": threads_nodes,
                    },
                }
            }
        }
    }

    monkeypatch.setattr(
        cli.gh,
        "run",
        lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=json.dumps(gql_response), stderr=""),
    )

    rc = cli.cmd_review(_make_args(pr=332, repo="atobar-flow/atobar-flow", unresolved=False))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert len(out) <= output.DEFAULT_MAX_CHARS
    assert "PR #332 [MERGED]" in out
    assert "Reviewers: arsenyspb=COMMENTED" in out
    for i in range(1, 14):
        assert f"[{i}/13]" in out
        assert f"flow-engine/src/flow_engine/file_{i}.py:{100 + i}" in out
    assert "[resolved,outdated]" in out
    assert "[unresolved]" in out

    # Also test --unresolved filter
    rc_unres = cli.cmd_review(_make_args(pr=332, repo="atobar-flow/atobar-flow", unresolved=True))
    out_unres = capsys.readouterr().out
    assert rc_unres == cli.EXIT_OK
    assert "[resolved" not in out_unres
    assert "Threads: 9 shown (unresolved only)" in out_unres


def test_jumbo_new_failed_issue_create_leaves_no_trace_and_retry_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    create_rc = {"rc": 1}

    def fake_gh_run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["issue", "create"]:
            if create_rc["rc"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="network down")
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/atobar-flow/atobar-flow/issues/351\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.gh, "json_or_empty", lambda args, *, timeout=30: [])
    monkeypatch.setattr(cli.gh, "json_or_raise", lambda args, *, timeout=30: [])
    monkeypatch.setattr(cli.gh, "run", fake_gh_run)
    new_args = _make_args(number="43", slug="retry", title="Retry", context=None, from_file=None)

    assert cli.cmd_jumbo_new(new_args) == cli.EXIT_ERROR
    assert "network down" in capsys.readouterr().out
    assert not (tmp_path / "issues" / "J43-retry.md").exists()
    assert not (tmp_path / "JUMBO_STATUS.md").exists()

    create_rc["rc"] = 0
    assert cli.cmd_jumbo_new(new_args) == cli.EXIT_OK
    assert "J43: Retry (https://github.com/atobar-flow/atobar-flow/issues/351)" in (tmp_path / "JUMBO_STATUS.md").read_text(
        encoding="utf-8"
    )


def _review_pr(threads_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 7,
                    "title": "t",
                    "state": "OPEN",
                    "url": "u",
                    "reviews": {"nodes": []},
                    "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": threads_nodes},
                }
            }
        }
    }


def test_review_reports_omitted_threads_instead_of_silent_cut(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    nodes = [
        {
            "id": f"T{i}",
            "isResolved": False,
            "isOutdated": False,
            "path": f"flow-engine/src/flow_engine/some/deep/module_{i}.py",
            "line": i,
            "comments": {"nodes": [{"author": {"login": "bot"}, "body": "finding " * 40}]},
        }
        for i in range(1, 81)
    ]
    resp = json.dumps(_review_pr(nodes))
    monkeypatch.setattr(
        cli.gh, "run", lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=resp, stderr="")
    )
    assert cli.cmd_review(_make_args(pr=7, repo="o/r", unresolved=False)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert len(out) <= output.DEFAULT_MAX_CHARS
    assert "[truncated:" not in out
    m = __import__("re").search(r"Threads: (\d+) of 80 shown \((\d+) omitted", out)
    assert m and int(m.group(1)) + int(m.group(2)) == 80
    assert out.count("/80] [unresolved]") == int(m.group(1))


def test_review_paginates_thread_comments_beyond_first_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = [{"author": {"login": "a"}, "body": f"c{i}"} for i in range(20)]
    nodes = [
        {
            "id": "THREAD1",
            "isResolved": False,
            "isOutdated": False,
            "path": "x.py",
            "line": 1,
            "comments": {"pageInfo": {"hasNextPage": True, "endCursor": "CUR1"}, "nodes": first},
        }
    ]
    page2 = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "CUR2"},
                    "nodes": [{"author": {"login": "b"}, "body": "late-reply-21"}],
                }
            }
        }
    }
    calls: list[list[str]] = []

    def fake_gh_run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        body = page2 if "id=THREAD1" in args else _review_pr(nodes)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(cli.gh, "run", fake_gh_run)
    assert cli.cmd_review(_make_args(pr=7, repo="o/r", unresolved=False, verbose=True)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "late-reply-21" in out
    assert any("cursor=CUR1" in a for a in calls if "id=THREAD1" in a)


def test_jumbo_new_recovers_issue_created_despite_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    lists: list[list[str]] = []

    def fake_json_or_raise(args: list[str], *, timeout: float = 30) -> Any:
        lists.append(args)
        return [{"number": 352, "title": "J44: Timed out", "url": "https://github.com/atobar-flow/atobar-flow/issues/352"}]

    monkeypatch.setattr(cli.gh, "json_or_empty", lambda args, *, timeout=30: [])
    monkeypatch.setattr(cli.gh, "json_or_raise", fake_json_or_raise)
    monkeypatch.setattr(
        cli.gh,
        "run",
        lambda args, *, timeout=30: subprocess.CompletedProcess(args, 124, stdout="", stderr="[timebox] timed out"),
    )
    rc = cli.cmd_jumbo_new(_make_args(number="44", slug="timed-out", title="Timed out", context=None, from_file=None))
    assert rc == cli.EXIT_OK
    assert (tmp_path / "issues" / "J44-timed-out.md").is_file()
    assert "issues/352" in (tmp_path / "JUMBO_STATUS.md").read_text(encoding="utf-8")
    assert any("all" in a for a in lists)


def test_jumbo_new_keeps_spec_when_remote_state_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    def boom(args: list[str], *, timeout: float = 30) -> Any:
        raise cli.gh.GhError("network down")

    monkeypatch.setattr(cli.gh, "json_or_empty", lambda args, *, timeout=30: [])
    monkeypatch.setattr(cli.gh, "json_or_raise", boom)
    monkeypatch.setattr(
        cli.gh, "run", lambda args, *, timeout=30: subprocess.CompletedProcess(args, 124, stdout="", stderr="timeout")
    )
    rc = cli.cmd_jumbo_new(_make_args(number="45", slug="unknown", title="Unknown", context=None, from_file=None))
    assert rc == cli.EXIT_ERROR
    assert (tmp_path / "issues" / "J45-unknown.md").is_file()
    assert not (tmp_path / "JUMBO_STATUS.md").exists()
    assert "could not be confirmed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("changed", "must_include"),
    [
        (["contracts/src/atobar_contracts/github_rest.py"], {"tests", "contracts/tests", "flow-engine/tests", "control-panel/tests"}),
        (["contracts/src/atobar_contracts/store.py"], {"tests", "flow-engine/tests", "control-panel/tests"}),
        (
            ["infra/terraform/control-panel/scripts/cloud_init.sh.tpl", "flow-engine/src/flow_engine/api.py"],
            {"tests", "flow-engine/tests"},
        ),
        (["contracts/src/atobar_contracts/deleted_file.py"], {"tests", "contracts/tests", "flow-engine/tests", "control-panel/tests"}),
        (["AGENTS.md"], {"tests"}),
    ],
)
def test_verify_changed_always_runs_root_guards_and_fans_out_contracts(
    monkeypatch: pytest.MonkeyPatch, changed: list[str], must_include: set[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    steps = dict((name, cmd) for name, cmd, _ in verify._changed_verify_steps(changed))
    assert "--tb=short" in steps["pytest"]
    targets = {a for a in steps["pytest"][4:] if not a.startswith("-")}
    assert must_include <= targets, targets


def test_verify_changed_does_not_duplicate_files_under_targeted_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    steps = dict((name, cmd) for name, cmd, _ in verify._changed_verify_steps(["tests/test_boundaries.py"]))
    targets = [a for a in steps["pytest"][4:] if not a.startswith("-")]
    assert targets == ["tests"]


def test_wp3_verify_changed_runs_mypy_when_src_py_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    src_steps = dict((name, cmd) for name, cmd, _ in verify._changed_verify_steps(["flow-engine/src/flow_engine/api.py"]))
    assert "mypy" in src_steps
    assert src_steps["mypy"] == verify._mypy_command()

    doc_steps = dict((name, cmd) for name, cmd, _ in verify._changed_verify_steps(["AGENTS.md", "tests/test_boundaries.py"]))
    assert "mypy" not in doc_steps


def test_wp3_verify_failure_excerpt_preserves_assertion_diff_and_quiets_git_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Real `git init` with pytest_subprocess_env() emits zero defaultBranchName advice lines
    git_res = verify.timebox.run(["git", "init", str(tmp_path / "repo")], timeout=10, env=verify.pytest_subprocess_env())
    assert git_res.returncode == 0
    assert "hint:" not in git_res.stderr.lower()
    assert "defaultbranchname" not in git_res.stderr.lower()

    # 2. Excerpt preserves E-line assertion diff even when captured stdout and durations follow
    noisy_pytest_stdout = "\n".join(
        [
            "=================================== FAILURES ===================================",
            "___________________________ test_deliberate_failure ____________________________",
            "tests/test_sample.py:12: in test_deliberate_failure",
            "    assert actual == expected",
            "E   AssertionError: assert {'a': 1} == {'a': 2}",
            "E     Differing items:",
            "E     {'a': 1} != {'a': 2}",
            "----------------------------- Captured stdout call -----------------------------",
            *[f"noisy captured line {i}" for i in range(30)],
            "============================= slowest 10 durations =============================",
            *[f"0.1{i}s call     tests/test_sample.py::test_{i}" for i in range(10)],
            "=========================== short test summary info ============================",
            "FAILED tests/test_sample.py::test_deliberate_failure - AssertionError: assert {'a': 1} == {'a': 2}",
            "1 failed, 99 passed in 1.23s",
        ]
    )
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)

    def fake_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=noisy_pytest_stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(verify.timebox, "run", fake_run)
    rc = verify.cmd_verify(_make_args())
    out = capsys.readouterr().out
    assert rc == verify.EXIT_ERROR
    assert "E   AssertionError: assert {'a': 1} == {'a': 2}" in out
    assert "E     {'a': 1} != {'a': 2}" in out
    assert "FAILED tests/test_sample.py::test_deliberate_failure" in out
    assert "noisy captured line" not in out


def test_review_strips_html_markup_before_budgeting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    noise = '<!-- devin-review-comment {"id": "x", "blob": "' + "z" * 2000 + '"} -->'
    badge = (
        '<!-- devin-review-badge-begin -->\n<a href="https://x"><picture><source srcset="https://x/y.svg">'
        '<img src="https://x/y.svg" alt="Devin Review"></picture></a>\n<!-- devin-review-badge-end -->'
    )
    nodes = [
        {
            "id": "T1",
            "isResolved": False,
            "isOutdated": False,
            "path": "a.py",
            "line": 1,
            "comments": {"nodes": [{"author": {"login": "bot"}, "body": f"{noise}\nReal finding text\n{badge}"}]},
        }
    ]
    resp = json.dumps(_review_pr(nodes))
    monkeypatch.setattr(
        cli.gh, "run", lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=resp, stderr="")
    )
    assert cli.cmd_review(_make_args(pr=7, repo="o/r", unresolved=False)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Real finding text" in out
    assert "devin-review-comment" not in out and "<picture>" not in out and "zzzz" not in out


@pytest.mark.parametrize(
    "body",
    [
        "The renderer outputs <script> instead of escaped text.",
        "<script>",
        "Return type should be `List<str>` here",
        "See <https://example.com/docs> for details",
        'The fallback is wrong: <picture><source srcset="a.svg">Fallback text</picture>',
        '<a href="x"><img src="y.png" alt="screenshot of bug"></a>',
        "The parser mishandles:\n```html\n<!-- devin-review-badge-begin -->\n<a>important example</a>\n"
        "<!-- devin-review-badge-end -->\n```",
        "Inline `<!-- devin-review-comment {} -->` and `<details>` are quoted evidence",
    ],
)
def test_review_markup_strip_keeps_angle_bracket_evidence(body: str) -> None:
    assert cli._strip_review_markup(body) == body


def test_review_markup_strip_removes_devin_metadata_and_badges() -> None:
    body = (
        '<!-- devin-review-comment {"id": "x"} -->\n\nFinding <code> here\n\n<details>\n<summary>Learn more</summary>\n'
        "Detail text\n</details>\n\n<!-- devin-review-badge-begin -->\n"
        '<a href="https://app.devin.ai/review/x" target="_blank"><picture><source srcset="https://s/y.svg">'
        '<img src="https://s/y.svg" alt="Devin Review"></picture></a>\n<!-- devin-review-badge-end -->'
    )
    out = cli._strip_review_markup(body)
    assert "Finding <code> here" in out and "Learn more" in out and "Detail text" in out
    assert "devin-review" not in out and "<picture" not in out and "<a " not in out and "<details>" not in out


def test_review_includes_top_level_pr_issue_comments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = _review_pr([])
    pr["data"]["repository"]["pullRequest"]["comments"] = {
        "nodes": [
            {
                "author": {"login": "claude-reviewer"},
                "body": "## Claude Code review — 3 guard gaps fixed",
                "createdAt": "2026-10-21T11:54:02Z",
            }
        ]
    }
    resp = json.dumps(pr)
    monkeypatch.setattr(
        cli.gh, "run", lambda args, *, timeout=30: subprocess.CompletedProcess(args, 0, stdout=resp, stderr="")
    )
    assert cli.cmd_review(_make_args(pr=334, repo="o/r", unresolved=False)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "@claude-reviewer [PR_COMMENT]" in out
    assert "Claude Code review — 3 guard gaps fixed" in out
