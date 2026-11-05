"""Per-role WIP state synchronization and preemption recovery."""

import asyncio
import json
import os
import subprocess
from typing import TYPE_CHECKING, Any, Optional, cast

from flow_agent.core.config import get_flow_config

if TYPE_CHECKING:
    from flow_agent.telemetry.session_registry import SessionRegistry


class WipSyncer:
    """Manages periodic git-backed state synchronization and bootup resume per role."""

    def __init__(
        self,
        role: str = "developer",
        tenant_slug: str = "",
        issue_id: int = 0,
        registry: Optional["SessionRegistry"] = None,
        workspace_dir: Optional[str] = None,
        _subprocess: Optional[Any] = None,
    ) -> None:
        self.role = role
        self.tenant_slug = tenant_slug
        self.issue_id = issue_id
        self.registry = registry
        self._subprocess = _subprocess or subprocess

        if workspace_dir is not None:
            self.workspace_dir = workspace_dir
        else:
            default_dir = "/home/ubuntu/workspace/"
            self.workspace_dir = default_dir if os.path.exists(default_dir) else os.getcwd()

        self._sync_event = asyncio.Event()

    @property
    def branch_ref(self) -> str:
        """Returns the role-scoped WIP git branch name."""
        wip_prefix = get_flow_config().get("wip_state_branch_prefix", "flow-wip/")
        prefix = wip_prefix.rstrip("/")
        if self.role and self.tenant_slug:
            return f"{prefix}/{self.role}/{self.tenant_slug}/{self.issue_id}"
        if self.role:
            return f"{prefix}/{self.role}/{self.issue_id}"
        return f"{wip_prefix}{self.issue_id}"

    def trigger_sync(self) -> None:
        """Signals the background syncer daemon to sync immediately."""
        self._sync_event.set()

    async def sync_once(self) -> None:
        """Pushes a snapshot of the workspace to the WIP branch without touching the work branch.

        The snapshot is built in a throwaway index so HEAD, the real index and the
        agent's commit history stay exactly as the agent left them.
        """
        if self.issue_id <= 0:
            return
        try:
            git_dir = self._subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                cwd=self.workspace_dir, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if not git_dir:
                return
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(git_dir, "flow-wip.index")}

            def git(*args: str) -> str:
                res = self._subprocess.run(
                    ["git", *args], cwd=self.workspace_dir, env=env,
                    capture_output=True, text=True, check=False,
                )
                return str(res.stdout).strip() if res.returncode == 0 else ""

            git("read-tree", "HEAD")
            git("add", "-A")
            tree = git("write-tree")
            commit = git("commit-tree", tree, "-p", "HEAD", "-m", "Auto-WIP state sync") if tree else ""
            if commit:
                git("push", "--force", "origin", f"{commit}:refs/heads/{self.branch_ref}")

            if self.registry is not None:
                await self.registry.heartbeat()
        except Exception:
            pass

    async def run_daemon(self) -> None:
        """Continuous background daemon syncing state every 60s or upon trigger."""
        while True:
            try:
                try:
                    await asyncio.wait_for(self._sync_event.wait(), timeout=60.0)
                    self._sync_event.clear()
                except asyncio.TimeoutError:
                    pass

                await self.sync_once()
            except Exception:
                pass

    async def attempt_resume(self) -> Optional[dict[str, Any]]:
        """Checks remote WIP branch for this role and restores .atobar/state.json if present."""
        branch = self.branch_ref
        try:
            res = self._subprocess.run(
                ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )

            # Fallback to legacy branch name if role-scoped branch not found
            if res.returncode != 0:
                legacy_branch = f"flow-wip/{self.issue_id}"
                if branch != legacy_branch:
                    res = self._subprocess.run(
                        ["git", "ls-remote", "--exit-code", "--heads", "origin", legacy_branch],
                        cwd=self.workspace_dir,
                        capture_output=True,
                        text=True,
                    )
                    if res.returncode == 0:
                        branch = legacy_branch

            if res.returncode == 0:
                self._subprocess.run(
                    ["git", "fetch", "origin", branch],
                    cwd=self.workspace_dir,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._subprocess.run(
                    ["git", "checkout", "-B", branch, "FETCH_HEAD"],
                    cwd=self.workspace_dir,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                state_path = os.path.join(self.workspace_dir, ".atobar", "state.json")
                if os.path.exists(state_path):
                    with open(state_path, "r", encoding="utf-8") as f:
                        data = cast(dict[str, Any], json.load(f))
                        try:
                            from flow_agent.telemetry.event_sink import emit
                            emit("lifecycle.resume", resumed=True, source_branch=branch)
                        except Exception:
                            pass
                        return data
        except Exception:
            pass

        try:
            from flow_agent.telemetry.event_sink import emit
            emit("lifecycle.resume", resumed=False, source_branch=self.branch_ref)
        except Exception:
            pass
        return None
