from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nanobot.agent.hook import AgentHookContext
from nanobot.providers.base import ToolCallRequest

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.nanobot_runtime import FlowHook
from atobar_contracts.envelopes import Verdict, render
from flow_agent.roles.evaluator.runner import run_evaluator


@pytest.mark.asyncio
async def test_ac2_evaluator_on_nanobot_runtime(tmp_path):
    head_sha = "0123456789abcdef0123456789abcdef01234567"
    verdict = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=head_sha,
        round=1,
        verdict="approved",
        findings=[],
    )
    envelope_md = render(verdict)

    from nanobot.sdk.types import LLMUsage, RunResult
    fake_run_result = RunResult(
        content=f"Evaluator findings summary.\n{envelope_md}",
        tools_used=["flow__git_pr_checks"],
        messages=[{"role": "assistant", "content": f"Evaluator findings summary.\n{envelope_md}"}],
        usage=LLMUsage.reported(input_tokens=150, output_tokens=75),
    )

    fake_bot = MagicMock()
    fake_bot.run = AsyncMock(return_value=fake_run_result)

    mock_gh = MagicMock()

    import os
    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-eval-42",
        "FLOW_RUN_CREDENTIAL": "cred-eval-42",
        "FLOW_AGENT_RUNTIME": "nanobot",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
            with patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key") as mock_fetch:
                from flow_agent.runtime.key_delivery import TenantLLMKey
                mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

                await run_evaluator(
                    tenant="arsenyspb/ollama-monitor",
                    pr=42,
                    head_sha=head_sha,
                    task_prompt="Evaluate PR #42",
                    gh_client=mock_gh,
                )

    mock_gh.post_pr_comment.assert_called_once()
    body = mock_gh.post_pr_comment.call_args[1]["body"]
    assert "flow-verdict" in body
    assert head_sha in body
    assert '"verdict": "approved"' in body
    mock_gh.create_check_run.assert_called_once()


@pytest.mark.asyncio
async def test_ac3_evaluator_hook_rejects_disallowed_tools(tmp_path):
    hook = FlowHook(
        task=RoleTask(
            role="evaluator",
            system_prompt="",
            context="",
            task_prompt="",
            workspace_dir=str(tmp_path),
            allowed_tools=frozenset(["read_file", "search_codebase", "git_pr_checks"]),
            model_name="gemini-2.5-flash",
            done_envelope="flow-verdict",
        ),
        hooks=RuntimeHooks(),
    )

    disallowed = ["create_and_verify_pr", "write_file", "multi_edit"]
    for tool_name in disallowed:
        ctx = AgentHookContext(
            iteration=1,
            messages=[],
            tool_calls=[ToolCallRequest(id="1", name=tool_name, arguments={})],
        )
        with pytest.raises(PermissionError, match="not allowed"):
            await hook.before_execute_tools(ctx)


def test_ac4_decorator_tolerant_envelope_extraction():
    from flow_agent.agents.nanobot_runtime import _extract_envelope_from_text_or_messages
    head_sha = "0123456789abcdef0123456789abcdef01234567"
    verdict = Verdict(
        tenant="arsenyspb/ollama-monitor",
        pr=42,
        head_sha=head_sha,
        round=1,
        verdict="approved",
        findings=[],
    )
    raw_envelope = render(verdict)

    # Decorate output with conversational wrapper and markdown blockquotes
    decorated_content = (
        "Hello! I have reviewed your submission thoroughly.\n\n"
        "Here is the evaluation result:\n\n"
        f"{raw_envelope}\n\n"
        "Please let me know if you need further clarifications!\n"
    )

    extracted = _extract_envelope_from_text_or_messages(
        content=decorated_content,
        messages=[],
        done_envelope="flow-verdict",
        role="evaluator",
    )
    assert extracted is not None
    assert raw_envelope.strip() in extracted
