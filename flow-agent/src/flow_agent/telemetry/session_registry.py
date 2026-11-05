import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx


class SessionRegistry:
    """Central Control Plane client (Axiom 10 — Central Session Registry).

    Reports lightweight, ephemeral session heartbeats to the CP so the fleet
    has a centralized, real-time view of every running agent.
    Durable recovery state remains in Git (``flow-wip/<issue-id>``); this class is
    purely observability/coordination and never becomes a source of truth.

    Degrades gracefully to a no-op when Control Panel URL or run credentials
    are absent (local runs, tests), so the agent loop never crashes.
    """

    def __init__(
        self,
        instance_id: str,
        target_repo: str,
        issue_id: int,
    ) -> None:
        self.instance_id = instance_id
        self.target_repo = target_repo
        self.issue_id = issue_id

        self.started_at = datetime.now(timezone.utc)
        self.status = "Running"
        self.current_todo: Optional[str] = None
        self.last_tool: Optional[str] = None
        self.cost_estimate: float = 0.0
        self.iterations: int = 0
        self.last_activity_at: str = datetime.now(timezone.utc).isoformat()
        self.usage: Optional[dict[str, Any]] = None
        self.halt_reason: Optional[str] = None
        self.halt_detail: Optional[str] = None
        self.agent_runtime: str = os.environ.get("FLOW_AGENT_RUNTIME", "nanobot")

        self.cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL")
        self.run_id = os.environ.get("FLOW_RUN_ID")
        self.run_cred = os.environ.get("FLOW_RUN_CREDENTIAL")

        self.enabled = bool(self.cp_url and self.run_id and self.run_cred)

        # Background heartbeat state (see heartbeat_nowait). The CP round-trip can take
        # seconds, so the agent loop must never await it on the critical path.
        self._bg_task: Optional[asyncio.Task[None]] = None
        self._bg_resend = False
        self._last_bg_sent: Optional[float] = None

    def _payload(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "current_todo": self.current_todo,
            "last_tool": self.last_tool,
            "cost_estimate": self.cost_estimate,
            "instance_id": self.instance_id,
            "target_repo": self.target_repo,
            "issue_id": self.issue_id,
            "agent_runtime": self.agent_runtime,
            "iterations": self.iterations,
            "last_activity_at": self.last_activity_at,
            "usage": self.usage,
            "halt_reason": self.halt_reason,
            "halt_detail": self.halt_detail,
            "head_sha": os.environ.get("FLOW_HEAD_SHA"),
        }

    def _upsert_sync(self) -> None:
        """Blocking upsert. Runs off the event loop via :meth:`_upsert`."""
        if not self.enabled:
            return
        try:
            url = f"{self.cp_url.rstrip('/') if self.cp_url else ''}/v1/runs/{self.run_id}/heartbeat"
            headers = {"Authorization": f"Bearer {self.run_cred}"}
            resp = httpx.post(url, json=self._payload(), headers=headers, timeout=5.0)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logging.warning("Heartbeat failed for run_id=%s, status=%s: %s", self.run_id, status_code, body)
        except Exception as exc:
            logging.warning("Heartbeat error for run_id=%s: %s", self.run_id, str(exc)[:500])

    async def _upsert(self) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._upsert_sync)

    async def register(self) -> None:
        """Create/refresh this session's row on agent boot (Phase 0)."""
        self.status = "Running"
        await self._upsert()

    async def heartbeat(
        self,
        current_todo: Optional[str] = None,
        last_tool: Optional[str] = None,
        cost_estimate: Optional[float] = None,
        iterations: Optional[int] = None,
        usage: Optional[dict[str, Any]] = None,
        halt_reason: Optional[str] = None,
        halt_detail: Optional[str] = None,
    ) -> None:
        """Push a periodic heartbeat, optionally updating tracked fields."""
        if current_todo is not None:
            self.current_todo = current_todo
        if last_tool is not None:
            self.last_tool = last_tool
        if cost_estimate is not None:
            self.cost_estimate = cost_estimate
        if iterations is not None:
            self.iterations = iterations
        if usage is not None:
            self.usage = usage
        if halt_reason is not None:
            self.halt_reason = halt_reason
        if halt_detail is not None:
            self.halt_detail = halt_detail
        # Let an in-flight background send (built from older state) land first.
        await self.drain()
        self.last_activity_at = datetime.now(timezone.utc).isoformat()
        self._last_bg_sent = time.monotonic()
        await self._upsert()

    def heartbeat_nowait(self, *, min_interval_s: float = 15.0, force: bool = False) -> None:
        """Schedule a heartbeat in the background without blocking the caller.

        At most one send is in flight. Unforced calls are dropped within
        ``min_interval_s`` of the last send; a forced call (e.g. a todo change) that
        lands while a send is in flight is coalesced into one follow-up send, so the
        latest state always reaches the CP. Requires a running event loop.
        """
        if not self.enabled:
            return
        if self._bg_task is not None and not self._bg_task.done():
            self._bg_resend = self._bg_resend or force
            return
        now = time.monotonic()
        if not force and self._last_bg_sent is not None and now - self._last_bg_sent < min_interval_s:
            return
        self._last_bg_sent = now
        self._bg_task = asyncio.get_running_loop().create_task(self._bg_heartbeat())

    async def _bg_heartbeat(self) -> None:
        while True:
            self._bg_resend = False
            self.last_activity_at = datetime.now(timezone.utc).isoformat()
            await self._upsert()
            if not self._bg_resend:
                return
            self._last_bg_sent = time.monotonic()

    async def drain(self) -> None:
        """Wait for any in-flight background heartbeat to finish."""
        task = self._bg_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    async def terminate(self, status: str = "Complete", reason: Optional[str] = None) -> None:
        """Transition the session to a terminal state (``Complete``/``Halted``)."""
        # A background "Running" heartbeat finishing after this one would overwrite the
        # terminal status on the CP, so let it land first.
        await self.drain()
        self.status = status
        if reason:
            self.halt_detail = reason
        self.last_activity_at = datetime.now(timezone.utc).isoformat()
        await self._upsert()
