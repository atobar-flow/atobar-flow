import os
from unittest.mock import MagicMock, patch

import pytest

from flow_agent.agents.base import AgentRuntime, RoleResult, RoleTask, RuntimeHooks, UsageLedger
from flow_agent.agents.registry import register_runtime
from atobar_contracts.envelopes import Verdict, render
from flow_agent.roles.evaluator.runner import run_evaluator
from flow_agent.runtime.tools import allowlist_for


class FakeEvalRuntime(AgentRuntime):
    def __init__(self, envelope_text: str):
        self.recorded_task = None
        self.envelope_text = envelope_text

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        self.recorded_task = task
        return RoleResult(
            final_text=f"Review:\n{self.envelope_text}",
            envelope_text=self.envelope_text,
            usage=UsageLedger(),
            halt="done",
        )


@pytest.mark.asyncio
async def test_evaluator_runtime_dispatch_and_verdict_post():
    head_sha = "0123456789abcdef0123456789abcdef01234567"
    verdict = Verdict(
        tenant="owner/repo",
        pr=1,
        head_sha=head_sha,
        round=1,
        verdict="approved",
        findings=[],
    )
    envelope_md = render(verdict)

    fake_runtime = FakeEvalRuntime(envelope_md)
    register_runtime("fake_eval", lambda: fake_runtime)

    mock_gh = MagicMock()

    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-123",
        "FLOW_AGENT_RUNTIME": "fake_eval",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey
            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            await run_evaluator(
                tenant="owner/repo",
                pr=1,
                head_sha=head_sha,
                task_prompt="Review PR",
                gh_client=mock_gh,
            )

    assert fake_runtime.recorded_task is not None
    assert fake_runtime.recorded_task.allowed_tools == allowlist_for("evaluator")
    assert fake_runtime.recorded_task.done_envelope == "flow-verdict"
    mock_gh.post_pr_comment.assert_called_once()
    mock_gh.create_check_run.assert_called_once()


@pytest.mark.asyncio
async def test_evaluator_stale_sha_warning():
    current_head = "0123456789abcdef0123456789abcdef01234567"
    stale_head = "fedcba9876543210fedcba9876543210fedcba98"
    verdict = Verdict(
        tenant="owner/repo",
        pr=1,
        head_sha=stale_head,
        round=1,
        verdict="approved",
        findings=[],
    )
    envelope_md = render(verdict)

    fake_runtime = FakeEvalRuntime(envelope_md)
    register_runtime("fake_eval_stale", lambda: fake_runtime)

    mock_gh = MagicMock()

    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-123",
        "FLOW_AGENT_RUNTIME": "fake_eval_stale",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey
            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            await run_evaluator(
                tenant="owner/repo",
                pr=1,
                head_sha=current_head,
                task_prompt="Review PR",
                gh_client=mock_gh,
            )

    # Should post warning comment about stale SHA
    mock_gh.post_pr_comment.assert_called_once()
    assert "Stale SHA Detected" in mock_gh.post_pr_comment.call_args[1]["body"]
    mock_gh.create_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_evaluator_missing_credentials_fails_closed():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="Failed to fetch tenant LLM key"):
            await run_evaluator(
                tenant="owner/repo",
                pr=1,
                head_sha="0123456789abcdef0123456789abcdef01234567",
                task_prompt="Review PR",
            )
