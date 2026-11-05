import os
import subprocess

import pytest

from flow_agent.agents.nanobot_config import render_nanobot_run_config
from flow_agent.runtime.sandbox import (
    SetupFailure,
    Workspace,
    prepare,
    prepare_role_worktree,
)
from flow_agent.tools.git_tools import workspace_dir


@pytest.fixture(autouse=True)
def _clean_workspace_dir_env():
    prev = os.environ.pop("WORKSPACE_DIR", None)
    yield
    if prev is None:
        os.environ.pop("WORKSPACE_DIR", None)
    else:
        os.environ["WORKSPACE_DIR"] = prev


def test_ac1_hanging_setup_command_killed_at_timeout(tmp_path):
    # Initialize empty git repo to test setup execution
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    res = prepare(
        repo="owner/repo",
        ref="HEAD",
        setup_cmds=["sleep 5"],
        timeout_s=0.2,
        workspace_dir=tmp_path,
        skip_clone=True,
    )

    assert isinstance(res, SetupFailure)
    assert res.timed_out is True
    assert res.command == "sleep 5"


def test_ac2_failure_produces_setup_failure_tail_output(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    # Output more than 4000 chars before failing
    fail_cmd = 'python3 -c "import sys; sys.stdout.write(\'X\' * 5000); sys.stderr.write(\'Y\' * 500); sys.exit(2)"'

    res = prepare(
        repo="owner/repo",
        ref="HEAD",
        setup_cmds=[fail_cmd],
        timeout_s=5.0,
        workspace_dir=tmp_path,
        skip_clone=True,
    )

    assert isinstance(res, SetupFailure)
    assert res.timed_out is False
    assert res.returncode == 2
    assert len(res.output) <= 4000
    assert len(res.output) > 0


def test_ac3_checkout_is_exactly_requested_sha(tmp_path):
    # Setup origin repo with 2 commits
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(["git", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=origin_dir, check=True)

    (origin_dir / "file1.txt").write_text("commit 1")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "commit 1"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    (origin_dir / "file2.txt").write_text("commit 2")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    assert sha1 != sha2

    # Workspace directory to prepare
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    res = prepare(
        repo="owner/repo",
        ref=sha1,
        setup_cmds=[],
        timeout_s=5.0,
        workspace_dir=workspace,
        clone_url=str(origin_dir),
    )

    assert isinstance(res, Workspace)
    assert res.commit_sha == sha1
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    assert head_sha == sha1


def test_wp3_ac1_worktree_isolation_across_planner_developer_evaluator(tmp_path, monkeypatch):
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(["git", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=origin_dir, check=True)
    (origin_dir / "app.py").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    base_repo = tmp_path / "repo-base"
    wt_root = tmp_path / "worktrees"

    # 1. Planner worktree
    plan_ws = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="planner",
        ref=sha1,
        run_id="run_plan_abcdef1234",
        issue_id=42,
        repo="owner/repo",
        clone_url=str(origin_dir),
    )
    assert isinstance(plan_ws, Workspace)
    assert plan_ws.path == wt_root / "planner-run_plan"
    assert workspace_dir() == str(plan_ws.path)

    # 2. Developer worktree (Round 1)
    dev_ws = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="developer",
        ref=sha1,
        run_id="run_dev1_11112222",
        issue_id=42,
        repo="owner/repo",
        clone_url=str(origin_dir),
    )
    assert isinstance(dev_ws, Workspace)
    assert dev_ws.path == wt_root / "developer-42"
    assert workspace_dir() == str(dev_ws.path)

    # Developer writes nanobot transcripts, research notes, uncommitted file, and a new commit
    (dev_ws.path / "memory").mkdir(parents=True, exist_ok=True)
    (dev_ws.path / "memory" / "history.jsonl").write_text('{"role": "developer_secret"}\n')
    (dev_ws.path / ".nanobot" / "tool-results").mkdir(parents=True, exist_ok=True)
    (dev_ws.path / ".nanobot" / "tool-results" / "out.txt").write_text("dev tool trace")
    (dev_ws.path / ".atobar" / "research").mkdir(parents=True, exist_ok=True)
    (dev_ws.path / ".atobar" / "research" / "notes.md").write_text("dev private hypothesis")
    (dev_ws.path / "uncommitted.txt").write_text("should not be seen by evaluator")

    # Simulate developer pushing a commit to origin
    (origin_dir / "app.py").write_text("v2\n")
    subprocess.run(["git", "add", "app.py"], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "feat: v2"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    # 3. Evaluator worktree at sha2 — physically isolated from developer's worktree
    eval_ws = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="evaluator",
        ref=sha2,
        run_id="run_eval_99998888",
        issue_id=42,
        repo="owner/repo",
        clone_url=str(origin_dir),
    )
    assert isinstance(eval_ws, Workspace)
    assert eval_ws.path == wt_root / "evaluator-run_eval"
    assert eval_ws.commit_sha == sha2
    assert workspace_dir() == str(eval_ws.path)
    assert (eval_ws.path / "app.py").read_text() == "v2\n"
    assert not (eval_ws.path / "memory" / "history.jsonl").exists()
    assert not (eval_ws.path / ".nanobot").exists()
    assert not (eval_ws.path / ".atobar" / "research").exists()
    assert not (eval_ws.path / "uncommitted.txt").exists()

    # 4. Developer Round 2 reuses /worktrees/developer-42 with preserved state
    dev_ws_r2 = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="developer",
        ref=sha2,
        run_id="run_dev2_33334444",
        issue_id=42,
        repo="owner/repo",
        clone_url=str(origin_dir),
    )
    assert isinstance(dev_ws_r2, Workspace)
    assert dev_ws_r2.path == dev_ws.path
    assert (dev_ws_r2.path / "memory" / "history.jsonl").exists()
    assert (dev_ws_r2.path / ".atobar" / "research" / "notes.md").exists()


