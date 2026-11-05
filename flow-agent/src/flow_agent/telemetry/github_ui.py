import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx


class GitHubTelemetryUI:
    def __init__(self, target_repo: str, issue_id: int, github_token: str):
        self.target_repo = target_repo
        self.issue_id = issue_id
        self.github_token = github_token
        self.comment_id: Optional[int] = None
        self.start_time = datetime.now(timezone.utc)
        self.status = "🟢 Running"

        self.queue: asyncio.Queue[bool] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task[Any]] = None

        self.agent_state: Dict[str, Any] = {}
        self.total_tokens: int = 0
        self.last_heartbeat_tokens: int = 0

        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {self.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.client = httpx.AsyncClient(headers=self.headers)

    async def initialize(self) -> None:
        """Initializes the telemetry UI by posting the master comment."""
        payload = self.format_payload(self.agent_state)
        url = f"https://api.github.com/repos/{self.target_repo}/issues/{self.issue_id}/comments"
        resp = await self.client.post(url, json={"body": payload})
        resp.raise_for_status()
        self.comment_id = resp.json()["id"]

        self.worker_task = asyncio.create_task(self._worker())

    def update_state(self, agent_state: dict) -> None:
        """Update the internal cognitive plan state."""
        state_copy = dict(agent_state)
        if "delta_tokens" in state_copy:
            self.total_tokens += state_copy.pop("delta_tokens")
        if "total_tokens" in state_copy:
            self.total_tokens = max(self.total_tokens, state_copy.pop("total_tokens"))
        elif "tokens" in state_copy:
            self.total_tokens = max(self.total_tokens, state_copy.pop("tokens"))

        if "screenshots" in state_copy and isinstance(state_copy["screenshots"], list):
            existing_screens = list(self.agent_state.get("screenshots") or [])
            seen_keys = {
                (s.get("url") or s.get("markdown") or str(s)) if isinstance(s, dict) else str(s)
                for s in existing_screens
            }
            for s in state_copy["screenshots"]:
                k = (s.get("url") or s.get("markdown") or str(s)) if isinstance(s, dict) else str(s)
                if k not in seen_keys:
                    existing_screens.append(s)
                    seen_keys.add(k)
            state_copy["screenshots"] = existing_screens

        self.agent_state.update(state_copy)
        self.queue.put_nowait(True)

    def format_payload(self, agent_state: dict, recent_tools: Optional[list] = None) -> str:
        """Formats the payload utilizing collapsible Markdown."""
        elapsed_sec = int((datetime.now(timezone.utc) - self.start_time).total_seconds())
        elapsed_min = elapsed_sec // 60
        max_wc = os.environ.get("FLOW_MAX_WC", "240")
        role_label = agent_state.get("role")
        role_part = f" | Role: `{role_label}`" if role_label else ""

        lines = [
            "### 🌊 flow Agent Telemetry (Live)",
            f"*Status: {self.status}{role_part} | Time Elapsed: {elapsed_min}m / {max_wc}m*",
        ]

        last_tool = agent_state.get("last_tool")
        iterations = agent_state.get("iterations")
        token_info = None
        if self.total_tokens > 0:
            delta = self.total_tokens - self.last_heartbeat_tokens
            token_info = f"Tokens: {self.total_tokens:,} (+{delta:,} since last heartbeat)"

        if last_tool or iterations is not None or token_info:
            info_parts = []
            if iterations is not None:
                info_parts.append(f"Iterations: {iterations}")
            if last_tool:
                info_parts.append(f"Last Tool: `{last_tool}`")
            if token_info:
                info_parts.append(token_info)
            lines.append(f"*{' | '.join(info_parts)}*")

        lines.extend([
            "",
            "#### 📋 Current Cognitive Plan (`todo_write`)",
        ])

        tasks = agent_state.get("tasks", [])
        if not tasks:
            lines.append("*(No plan established yet)*")
        else:
            for task in tasks:
                status = task.get("status", "pending")
                is_done = status in ("completed", "done")
                is_in_progress = status == "in_progress"
                status_box = "- [x]" if is_done else "- [ ]"
                suffix = " *(In Progress)*" if is_in_progress else ""
                content = task.get("content") or task.get("title", "Unknown task")
                lines.append(f"{status_box} {content}{suffix}")

        lines.append("")

        tool_usage = agent_state.get("by_tool") or agent_state.get("tool_usage") or {}
        if isinstance(tool_usage, dict) and tool_usage:
            lines.extend([
                "#### 🔧 Tool Token Breakdown",
                "| Tool | Calls | Tokens (in / out) | Errors |",
                "| :--- | :---: | :---: | :---: |",
            ])
            for tool_name, stats in sorted(
                tool_usage.items(),
                key=lambda kv: (int((kv[1] or {}).get("total_tokens", 0)), int((kv[1] or {}).get("calls", 0))),
                reverse=True,
            ):
                if not isinstance(stats, dict):
                    continue
                calls = int(stats.get("calls", 0) or 0)
                toks = int(stats.get("total_tokens", 0) or 0)
                in_toks = int(stats.get("input_tokens", 0) or 0)
                out_toks = int(stats.get("output_tokens", 0) or 0)
                errs = int(stats.get("errors", 0) or 0)
                lines.append(
                    f"| `{tool_name}` | {calls} | {toks:,} ({in_toks:,} / {out_toks:,}) | {errs} |"
                )
            lines.append("")

        screenshots = agent_state.get("screenshots") or []
        if isinstance(screenshots, list):
            md_lines = []
            for item in screenshots:
                if isinstance(item, dict):
                    prov_md = (item.get("provenance_markdown") or "").strip()
                    md = (item.get("markdown") or "").strip()
                    url = (item.get("url") or "").strip()
                    if prov_md:
                        md_lines.append(prov_md)
                    elif any(k in item for k in ("data_mode", "data_source", "constraints", "scenario", "metadata")):
                        from flow_agent.tools.vision_tools import render_screenshot_with_provenance

                        md_lines.append(render_screenshot_with_provenance(item))
                    elif md:
                        md_lines.append(md)
                    elif url:
                        md_lines.append(f"![Screenshot]({url})")
                elif isinstance(item, str) and item.strip():
                    md_lines.append(item.strip())
            if md_lines:
                lines.append("#### 📸 Captured Screenshots")
                lines.extend(md_lines)
                lines.append("")

        return "\n".join(lines)

    async def _worker(self) -> None:
        """Background worker that throttles API calls."""
        while True:
            await self.queue.get()

            # Flush queue
            while not self.queue.empty():
                self.queue.get_nowait()

            await self._push_update()

            # Throttle to max 1 update every 12 seconds
            await asyncio.sleep(12)

    async def _push_update(self) -> None:
        """Pushes the PATCH update to the GitHub API."""
        if not self.comment_id:
            return

        payload = self.format_payload(self.agent_state)
        url = f"https://api.github.com/repos/{self.target_repo}/issues/comments/{self.comment_id}"
        try:
            resp = await self.client.patch(url, json={"body": payload})
            if hasattr(resp, "raise_for_status"):
                status_res = resp.raise_for_status()
                if asyncio.iscoroutine(status_res):
                    await status_res
            self.last_heartbeat_tokens = self.total_tokens
        except Exception:
            # Ignore transient errors during telemetry updates
            pass

    async def terminate(self, error: bool = False, reason: str = "") -> None:
        """Terminal state update."""
        if self.worker_task:
            self.worker_task.cancel()

        if error:
            self.status = f"🔴 Halted: {reason}"
        else:
            self.status = "✅ Complete"

        await self._push_update()
        await self.client.aclose()
