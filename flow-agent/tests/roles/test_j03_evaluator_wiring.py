import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow_agent.agents.base import RoleResult, UsageLedger
from flow_agent.roles.evaluator.runner import run_evaluator
from flow_agent.runtime.key_delivery import TenantLLMKey


@pytest.mark.asyncio
async def test_j03_ac1_ac3_ac4_run_evaluator_verdict_posting():
    envelope_text = (
        '```flow-verdict\n'
        '{"v":1,"role":"evaluator","tenant":"owner/repo","pr":42,'
        f'"head_sha":"{"a" * 40}","round":1,"verdict":"approved","findings":[]}}\n'
        '```'
    )
    fake_runtime = MagicMock()
    fake_runtime.run = AsyncMock(
        return_value=RoleResult(
            final_text=envelope_text,
            envelope_text=envelope_text,
            usage=UsageLedger(),
            halt="done",
        )
    )

    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "run", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), \
         patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key", return_value=TenantLLMKey(provider="gemini", api_key="k", model="gemini-4-argon")), \
         patch("flow_agent.roles.evaluator.runner.get_runtime", return_value=fake_runtime), \
         patch("flow_agent.roles.evaluator.runner.get_bot_logins", return_value={"evaluator": "bot"}):

        fake_gh = MagicMock()
        await run_evaluator(
            tenant="owner/repo",
            pr=42,
            head_sha="a" * 40,
            task_prompt="task",
            gh_client=fake_gh
        )

        # AC-1: exact head_sha, post_pr_comment called
        fake_gh.post_pr_comment.assert_called_once()
        body = fake_gh.post_pr_comment.call_args[1]["body"]
        assert "flow-verdict" in body
        assert "a" * 40 in body

        # AC-3: check run created with success conclusion
        fake_gh.create_check_run.assert_called_once_with(
            repo="owner/repo",
            name="flow/eval",
            head_sha="a" * 40,
            conclusion="success",
            output={"title": "flow/eval: approved", "summary": "Evaluator round 1: approved. Findings count: 0"}
        )

@pytest.mark.asyncio
async def test_j03_ac2_stale_sha_error_caught():
    envelope_text = (
        '```flow-verdict\n'
        '{"v":1,"role":"evaluator","tenant":"owner/repo","pr":42,'
        f'"head_sha":"{"b" * 40}","round":1,"verdict":"approved","findings":[]}}\n'
        '```'
    )
    fake_runtime = MagicMock()
    fake_runtime.run = AsyncMock(
        return_value=RoleResult(
            final_text=envelope_text,
            envelope_text=envelope_text,
            usage=UsageLedger(),
            halt="done",
        )
    )

    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "run", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), \
         patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key", return_value=TenantLLMKey(provider="gemini", api_key="k", model="gemini-4-argon")), \
         patch("flow_agent.roles.evaluator.runner.get_runtime", return_value=fake_runtime), \
         patch("flow_agent.roles.evaluator.runner.get_bot_logins", return_value={"evaluator": "bot"}):

        fake_gh = MagicMock()
        await run_evaluator(
            tenant="owner/repo",
            pr=42,
            head_sha="c" * 40,
            task_prompt="task",
            gh_client=fake_gh
        )

        fake_gh.create_check_run.assert_not_called()
        fake_gh.post_pr_comment.assert_called_once()
        body = fake_gh.post_pr_comment.call_args[1]["body"]
        assert "Stale SHA Detected" in body
        assert "b" * 40 in body

@pytest.mark.asyncio
async def test_j03_ac4_no_verdict_posted_on_invalid_reply():
    fake_runtime = MagicMock()
    fake_runtime.run = AsyncMock(
        return_value=RoleResult(
            final_text="No envelope here",
            envelope_text=None,
            usage=UsageLedger(),
            halt="done",
        )
    )

    env = {"CONTROL_PANEL_URL": "http://cp", "FLOW_RUN_ID": "run", "FLOW_RUN_CREDENTIAL": "cred"}
    with patch.dict(os.environ, env), \
         patch("flow_agent.roles.evaluator.runner.fetch_run_llm_key", return_value=TenantLLMKey(provider="gemini", api_key="k", model="gemini-4-argon")), \
         patch("flow_agent.roles.evaluator.runner.get_runtime", return_value=fake_runtime):
        fake_gh = MagicMock()
        await run_evaluator(
            tenant="owner/repo",
            pr=42,
            head_sha="exact_sha",
            task_prompt="task",
            gh_client=fake_gh
        )

        fake_gh.post_pr_comment.assert_not_called()
        fake_gh.create_check_run.assert_not_called()