def test_wp3_ac2_setup_complete_marker_skips_duplicate_setup(tmp_path):
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(["git", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=origin_dir, check=True)
    (origin_dir / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    counter_file = tmp_path / "setup_counter.txt"
    setup_cmds = [f"echo run >> {counter_file}"]
    base_repo = tmp_path / "repo-base"
    wt_root = tmp_path / "worktrees"

    # First role turn executes setup_cmds and writes .atobar/.setup_complete_<sha>
    ws1 = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="planner",
        ref=sha,
        run_id="run_11112222",
        issue_id=7,
        repo="owner/repo",
        setup_cmds=setup_cmds,
        clone_url=str(origin_dir),
    )
    assert isinstance(ws1, Workspace)
    assert (base_repo / ".atobar" / f".setup_complete_{sha}").exists()
    assert counter_file.read_text().strip().splitlines() == ["run"]

    # Second warm role turn on same SHA skips setup_cmds
    ws2 = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="developer",
        ref=sha,
        run_id="run_33334444",
        issue_id=7,
        repo="owner/repo",
        setup_cmds=setup_cmds,
        clone_url=str(origin_dir),
    )
    assert isinstance(ws2, Workspace)
    assert counter_file.read_text().strip().splitlines() == ["run"]


def test_wp3_ac3_nanobot_config_and_git_exclude_in_worktree(tmp_path, monkeypatch):
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(["git", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=origin_dir, check=True)
    (origin_dir / "main.py").write_text("print('ok')\n")
    subprocess.run(["git", "add", "."], cwd=origin_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=origin_dir, check=True, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=origin_dir, text=True).strip()

    base_repo = tmp_path / "repo-base"
    wt_root = tmp_path / "worktrees"
    ws = prepare_role_worktree(
        base_repo_dir=base_repo,
        worktrees_root=wt_root,
        role="developer",
        ref=sha,
        run_id="run_dev_12345678",
        issue_id=99,
        repo="owner/repo",
        clone_url=str(origin_dir),
    )
    assert isinstance(ws, Workspace)
    assert os.environ.get("WORKSPACE_DIR") == str(ws.path)

    cfg_path, agent_ws = render_nanobot_run_config(
        run_id="run_dev_12345678",
        role="developer",
        tenant_provider="gemini",
        role_model="gemini-3.8-flash",
        system_prompt="You are dev",
        base_dir=str(tmp_path / "flow-run"),
        workspace_dir=str(ws.path),
    )
    assert agent_ws == tmp_path / "flow-run" / "run_dev_12345678" / "agent_ws"
    assert (agent_ws / "SOUL.md").exists()
    assert not (ws.path / "SOUL.md").exists()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ws.path, text=True).strip()
    assert dirty == ""

