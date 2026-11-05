from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("nanobot")

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.nanobot_runtime import NanobotRuntime
from flow_agent.core.watchdog import CircuitBreaker, CircuitBreakerException
from atobar_contracts.envelopes import Ack, render


@pytest.mark.asyncio
async def test_nanobot_runtime_ack_success(tmp_path):
    ack = Ack(tenant="owner/repo", pr=1, head_sha="0123456789abcdef0123456789abcdef01234567")
    envelope_md = render(ack)

    from nanobot.sdk.types import LLMUsage, RunResult

    fake_run_result = RunResult(
        content=f"Done!\n{envelope_md}",
        tools_used=["flow__read_file"],
        messages=[{"role": "assistant", "content": f"Done!\n{envelope_md}"}],
        usage=LLMUsage.reported(input_tokens=100, output_tokens=50, cache_read_tokens=20),
    )

    fake_bot = MagicMock()
    fake_bot.run = AsyncMock(return_value=fake_run_result)

    task = RoleTask(
        role="developer",
        system_prompt="You are a developer.",
        context="Some context",
        task_prompt="Fix issue",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(task, hooks=RuntimeHooks())

    assert result.halt == "done"
    assert result.envelope_text is not None
    assert "flow-ack" in result.envelope_text


@pytest.mark.asyncio
async def test_nanobot_runtime_missing_envelope_reprompt(tmp_path):
    ack = Ack(tenant="owner/repo", pr=1, head_sha="0123456789abcdef0123456789abcdef01234567")
    envelope_md = render(ack)

    from nanobot.sdk.types import LLMUsage, RunResult

    res1 = RunResult(
        content="I fixed the bug without envelope.",
        tools_used=[],
        messages=[{"role": "assistant", "content": "I fixed the bug."}],
        usage=LLMUsage.reported(input_tokens=50, output_tokens=25),
    )
    res2 = RunResult(
        content=f"Here it is:\n{envelope_md}",
        tools_used=[],
        messages=[{"role": "assistant", "content": f"Here it is:\n{envelope_md}"}],
        usage=LLMUsage.reported(input_tokens=30, output_tokens=20),
    )

    fake_bot = MagicMock()
    fake_bot.run = AsyncMock(side_effect=[res1, res2])

    task = RoleTask(
        role="developer",
        system_prompt="You are a developer.",
        context="Some context",
        task_prompt="Fix issue",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(task, hooks=RuntimeHooks())

    assert fake_bot.run.call_count == 2
    assert result.envelope_text is not None
    assert "flow-ack" in result.envelope_text


@pytest.mark.asyncio
async def test_before_execute_tools_disallowed_tool(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.providers.base import ToolCallRequest

    from flow_agent.agents.nanobot_runtime import FlowHook

    hook = FlowHook(
        task=RoleTask(
            role="evaluator",
            system_prompt="You are an evaluator.",
            context="",
            task_prompt="",
            workspace_dir=str(tmp_path),
            allowed_tools=frozenset(["read_file"]),
            model_name="gemini-2.5-flash",
            done_envelope="flow-verdict",
        ),
        hooks=RuntimeHooks(),
    )

    ctx = AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[ToolCallRequest(id="1", name="write_file", arguments={})],
    )

    with pytest.raises(PermissionError, match="not allowed"):
        await hook.before_execute_tools(ctx)


@pytest.mark.asyncio
async def test_before_execute_tools_normalizes_mcp_flow_prefix(tmp_path):
    """mcp_flow_<tool> names (nanobot's mcp_<server>_<tool> namespacing) resolve to allowlisted tools."""
    from nanobot.agent.hook import AgentHookContext
    from nanobot.providers.base import ToolCallRequest

    from flow_agent.agents.nanobot_runtime import FlowHook

    hook = FlowHook(
        task=RoleTask(
            role="developer",
            system_prompt="You are a developer.",
            context="",
            task_prompt="",
            workspace_dir=str(tmp_path),
            allowed_tools=frozenset(["search_codebase", "read_file"]),
            model_name="gemini-2.5-flash",
            done_envelope="flow-ack",
        ),
        hooks=RuntimeHooks(),
    )

    ctx = AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[
            ToolCallRequest(id="1", name="mcp_flow_search_codebase", arguments={"query": "x"}),
            ToolCallRequest(id="2", name="mcp_flow_read_file", arguments={"path": "README.md"}),
        ],
    )
    await hook.before_execute_tools(ctx)

    ctx_blocked = AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[ToolCallRequest(id="3", name="mcp_flow_write_file", arguments={})],
    )
    with pytest.raises(PermissionError, match="not allowed"):
        await hook.before_execute_tools(ctx_blocked)


