"""Role sandbox workspace preparation, isolated git worktrees, and setup command runner."""

import os
import pathlib
import shutil
import subprocess
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict

from atobar_contracts.manifest import validate_compiled_metric_command


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    ref: str
    path: pathlib.Path
    commit_sha: str


class SetupFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    ref: str
    command: str
    returncode: int
    output: str
    timed_out: bool = False


def _resolve_clone_url(repo: str, clone_url: Optional[str], token: str) -> str:
    if clone_url:
        return clone_url
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def _has_setup_marker(marker_dir: pathlib.Path, exact_sha: str) -> bool:
    atobar_dir = marker_dir / ".atobar"
    return (
        (atobar_dir / f".setup_complete_{exact_sha}").exists()
        or (atobar_dir / ".setup_complete_base").exists()
    )


def _write_setup_marker(marker_dir: pathlib.Path, exact_sha: str) -> None:
    try:
        atobar_dir = marker_dir / ".atobar"
        atobar_dir.mkdir(parents=True, exist_ok=True)
        (atobar_dir / f".setup_complete_{exact_sha}").write_text(exact_sha + "\n", encoding="utf-8")
    except Exception:
        pass


def _run_setup_commands(
    repo: str,
    ref: str,
    target_path: pathlib.Path,
    marker_dir: pathlib.Path,
    exact_sha: str,
    setup_cmds: list[str],
    timeout_s: float,
) -> Optional[SetupFailure]:
    if not setup_cmds or _has_setup_marker(marker_dir, exact_sha) or _has_setup_marker(target_path, exact_sha):
        return None

    for cmd in setup_cmds:
        problems = validate_compiled_metric_command(cmd)
        if problems:
            return SetupFailure(
                repo=repo,
                ref=ref,
                command=cmd,
                returncode=1,
                output="; ".join(problems),
                timed_out=False,
            )
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=target_path,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode != 0:
                combined = (proc.stdout or "") + (proc.stderr or "")
                tail = combined[-4000:]
                return SetupFailure(
                    repo=repo,
                    ref=ref,
                    command=cmd,
                    returncode=proc.returncode,
                    output=tail,
                    timed_out=False,
                )
        except subprocess.TimeoutExpired as exc:
            stdout_str = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr_str = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            tail = (stdout_str + stderr_str)[-4000:]
            return SetupFailure(
                repo=repo,
                ref=ref,
                command=cmd,
                returncode=-1,
                output=tail,
                timed_out=True,
            )

    _write_setup_marker(marker_dir, exact_sha)
    return None


def _activate_workspace_dir(path: pathlib.Path) -> None:
    resolved = str(path.resolve())
    os.environ["WORKSPACE_DIR"] = resolved
    try:
        import flow_agent.tools.git_tools as git_tools

        git_tools.WORKSPACE_DIR = resolved
    except Exception:
        pass


def prepare(
    repo: str,
    ref: str,
    setup_cmds: list[str],
    timeout_s: float = 300.0,
    workspace_dir: Optional[Union[str, pathlib.Path]] = None,
    clone_url: Optional[str] = None,
    token: str = "",
    skip_clone: bool = False,
) -> Union[Workspace, SetupFailure]:
    """Clones the repository at exact ref and runs setup commands under timeouts."""
    if workspace_dir is not None:
        target_path = pathlib.Path(workspace_dir)
    else:
        target_path = pathlib.Path("/home/ubuntu/workspace/")

    target_path.mkdir(parents=True, exist_ok=True)

    if not skip_clone:
        url = _resolve_clone_url(repo, clone_url, token)
        git_dir = target_path / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "clone", url, str(target_path)],
                check=True,
                capture_output=True,
                text=True,
            )

        # Fetch and checkout exact ref
        subprocess.run(
            ["git", "fetch", "origin", ref],
            cwd=target_path,
            check=False,
            capture_output=True,
            text=True,
        )
        checkout_res = subprocess.run(
            ["git", "checkout", ref],
            cwd=target_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if checkout_res.returncode != 0:
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD"],
                cwd=target_path,
                check=True,
                capture_output=True,
                text=True,
            )

    try:
        exact_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=target_path,
            text=True,
        ).strip()
    except Exception as e:
        return SetupFailure(
            repo=repo,
            ref=ref,
            command="git rev-parse HEAD",
            returncode=-1,
            output=str(e),
            timed_out=False,
        )

    failure = _run_setup_commands(
        repo=repo,
        ref=ref,
        target_path=target_path,
        marker_dir=target_path,
        exact_sha=exact_sha,
        setup_cmds=setup_cmds,
        timeout_s=timeout_s,
    )
    if failure is not None:
        return failure

    return Workspace(
        repo=repo,
        ref=ref,
        path=target_path,
        commit_sha=exact_sha,
    )


