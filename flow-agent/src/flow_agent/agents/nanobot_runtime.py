"""Nanobot AgentRuntime implementation running swarm roles through nanobot-ai SDK."""

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional, cast

from flow_agent.agents.base import (
    AgentRuntime,
    ModelCallRecord,
    RoleResult,
    RoleTask,
    RuntimeHooks,
    ToolUsageBreakdown,
    UsageDetail,
    UsageLedger,
)
from flow_agent.agents.errors import classify_provider_error
from flow_agent.agents.nanobot_config import render_nanobot_run_config
from flow_agent.core.config import get_bot_logins
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.core.watchdog import CircuitBreakerException
from flow_agent.llm.pricing import calculate_cost
from atobar_contracts.envelopes import FENCE_RE, MODEL_BY_TYPE, Ack, Envelope, Verdict, fingerprint, parse, render

try:
    from nanobot.agent.hook import AgentHook
except ImportError:
    class AgentHook:  # type: ignore[no-redef]
        def wants_streaming(self) -> bool:
            return False

# Minimum seconds between per-iteration liveness heartbeats sent from FlowHook.
_HEARTBEAT_INTERVAL_S = 15.0

# Map nanobot builtin tool names to flow tool equivalents for defense-in-depth allowlist checks
_NANOBOT_TOOL_MAP: dict[str, str] = {
    "read_file": "read_file",
    "view_image": "read_file",
    "write_file": "write_file",
    "edit_file": "multi_edit",
    "apply_patch": "multi_edit",
    "list_dir": "search_codebase",
    "find_files": "search_codebase",
    "grep": "search_codebase",
    "exec": "execute_command",
}

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def _image_viewer_tool(inner: Any, workspace_dir: Optional[str] = None) -> Any:
    """Wrap nanobot's builtin ``read_file`` as an images-only ``view_image`` tool.

    The flow MCP ``read_file`` covers text (with role guards), but nanobot stores MCP
    image results as artifacts instead of showing them to the model, so screenshots
    still need the builtin's multimodal path. Narrowing it to images keeps a single
    tool per job instead of two competing file readers.
    """
    from pathlib import Path

    from nanobot.agent.tools.base import Tool, ToolResult

    from flow_agent.tools.fs_tools import _resolve_path_in_workspace

    ws_root = (workspace_dir or os.environ.get("WORKSPACE_DIR") or "").strip()
    if ws_root:
        ws_path = Path(ws_root).expanduser().resolve(strict=False)
        if hasattr(inner, "_workspace"):
            inner._workspace = ws_path
        if hasattr(inner, "_allowed_dir"):
            inner._allowed_dir = ws_path

    class ViewImageTool(Tool):
        @property
        def name(self) -> str:
            return "view_image"

        @property
        def description(self) -> str:
            return (
                "Look at an image file (PNG/JPEG/GIF/WebP screenshot) by path. "
                "For text files use mcp_flow_read_file instead."
            )

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Image file path"}},
                "required": ["path"],
            }

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, path: str | None = None, **kwargs: Any) -> Any:
            if not path or os.path.splitext(path)[1].lower() not in _IMAGE_SUFFIXES:
                return ToolResult.error(
                    f"view_image only opens image files; use mcp_flow_read_file for {path!r}."
                )
            eff_ws = (workspace_dir or os.environ.get("WORKSPACE_DIR") or "").strip() or None
            try:
                resolved = _resolve_path_in_workspace(path, workspace_dir=eff_ws, allow_overflow=False)
            except PermissionError as exc:
                return ToolResult.error(str(exc))
            return await inner.execute(path=resolved)

    return ViewImageTool()