@pytest.mark.asyncio
async def test_before_iteration_circuit_breaker(tmp_path):
    from nanobot.agent.hook import AgentHookContext

    from flow_agent.agents.nanobot_runtime import FlowHook

    cb = CircuitBreaker(max_wall_clock_minutes=0)  # already tripped

    hook = FlowHook(
        task=RoleTask(
            role="developer",
            system_prompt="",
            context="",
            task_prompt="",
            workspace_dir=str(tmp_path),
            allowed_tools=frozenset(["read_file"]),
            model_name="gemini-2.5-flash",
            done_envelope="flow-ack",
        ),
        hooks=RuntimeHooks(circuit_breaker=cb),
    )

    ctx = AgentHookContext(iteration=1, messages=[])
    with pytest.raises(CircuitBreakerException):
        await hook.before_iteration(ctx)


@pytest.mark.asyncio
async def test_liveness_heartbeat_and_stall(tmp_path):
    from nanobot.agent.hook import AgentHookContext

    from flow_agent.agents.nanobot_runtime import FlowHook

    mock_reg = MagicMock()
    mock_reg.heartbeat = AsyncMock()

    hook = FlowHook(
        task=RoleTask(
            role="developer",
            system_prompt="",
            context="",
            task_prompt="",
            workspace_dir=str(tmp_path),
            allowed_tools=frozenset(["read_file"]),
            model_name="gemini-2.5-flash",
            done_envelope="flow-ack",
        ),
        hooks=RuntimeHooks(registry=mock_reg),
    )

    ctx = AgentHookContext(iteration=1, messages=[])
    await hook.before_iteration(ctx)
    mock_reg.heartbeat_nowait.assert_called_once()
    mock_reg.heartbeat.assert_not_called()  # never awaited on the LLM critical path
    assert hook.iterations == 1
    assert mock_reg.iterations == 1


