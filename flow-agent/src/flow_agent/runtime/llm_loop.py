"""Generic tool-calling loop for swarm roles parameterized by RoleSpec."""

import asyncio
import base64
import time
from typing import Any, Awaitable, Callable, Optional

from flow_agent.agents.base import ModelCallRecord, UsageDetail, UsageLedger
from flow_agent.core.watchdog import CircuitBreaker
from flow_agent.llm.protocol import Message, Reply, ToolSpec
from flow_agent.roles.base import RoleSpec
from flow_agent.runtime.tools import execute_tool as default_execute_tool
from flow_agent.telemetry.event_sink import emit
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry
from flow_agent.tools.ci_tools import get_truncation_limit


def _extract_current_todo(todos: list[dict[str, Any]]) -> Optional[str]:
    for task in todos:
        if task.get("status") == "in_progress":
            return task.get("content") or task.get("title")
    return None


async def llm_loop(
    spec: RoleSpec,
    task_prompt: str,
    context: str = "",
    tool_executor: Optional[Callable[[str, dict[str, Any]], Awaitable[Any]]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    registry: Optional[SessionRegistry] = None,
    telemetry_ui: Optional[GitHubTelemetryUI] = None,
) -> Reply:
    """Executes the autonomous tool-calling loop until the role's done condition is met."""
    executor = tool_executor or default_execute_tool

    initial_prompt = (
        f"SYSTEM KNOWLEDGE:\n{context}\n\nSTART TASK:\n{task_prompt}"
        if context
        else task_prompt
    )
    messages: list[Message] = [Message(role="user", content=initial_prompt)]

    # Prepare tool specifications for the allowed tools
    tool_specs: list[ToolSpec] = [
        ToolSpec(
            name=name,
            description=f"Tool {name}",
            parameters={"type": "object", "properties": {}},
        )
        for name in sorted(spec.tools)
    ]

    last_reply = Reply(text="")
    iteration = 0
    total_tokens = 0
    ledger = UsageLedger()

    while True:
        iteration += 1
        if circuit_breaker is not None:
            circuit_breaker.check_time()

        reply = await asyncio.to_thread(
            spec.llm.chat,
            messages=messages,
            tools=tool_specs,
            system_instruction=spec.system_prompt,
        )
        last_reply = reply

        provider = getattr(spec.llm, "provider", "gemini")
        model = getattr(spec.llm, "model", getattr(spec, "model_name", "unknown"))
        in_tok = int(getattr(reply.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(reply.usage, "output_tokens", 0) or 0)
        cached_tok = getattr(reply.usage, "cached_input_tokens", None)
        cost_usd = float(getattr(reply.usage, "cost_usd", 0.0) or 0.0)

        ledger.calls.append(
            ModelCallRecord(
                iteration=iteration,
                provider=provider,
                model=model,
                input_tokens=in_tok,
                cached_input_tokens=cached_tok,
                output_tokens=out_tok,
                cost_usd=cost_usd,
            )
        )
        ledger.total.input_tokens += in_tok
        ledger.total.output_tokens += out_tok
        ledger.total.cost_usd += cost_usd
        ledger.total.total_tokens = ledger.total.input_tokens + ledger.total.output_tokens
        if cached_tok is not None:
            ledger.total.cached_input_tokens = (ledger.total.cached_input_tokens or 0) + int(cached_tok)

        emit(
            "model_call",
            iteration=iteration,
            provider=provider,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_input_tokens=cached_tok,
            cost_usd=cost_usd,
        )

        total_tokens += in_tok + out_tok
        if reply.tool_calls:
            ledger.record_tool_tokens(
                [c.name for c in reply.tool_calls if c.name in spec.tools],
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=in_tok + out_tok,
            )
        if registry is not None:
            registry.usage = ledger.to_dict()
        if telemetry_ui is not None:
            telemetry_ui.update_state(
                {
                    "total_tokens": total_tokens,
                    "tool_usage": {k: v.to_dict() for k, v in ledger.by_tool.items()},
                }
            )

        if spec.done(reply):
            break

        # Append assistant turn before tool-result follow-up so model sees its previous turns
        messages.append(Message(role="assistant", content=reply.text or ""))

        if reply.tool_calls:
            for call in reply.tool_calls:
                call_name = call.name
                call_args = call.args

                if call_name not in spec.tools:
                    # Tool refused: outside allowed tools for this role
                    error_msg = f"Error: Tool '{call_name}' is not allowed for role '{spec.name}'"
                    emit(
                        "tool_call",
                        name=call_name,
                        args_head=str(call_args)[:4000],
                        result_head="",
                        duration_ms=0.0,
                        ok=False,
                        error=error_msg,
                    )
                    messages.append(
                        Message(role="tool", name=call_name, content=error_msg)
                    )
                    continue

                if registry is not None:
                    registry.last_tool = call_name

                # Execute permitted tool
                t0 = time.perf_counter()
                ok = True
                err: Optional[str] = None
                try:
                    result = await executor(call_name, call_args)
                except Exception as exc:
                    ok = False
                    err = str(exc)
                    result = f"Error: {exc}"
                dur_ms = (time.perf_counter() - t0) * 1000.0
                result_str = str(result)

                ledger.record_tool_call(
                    call_name,
                    result_chars=len(result_str),
                    duration_ms=int(dur_ms),
                    is_error=not ok,
                )
                if registry is not None:
                    registry.usage = ledger.to_dict()

                emit(
                    "tool_call",
                    name=call_name,
                    args_head=str(call_args)[:4000],
                    result_head=result_str[:4000],
                    duration_ms=dur_ms,
                    ok=ok,
                    error=err,
                )

                if call_name == "todo_write" and isinstance(call_args, dict):
                    todos = call_args.get("todos", [])
                    if telemetry_ui is not None:
                        telemetry_ui.update_state({"tasks": todos})
                    if registry is not None:
                        registry.current_todo = _extract_current_todo(todos)
                        registry.heartbeat_nowait(force=True)

                if call_name == "create_and_verify_pr" and "FAILED" in result_str.upper():
                    if circuit_breaker is not None:
                        circuit_breaker.record_ci_failure()

                if telemetry_ui is not None:
                    telemetry_ui.update_state(
                        {
                            "last_tool": call_name,
                            "iterations": iteration,
                            "tool_usage": {k: v.to_dict() for k, v in ledger.by_tool.items()},
                        }
                    )

                # Append tool result to history (with screenshot/DOM handling)
                if isinstance(result, dict) and "screenshot_base64" in result:
                    image_bytes = base64.b64decode(result["screenshot_base64"])
                    dom_text = str(result.get("annotated_dom", ""))
                    limit = get_truncation_limit()
                    if len(dom_text) > limit:
                        dom_text = (
                            dom_text[:limit]
                            + "\n\n<truncation_notice>DOM output truncated.</truncation_notice>"
                        )

                    multimodal_content: list[dict[str, Any]] = [
                        {"data": image_bytes, "mime_type": "image/png"},
                        {"text": dom_text},
                    ]
                    messages.append(
                        Message(role="tool", name=call_name, content=multimodal_content)
                    )
                else:
                    messages.append(
                        Message(role="tool", name=call_name, content=result_str)
                    )
        else:
            messages.append(
                Message(
                    role="user",
                    content="Please continue executing tools to complete the task.",
                )
            )

    return last_reply