def _restrict_builtin_tools(
    bot: Any,
    allowed: frozenset[str],
    workspace_dir: Optional[str] = None,
) -> None:
    """Leave the model one tool per job: the flow MCP tools plus an images-only viewer.

    nanobot isolates hook exceptions, so the FlowHook allowlist check alone cannot
    stop a call; the model must never be offered disallowed tools. Builtins that
    duplicate a flow MCP tool are removed too: two readers/shells with different
    schemas (``path`` vs ``file_path``, an ``exec`` without the workspace venv on
    PATH) made the model alternate between them and burn turns.
    """
    registry = getattr(getattr(bot, "_loop", None), "tools", None)
    names = getattr(registry, "tool_names", None)
    if registry is None or not isinstance(names, list):
        return
    for name in list(names):
        if name.startswith("mcp_"):
            continue
        if name == "read_file" and "read_file" in allowed:
            inner = registry.get(name)
            registry.unregister(name)
            if inner is not None:
                registry.register(_image_viewer_tool(inner, workspace_dir=workspace_dir))
            continue
        registry.unregister(name)


def _has_unpublished_work(workspace_dir: str) -> bool:
    """True if the workspace holds uncommitted changes or commits not on the default branch."""
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace_dir, capture_output=True, text=True,
    ).stdout.strip()
    return bool(dirty) or _commits_ahead_of_base(workspace_dir) > 0