@pytest.mark.asyncio
async def test_provider_error_retry_and_classify(tmp_path):
    class Fake429(Exception):
        status_code = 429
        response_json = {
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": "generate_content_input_token_count",
                                "quotaDimensions": {"model": "gemini-2.5-flash"},
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.01s"},
                ],
            }
        }

    fake_bot = MagicMock()
    fake_bot.run = AsyncMock(side_effect=Fake429("Rate limit"))

    task = RoleTask(
        role="developer",
        system_prompt="",
        context="",
        task_prompt="",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(task, hooks=RuntimeHooks())

    assert result.halt == "provider_quota"
    assert result.halt_detail is not None
    assert "\n" not in result.halt_detail
    assert "{" not in result.halt_detail
    assert "gemini-2.5-flash" in result.halt_detail or "retry_delay" in result.halt_detail


@pytest.mark.asyncio
async def test_crash_flush_emits_usage(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.sdk.types import LLMUsage

    mock_reg = MagicMock()
    mock_reg.heartbeat = AsyncMock()
    mock_reg.terminate = AsyncMock()

    mock_telemetry = MagicMock()
    mock_telemetry.terminate = AsyncMock()

    fake_bot = MagicMock()

    async def fake_run(*args, **kwargs):
        hooks = kwargs.get("hooks", [])
        for h in hooks:
            ctx = AgentHookContext(
                iteration=1,
                messages=[],
                usage=LLMUsage.reported(input_tokens=100, output_tokens=50, cache_read_tokens=25),
            )
            await h.after_iteration(ctx)
        raise RuntimeError("Mid-flight crash")

    fake_bot.run = AsyncMock(side_effect=fake_run)

    task = RoleTask(
        role="developer",
        system_prompt="",
        context="",
        task_prompt="",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(task, hooks=RuntimeHooks(registry=mock_reg, telemetry_ui=mock_telemetry))

    assert result.halt in ("crash", "provider_error")
    assert result.usage.total.input_tokens == 100
    assert result.usage.total.output_tokens == 50
    assert result.usage.total.cached_input_tokens == 25
    assert len(result.usage.calls) == 1


@pytest.mark.asyncio
async def test_per_tool_token_breakdown_and_registry_sync(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.providers.base import ToolCallRequest
    from nanobot.sdk.types import LLMUsage, RunResult

    ack = Ack(tenant="owner/repo", pr=1, head_sha="0123456789abcdef0123456789abcdef01234567")
    envelope_md = render(ack)

    mock_reg = MagicMock()
    mock_reg.heartbeat = AsyncMock()
    mock_reg.terminate = AsyncMock()

    mock_telemetry = MagicMock()
    mock_telemetry.update_state = MagicMock()

    fake_bot = MagicMock()

    async def fake_run(*args, **kwargs):
        hooks = kwargs.get("hooks", [])
        for h in hooks:
            # Iteration 1: calls exec (nanobot builtin -> execute_command) and mcp_flow_read_file
            tc1 = ToolCallRequest(id="c1", name="exec", arguments={"command": "pytest -q"})
            tc2 = ToolCallRequest(id="c2", name="mcp_flow_read_file", arguments={"file_path": "src/main.py"})
            ctx1 = AgentHookContext(
                iteration=1,
                messages=[],
                tool_calls=[tc1, tc2],
                tool_results=["1 passed in 0.1s", "def main(): pass"],
                usage=LLMUsage.reported(input_tokens=1000, output_tokens=200, cache_read_tokens=400),
            )
            await h.before_iteration(ctx1)
            await h.before_execute_tools(ctx1)
            await h.before_execute_tool(ctx1, tc1, None, tc1.arguments)
            await h.after_execute_tool(ctx1, tc1, None, tc1.arguments, "1 passed in 0.1s")
            await h.before_execute_tool(ctx1, tc2, None, tc2.arguments)
            await h.after_execute_tool(ctx1, tc2, None, tc2.arguments, "def main(): pass")
            await h.after_iteration(ctx1)

            # Iteration 2: final response with no tool calls
            ctx2 = AgentHookContext(
                iteration=2,
                messages=[],
                tool_calls=[],
                tool_results=[],
                usage=LLMUsage.reported(input_tokens=600, output_tokens=100, cache_read_tokens=300),
            )
            await h.before_iteration(ctx2)
            await h.after_iteration(ctx2)

        return RunResult(
            content=f"Done!\n{envelope_md}",
            tools_used=["exec", "mcp_flow_read_file"],
            messages=[{"role": "assistant", "content": f"Done!\n{envelope_md}"}],
            usage=LLMUsage.reported(input_tokens=1600, output_tokens=300, cache_read_tokens=700),
        )

    fake_bot.run = AsyncMock(side_effect=fake_run)

    task = RoleTask(
        role="developer",
        system_prompt="",
        context="",
        task_prompt="",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["execute_command", "read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(
            task,
            hooks=RuntimeHooks(registry=mock_reg, telemetry_ui=mock_telemetry),
        )

    assert result.halt == "done"
    assert result.usage.total.input_tokens == 1600
    assert result.usage.total.output_tokens == 300
    assert result.usage.total.total_tokens == 1900
    assert "execute_command" in result.usage.by_tool
    assert "read_file" in result.usage.by_tool
    assert result.usage.by_tool["execute_command"].calls == 1
    assert result.usage.by_tool["execute_command"].input_tokens == 500
    assert result.usage.by_tool["execute_command"].output_tokens == 100
    assert result.usage.by_tool["execute_command"].total_tokens == 600
    assert result.usage.by_tool["execute_command"].result_tokens > 0
    assert result.usage.by_tool["read_file"].calls == 1
    assert result.usage.by_tool["read_file"].total_tokens == 600

    # Registry received usage with by_tool breakdown, agent_runtime, and per-call records
    assert mock_reg.usage is not None
    assert mock_reg.usage["agent_runtime"] == "nanobot"
    assert mock_reg.usage["total"]["total_tokens"] == 1900
    assert len(mock_reg.usage["calls"]) == 2
    assert mock_reg.usage["calls"][0]["input_tokens"] == 1000
    assert "execute_command" in mock_reg.usage["by_tool"]


@pytest.mark.asyncio
async def test_nanobot_runtime_handles_in_band_llm_error_without_reprompting(tmp_path):
    from nanobot.sdk.types import LLMUsage, RunResult

    mock_reg = MagicMock()
    mock_reg.heartbeat = AsyncMock()
    mock_reg.terminate = AsyncMock()

    mock_telemetry = MagicMock()
    mock_telemetry.terminate = AsyncMock()

    fake_bot = MagicMock()
    fake_bot.run = AsyncMock(
        return_value=RunResult(
            content="Error calling LLM: litellm.BadRequestError: 400 API_KEY_INVALID: API key not valid.",
            tools_used=[],
            messages=[],
            usage=LLMUsage.reported(input_tokens=0, output_tokens=0),
        )
    )

    task = RoleTask(
        role="developer",
        system_prompt="",
        context="",
        task_prompt="Fix issue",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-4-argon",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        result = await runtime.run(
            task,
            hooks=RuntimeHooks(registry=mock_reg, telemetry_ui=mock_telemetry),
        )

    assert fake_bot.run.call_count == 1
    assert result.halt == "provider_error"
    assert result.halt_detail == "provider_error: status=400"
    assert mock_reg.usage is not None
    assert mock_reg.usage["agent_runtime"] == "nanobot"
    assert len(mock_reg.usage["calls"]) == 1
    assert mock_reg.usage["calls"][0]["stop_reason"] == "provider_error"
    assert mock_reg.halt_reason == "provider_error"
    mock_reg.terminate.assert_called_once_with("Halted", reason="provider_error: status=400")
    mock_telemetry.terminate.assert_called_once_with(error=True, reason="provider_error: status=400")