def prepare_role_worktree(
    base_repo_dir: Union[str, pathlib.Path] = "/home/ubuntu/workspace",
    worktrees_root: Union[str, pathlib.Path] = "/home/ubuntu/worktrees",
    role: str = "developer",
    ref: str = "HEAD",
    run_id: str = "",
    issue_id: Union[int, str] = 0,
    repo: str = "owner/repo",
    setup_cmds: Optional[list[str]] = None,
    timeout_s: float = 300.0,
    clone_url: Optional[str] = None,
    token: str = "",
) -> Union[Workspace, SetupFailure]:
    """Prepares an isolated git worktree for a swarm role turn on a warm Spot instance.

    - `developer` uses `<worktrees_root>/developer-<issue_id>` (persisted across rework rounds).
    - `planner` and `evaluator` each get a clean detached `<worktrees_root>/<role>-<run_id[:8]>`
      at the exact target `ref`, physically isolated from developer transcripts/files (Axiom 11).
    """
    base_path = pathlib.Path(base_repo_dir)
    wt_root = pathlib.Path(worktrees_root)
    base_path.mkdir(parents=True, exist_ok=True)
    wt_root.mkdir(parents=True, exist_ok=True)

    if not (base_path / ".git").exists():
        url = _resolve_clone_url(repo, clone_url, token)
        subprocess.run(
            ["git", "clone", url, str(base_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    effective_ref = ref if (ref and ref != "0" * 40) else "HEAD"
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=base_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if effective_ref != "HEAD":
        subprocess.run(
            ["git", "fetch", "origin", effective_ref],
            cwd=base_path,
            check=False,
            capture_output=True,
            text=True,
        )

    # Resolve target commit SHA in base_path
    target_sha = ""
    for candidate in (f"{effective_ref}^{{commit}}", f"origin/{effective_ref}^{{commit}}", "FETCH_HEAD", "HEAD"):
        res = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=base_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            target_sha = res.stdout.strip()
            break

    if not target_sha:
        return SetupFailure(
            repo=repo,
            ref=ref,
            command=f"git rev-parse {effective_ref}",
            returncode=-1,
            output=f"Unable to resolve git ref {effective_ref!r} in {base_path}",
            timed_out=False,
        )

    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=base_path,
        check=False,
        capture_output=True,
        text=True,
    )

    role_norm = (role or "developer").strip().lower()
    if role_norm == "developer":
        wt_path = wt_root / f"developer-{issue_id}"
        if not (wt_path / ".git").exists():
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), target_sha],
                cwd=base_path,
                check=True,
                capture_output=True,
                text=True,
            )
    else:
        short_run = (run_id or "run")[:8]
        wt_path = wt_root / f"{role_norm}-{short_run}"
        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )
            shutil.rmtree(wt_path, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_path), target_sha],
            cwd=base_path,
            check=True,
            capture_output=True,
            text=True,
        )

    try:
        exact_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=wt_path,
            text=True,
        ).strip()
    except Exception as e:
        return SetupFailure(
            repo=repo,
            ref=ref,
            command="git rev-parse HEAD",
            returncode=-1,
            output=str(e),
            timed_out=False,
        )

    failure = _run_setup_commands(
        repo=repo,
        ref=ref,
        target_path=wt_path,
        marker_dir=base_path,
        exact_sha=exact_sha,
        setup_cmds=list(setup_cmds or []),
        timeout_s=timeout_s,
    )
    if failure is not None:
        return failure

    _activate_workspace_dir(wt_path)

    return Workspace(
        repo=repo,
        ref=ref,
        path=wt_path,
        commit_sha=exact_sha,
    )