def _commits_ahead_of_base(workspace_dir: str) -> int:
    """Number of local commits on HEAD that are not on the client's default branch."""
    base = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=workspace_dir, capture_output=True, text=True,
    ).stdout.strip() or "origin/main"
    res = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=workspace_dir, capture_output=True, text=True,
    )
    return int(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip().isdigit() else 0

_GENERIC_FENCE_RE = re.compile(
    r"```(?:flow-)?(ack|verdict|arbitration|plan|escalation)\s*\n(.*?)\n```",
    re.DOTALL,
)


def _extract_envelope_from_text_or_messages(
    content: str,
    messages: list[dict[str, Any]],
    done_envelope: str,
    role: str,
) -> Optional[str]:
    """Extracts fenced envelope text using protocol parser logic with schema repair."""
    expected_tag = done_envelope.replace("flow-", "")

    # Check content first
    candidates = [content]
    for msg in reversed(messages):
        c = msg.get("content")
        if isinstance(c, str):
            candidates.append(c)

    bots = get_bot_logins()
    bot_login = bots.get(role, "atobar-flow-agent")

    def _valid_candidate(env_obj: Any, raw_fallback: Optional[str] = None) -> Optional[str]:
        if env_obj is None:
            return None
        if isinstance(env_obj, Verdict):
            from flow_agent.roles.evaluator.spec import validate_evaluator_finding

            try:
                for f in env_obj.findings:
                    validate_evaluator_finding(f)
                    f.fingerprint = fingerprint(f)
            except ValueError:
                return None
            return render(cast("Envelope", env_obj)).strip()
        return raw_fallback or render(cast("Envelope", env_obj)).strip()

    # Pass 1: Strict parse
    for text in candidates:
        if not text:
            continue
        matches = list(FENCE_RE.finditer(text))
        for m in reversed(matches):
            if m.group(1) == expected_tag:
                raw_block = m.group(0).strip()
                parsed = parse(raw_block, author_login=bot_login, bots=bots)
                checked = _valid_candidate(parsed, raw_fallback=raw_block)
                if checked is not None:
                    return checked

    # Pass 2: Resilient repair (normalize role, schema validate, and re-render standard fence)
    for text in candidates:
        if not text:
            continue
        matches = list(_GENERIC_FENCE_RE.finditer(text))
        for m in reversed(matches):
            if m.group(1) == expected_tag:
                try:
                    payload = json.loads(m.group(2).strip())
                    if isinstance(payload, dict):
                        if "role" not in payload:
                            payload["role"] = role
                        model_cls = MODEL_BY_TYPE.get(expected_tag)
                        if model_cls:
                            val = model_cls.model_validate(payload)
                            rendered = render(cast("Envelope", val)).strip()
                            parsed = parse(rendered, author_login=bot_login, bots=bots)
                            checked = _valid_candidate(parsed)
                            if checked is not None:
                                return checked
                except Exception:
                    pass

    return None


class FlowHook(AgentHook):
    """Nanobot AgentHook bridging factory obligations."""

    def __init__(self, task: RoleTask, hooks: RuntimeHooks) -> None:
        super().__init__()
        self.task = task
        self.hooks = hooks
        self.last_activity_at = datetime.now(timezone.utc)
        self.iterations = 0
        self.recorded_calls: list[ModelCallRecord] = []
        self.by_tool: dict[str, ToolUsageBreakdown] = {}
        self._turn_tool_names: list[str] = []
        self._turn_tools_started_at: Optional[datetime] = None
        self._iter_started_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    @staticmethod
    def _normalize_tool_name(call_name: str) -> str:
        normalized_name = call_name
        if call_name.startswith("mcp_flow_"):
            normalized_name = call_name[len("mcp_flow_"):]
        elif call_name.startswith("flow__") or call_name.startswith("flow:"):
            normalized_name = call_name.split("__", 1)[-1].split(":", 1)[-1]
        elif call_name in _NANOBOT_TOOL_MAP:
            normalized_name = _NANOBOT_TOOL_MAP[call_name]
        return normalized_name

    def build_ledger(self) -> UsageLedger:
        recorded = self.recorded_calls
        total_in = sum(c.input_tokens for c in recorded)
        total_out = sum(c.output_tokens for c in recorded)
        total_cost = sum(c.cost_usd for c in recorded)
        total_cached = (
            sum(c.cached_input_tokens for c in recorded if c.cached_input_tokens is not None)
            if any(c.cached_input_tokens is not None for c in recorded)
            else None
        )
        total_write = (
            sum(c.cache_write_tokens for c in recorded if c.cache_write_tokens is not None)
            if any(c.cache_write_tokens is not None for c in recorded)
            else None
        )
        return UsageLedger(
            total=UsageDetail(
                input_tokens=total_in,
                output_tokens=total_out,
                cost_usd=total_cost,
                cached_input_tokens=total_cached,
                cache_write_tokens=total_write,
                total_tokens=total_in + total_out,
            ),
            calls=list(recorded),
            by_tool=dict(self.by_tool),
        )

    async def before_iteration(self, context: Any) -> None:
        self.last_activity_at = datetime.now(timezone.utc)
        self._iter_started_at = self.last_activity_at
        self.iterations += 1
        self._turn_tool_names = []
        self._turn_tools_started_at = None

        if self.hooks.circuit_breaker is not None:
            self.hooks.circuit_breaker.check_time()

        if self.hooks.registry is not None:
            # Never block the LLM call on the CP round-trip; throttled liveness only.
            self.hooks.registry.iterations = self.iterations
            self.hooks.registry.heartbeat_nowait(min_interval_s=_HEARTBEAT_INTERVAL_S)

    async def before_execute_tools(self, context: Any) -> None:
        now = datetime.now(timezone.utc)
        self.last_activity_at = now
        self._turn_tools_started_at = now
        tool_calls = getattr(context, "tool_calls", [])
        turn_names: list[str] = []

        for call in tool_calls:
            call_name = getattr(call, "name", "")
            normalized_name = self._normalize_tool_name(call_name)

            if normalized_name not in self.task.allowed_tools:
                raise PermissionError(
                    f"Tool '{call_name}' (normalized: '{normalized_name}') is not allowed for role '{self.task.role}'"
                )

            turn_names.append(normalized_name)
            entry = self.by_tool.setdefault(normalized_name, ToolUsageBreakdown())
            entry.calls += 1

            if self.hooks.registry is not None:
                self.hooks.registry.last_tool = normalized_name

            args = getattr(call, "arguments", {}) or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if normalized_name == "todo_write" and isinstance(args, dict):
                todos = args.get("todos", [])
                if self.hooks.telemetry_ui is not None:
                    self.hooks.telemetry_ui.update_state({"tasks": todos})
                if self.hooks.registry is not None:
                    for t in todos:
                        if t.get("status") == "in_progress":
                            self.hooks.registry.current_todo = t.get("content") or t.get("title")
                            break
                    self.hooks.registry.heartbeat_nowait(force=True)

            if self.hooks.telemetry_ui is not None:
                self.hooks.telemetry_ui.update_state(
                    {
                        "last_tool": normalized_name,
                        "iterations": self.iterations,
                        "by_tool": {k: v.to_dict() for k, v in self.by_tool.items()},
                    }
                )

        self._turn_tool_names = turn_names

    async def after_execute_tool(
        self,
        context: Any,
        call: Any = None,
        tool: Any = None,
        args: Any = None,
        result: Any = None,
    ) -> None:
        call_name = getattr(call, "name", "") if call is not None else ""
        if not call_name:
            return
        tname = self._normalize_tool_name(call_name)
        entry = self.by_tool.setdefault(tname, ToolUsageBreakdown())
        if result is not None:
            res_str = str(result)
            entry.result_chars += len(res_str)
            if res_str:
                entry.result_tokens += max(1, len(res_str) // 4)
            if res_str.startswith("Error:") or '"status": "error"' in res_str:
                entry.errors += 1

    async def after_iteration(self, context: Any) -> None:
        now = datetime.now(timezone.utc)
        self.last_activity_at = now

        ctx_calls = getattr(context, "tool_calls", None) or []
        turn_tools = list(self._turn_tool_names)
        if not turn_tools and ctx_calls:
            for call in ctx_calls:
                cname = self._normalize_tool_name(getattr(call, "name", ""))
                if cname:
                    turn_tools.append(cname)
                    entry = self.by_tool.setdefault(cname, ToolUsageBreakdown())
                    entry.calls += 1

        tool_results = getattr(context, "tool_results", None) or []
        elapsed_ms = (
            int((now - self._turn_tools_started_at).total_seconds() * 1000)
            if self._turn_tools_started_at is not None
            else 0
        )
        if turn_tools:
            per_tool_ms = elapsed_ms // len(turn_tools) if elapsed_ms > 0 else 0
            for idx, tname in enumerate(turn_tools):
                entry = self.by_tool.setdefault(tname, ToolUsageBreakdown())
                entry.duration_ms += per_tool_ms
                if idx < len(tool_results) and tool_results[idx] is not None and entry.result_chars == 0:
                    res_str = str(tool_results[idx])
                    entry.result_chars += len(res_str)
                    if res_str:
                        entry.result_tokens += max(1, len(res_str) // 4)
                    if res_str.startswith("Error:") or '"status": "error"' in res_str:
                        entry.errors += 1

        ctx_err = getattr(context, "error", None)
        if ctx_err:
            self.last_error = str(ctx_err)

        usage = getattr(context, "usage", None)
        if usage is not None:
            in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            cache_read = getattr(usage, "cache_read_tokens", None)
            cache_write = getattr(usage, "cache_write_tokens", None)
            cost = calculate_cost(self.task.model_name, in_tokens, out_tokens)
            iter_latency_ms = (
                (now - self._iter_started_at).total_seconds() * 1000.0
                if self._iter_started_at is not None
                else None
            )

            rec = ModelCallRecord(
                iteration=self.iterations,
                provider="nanobot",
                model=self.task.model_name,
                input_tokens=in_tokens,
                cached_input_tokens=cache_read,
                cache_write_tokens=cache_write,
                output_tokens=out_tokens,
                cost_usd=cost,
                latency_ms=iter_latency_ms,
                stop_reason=getattr(context, "stop_reason", None),
            )
            self.recorded_calls.append(rec)

            if turn_tools:
                count = len(turn_tools)
                tot_tokens = in_tokens + out_tokens
                for idx, tname in enumerate(turn_tools):
                    entry = self.by_tool.setdefault(tname, ToolUsageBreakdown())
                    entry.input_tokens += (in_tokens // count) + (1 if idx < (in_tokens % count) else 0)
                    entry.output_tokens += (out_tokens // count) + (1 if idx < (out_tokens % count) else 0)
                    entry.total_tokens += (tot_tokens // count) + (1 if idx < (tot_tokens % count) else 0)
                    entry.cost_usd += cost / count

            try:
                from flow_agent.telemetry.event_sink import emit
                emit(
                    "model_call",
                    iteration=self.iterations,
                    provider="nanobot",
                    model=self.task.model_name,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cached_input_tokens=cache_read,
                    cost_usd=cost,
                    tools=turn_tools,
                )
            except Exception:
                pass

            ledger_snap = self.build_ledger()
            if self.hooks.registry is not None:
                self.hooks.registry.usage = ledger_snap.to_dict()

            if self.hooks.telemetry_ui is not None:
                from flow_agent.tools.vision_tools import load_recorded_screenshots

                total_tokens = sum(r.input_tokens + r.output_tokens for r in self.recorded_calls)
                state_update: dict[str, Any] = {
                    "total_tokens": total_tokens,
                    "by_tool": {k: v.to_dict() for k, v in self.by_tool.items()},
                }
                screens = load_recorded_screenshots(self.task.workspace_dir)
                if screens:
                    state_update["screenshots"] = screens
                self.hooks.telemetry_ui.update_state(state_update)

        self._turn_tool_names = []
        self._turn_tools_started_at = None


class NanobotRuntime(AgentRuntime):
    """Executes a RoleTask through nanobot SDK."""

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        import nanobot

        from flow_agent.agents import nanobot_patches

        nanobot_patches.install()

        run_id = os.environ.get("FLOW_RUN_ID", "default")
        provider = (
            os.environ.get(f"FLOW_{task.role.upper()}_PROVIDER")
            or os.environ.get("FLOW_LLM_PROVIDER", "gemini")
        )

        # Multi-provider fan-out: any FLOW_LLM_API_KEY_<PROVIDER> exported by
        # key_delivery becomes an extra entry in the nanobot providers map.
        extra_providers = {
            env[len("FLOW_LLM_API_KEY_"):].lower()
            for env, value in os.environ.items()
            if env.startswith("FLOW_LLM_API_KEY_") and value
        }

        cfg_path, nanobot_home = render_nanobot_run_config(
            run_id=run_id,
            role=task.role,
            tenant_provider=provider,
            role_model=task.model_name,
            system_prompt=task.system_prompt,
            workspace_dir=task.workspace_dir,
            extra_providers=extra_providers or None,
            allowed_tools=task.allowed_tools,
        )

        bot = nanobot.Nanobot.from_config(config_path=cfg_path, workspace=nanobot_home)
        _restrict_builtin_tools(bot, task.allowed_tools, workspace_dir=task.workspace_dir)

        flow_hook = FlowHook(task=task, hooks=hooks)

        # Build task message
        wrapped_prompt = (
            task.task_prompt
            if "<untrusted_client_content" in task.task_prompt
            else wrap_untrusted(task.task_prompt, source="issue_specification")
        )
        task_message = (
            f"SYSTEM KNOWLEDGE:\n{task.context}\n\nSTART TASK:\n{wrapped_prompt}"
            if task.context
            else wrapped_prompt
        )

        session_key = f"flow-{run_id}-{task.role}"
        stall_timeout = float(os.environ.get("NANOBOT_STALL_TIMEOUT_S", "900"))

        last_result: Optional[Any] = None
        halt_reason: str = "done"
        halt_detail: Optional[str] = None

        max_attempts = 3
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                # Wrap bot.run with stall detector
                async def monitored_run() -> Any:
                    run_task = asyncio.create_task(
                        bot.run(
                            task_message,
                            session_key=session_key,
                            hooks=[flow_hook],
                        )
                    )
                    poll_interval = min(stall_timeout / 2.0, 5.0)
                    while not run_task.done():
                        await asyncio.wait([run_task], timeout=poll_interval)
                        if run_task.done():
                            break
                        if hooks.circuit_breaker is not None:
                            try:
                                hooks.circuit_breaker.check_time()
                            except CircuitBreakerException:
                                run_task.cancel()
                                raise
                        stall_sec = (datetime.now(timezone.utc) - flow_hook.last_activity_at).total_seconds()
                        if stall_sec > stall_timeout:
                            run_task.cancel()
                            try:
                                from flow_agent.telemetry.event_sink import emit
                                emit("halt", reason="stall", detail=f"no activity for {stall_sec:.1f}s")
                            except Exception:
                                pass
                            raise TimeoutError(f"Nanobot run stalled: no activity for {stall_sec:.1f}s")
                    return await run_task

                last_result = await monitored_run()
                res_content = (getattr(last_result, "content", "") or "").strip()
                res_err = flow_hook.last_error or getattr(last_result, "error", None)
                if not res_err and (
                    getattr(last_result, "stop_reason", None) == "error"
                    or res_content.startswith("Error calling LLM")
                ):
                    res_err = res_content or "Error calling LLM: provider failure"
                if res_err:
                    flow_hook.last_error = None
                    raise RuntimeError(str(res_err))
                break

            except CircuitBreakerException:
                raise
            except Exception as exc:
                reason, detail = classify_provider_error(exc)
                if reason == "provider_quota" and attempt < max_attempts:
                    # Retry once or twice if delay is small
                    delay = 10.0
                    if detail and "retry_delay=" in detail:
                        try:
                            delay_str = detail.split("retry_delay=")[1].split()[0].replace("s", "")
                            delay = min(float(delay_str), 120.0)
                        except Exception:
                            delay = 10.0
                    try:
                        from flow_agent.telemetry.event_sink import emit
                        emit(
                            "reprompt",
                            reason="provider_retry",
                            attempt=attempt,
                            delay_s=delay,
                            error_class=exc.__class__.__name__,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(min(delay, 120.0))  # Bounded
                    continue

                halt_reason = reason if reason else "crash"
                halt_detail = detail if detail else str(exc).split("\n")[0].replace("{", "").replace("}", "")
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit("halt", reason=halt_reason, detail=halt_detail)
                except Exception:
                    pass
                break

        # Post-run / finally accounting
        if (
            (halt_reason != "done" or last_result is None)
            and halt_reason in ("provider_error", "provider_quota")
            and not flow_hook.recorded_calls
        ):
            flow_hook.recorded_calls.append(
                ModelCallRecord(
                    iteration=max(1, flow_hook.iterations),
                    provider=provider,
                    model=task.model_name,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    stop_reason=halt_reason,
                )
            )
        ledger = flow_hook.build_ledger()

        if halt_reason != "done" or last_result is None:
            # Crash-path flush
            if hooks.registry is not None:
                hooks.registry.usage = ledger.to_dict()
                hooks.registry.halt_reason = halt_reason
                await hooks.registry.terminate("Halted", reason=halt_detail or halt_reason)
            if hooks.telemetry_ui is not None:
                await hooks.telemetry_ui.terminate(error=True, reason=halt_detail or halt_reason)

            return RoleResult(
                final_text="",
                envelope_text=None,
                usage=ledger,
                halt=halt_reason,  # type: ignore[arg-type]
                halt_detail=halt_detail,
            )

        content = getattr(last_result, "content", "") or ""
        messages = getattr(last_result, "messages", []) or []

        envelope = _extract_envelope_from_text_or_messages(
            content=content,
            messages=messages,
            done_envelope=task.done_envelope,
            role=task.role,
        )

        if not envelope:
            # The agent might have paused to chat. Prompt it to either continue working or emit the envelope.
            reprompt = (
                f"You returned text but did not call any tools, and you did not emit the required "
                f"```{task.done_envelope} JSON envelope block.\n\n"
                f"If you are still working, you MUST call a tool "
                f"(like `mcp_flow_multi_edit` or `mcp_flow_execute_command`).\n"
                f"If you are completely finished with your task (tests pass, branch pushed, PR opened), "
                f"you MUST emit the envelope now "
                f"(e.g. ```{task.done_envelope}\\n{{\"tenant\": \"<owner/repo>\", "
                f"\"pr\": <number>, \"role\": \"{task.role}\", \"head_sha\": \"<git_sha>\"}}\\n```)."
            )
            reprompt_attempts = 0
            while not envelope and reprompt_attempts < 5:
                reprompt_attempts += 1
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit("reprompt", reason="missing_envelope", attempt=reprompt_attempts)
                except Exception:
                    pass
                last_result = await bot.run(
                    reprompt
                    if reprompt_attempts == 1
                    else "Still no envelope and no tool calls. Please call a tool to continue "
                    "working, or emit the envelope if done.",
                    session_key=session_key,
                    hooks=[flow_hook],
                )
                content = getattr(last_result, "content", "") or ""
                messages = getattr(last_result, "messages", []) or []

                envelope = _extract_envelope_from_text_or_messages(
                    content=content,
                    messages=messages,
                    done_envelope=task.done_envelope,
                    role=task.role,
                )

            if not envelope:
                # Fallback safety net for developer role if repository workspace is clean and pushed
                if task.done_envelope == "flow-ack" and task.role == "developer":
                    head_sha = ""
                    try:
                        r = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=task.workspace_dir,
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        head_sha = r.stdout.strip()
                    except Exception:
                        pass
                    if not head_sha or len(head_sha) != 40:
                        head_sha = os.environ.get("FLOW_HEAD_SHA", "")

                    tenant = os.environ.get("FLOW_TENANT") or os.environ.get("TARGET_REPO", "")
                    pr_str = os.environ.get("FLOW_PR") or os.environ.get("ISSUE_ID", "0")
                    pr = int(pr_str) if pr_str.isdigit() else 0

                    # Only vouch for real work: a HEAD equal to the base branch means
                    # nothing was implemented, and acking it sends the evaluator to review nothing.
                    has_work = bool(task.workspace_dir) and _has_unpublished_work(task.workspace_dir)
                    if has_work and tenant and pr and head_sha and len(head_sha) == 40:
                        ack_obj = Ack(
                            tenant=tenant,
                            pr=pr,
                            head_sha=head_sha,
                            run_id=os.environ.get("FLOW_RUN_ID"),
                            role="developer",
                            addressed_findings=[],
                            dispute=[],
                        )
                        envelope = render(ack_obj).strip()
                elif task.done_envelope == "flow-verdict" and task.role == "evaluator":
                    # Fallback for evaluator role if model fails checks or refuses to hallucinate check run evidence
                    head_sha = os.environ.get("FLOW_HEAD_SHA", "")
                    tenant = os.environ.get("FLOW_TENANT") or os.environ.get("TARGET_REPO", "")
                    pr_str = os.environ.get("FLOW_PR") or os.environ.get("ISSUE_ID", "0")
                    pr = int(pr_str) if pr_str.isdigit() else 0
                    if tenant and pr and head_sha and len(head_sha) == 40:
                        from atobar_contracts.envelopes import Finding, Verdict

                        verdict_obj = Verdict(
                            tenant=tenant,
                            pr=pr,
                            head_sha=head_sha,
                            run_id=os.environ.get("FLOW_RUN_ID"),
                            role="evaluator",
                            round=1,
                            verdict="changes_requested",
                            findings=[
                                Finding(
                                    id="1",
                                    criterion="Catch-all check",
                                    evidence="check_run:0",  # stub
                                    severity="major",
                                    message="The agent failed to emit a flow-verdict due to formatting."
                                )
                            ]
                        )
                        envelope = render(verdict_obj).strip()

            if not envelope:
                raise ValueError(f"Nanobot failed to emit required envelope {task.done_envelope}")

        ledger = flow_hook.build_ledger()
        return RoleResult(
            final_text=content,
            envelope_text=envelope,
            usage=ledger,
            halt="done",
            halt_detail=None,
        )
