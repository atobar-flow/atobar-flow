import os
from unittest.mock import MagicMock, patch

import pytest

from flow_agent.agents.base import AgentRuntime, RoleResult, RoleTask, RuntimeHooks, UsageLedger
from flow_agent.agents.registry import register_runtime
from atobar_contracts.github_rest import RealGitHubClient
from atobar_contracts.envelopes import Ack, render
from flow_agent.roles.developer.runner import run_developer


class FakePublishRuntime(AgentRuntime):
    def __init__(self, envelope_text: str):
        self.envelope_text = envelope_text

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        return RoleResult(
            final_text="Done",
            envelope_text=self.envelope_text,
            usage=UsageLedger(),
            halt="done",
        )


@pytest.fixture
def mock_env():
    return {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-123",
        "FLOW_AGENT_RUNTIME": "fake_publish",
    }


@pytest.fixture
def base_ack_envelope():
    ack = Ack(
        tenant="owner/repo",
        pr=1,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="run-123",
        addressed_findings=[],
        dispute=[],
    )
    return render(ack)


@pytest.mark.asyncio
async def test_developer_publish_success(mock_env, base_ack_envelope):
    fake_runtime = FakePublishRuntime(envelope_text=base_ack_envelope)
    register_runtime("fake_publish", lambda: fake_runtime)

    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = []

    with patch.dict(os.environ, mock_env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            with patch(
                "flow_agent.roles.developer.runner.create_and_verify_pr",
                return_value="PR Creation Output: https://github.com/owner/repo/pull/2",
            ) as mock_create_pr:
                with patch(
                    "flow_agent.roles.developer.runner.get_current_git_sha",
                    return_value="9999999999abcdef9999999999abcdef99999999",
                ):
                    await run_developer(
                        tenant="owner/repo",
                        pr=1,
                        task_prompt="Implement feature",
                        gh_client=gh_client,
                    )

                    mock_create_pr.assert_called_once_with(
                        title="flow: implementation for issue #1",
                        body="Automated implementation by atobar-flow developer (run run-123).\n\nCloses #1",
                        wait_ci=False,
                    )

                    # Verify gh_client.post_pr_comment was called
                    gh_client.post_pr_comment.assert_called_once()
                    call_args = gh_client.post_pr_comment.call_args[1]
                    assert call_args["repo"] == "owner/repo"
                    assert call_args["pr_number"] == 1

                    # Verify the body has the updated SHA
                    assert "9999999999abcdef9999999999abcdef99999999" in call_args["body"]
                    assert "0123456789abcdef0123456789abcdef01234567" not in call_args["body"]


@pytest.mark.asyncio
async def test_developer_publish_dedup(mock_env, base_ack_envelope):
    fake_runtime = FakePublishRuntime(envelope_text=base_ack_envelope)
    register_runtime("fake_publish", lambda: fake_runtime)

    gh_client = MagicMock(spec=RealGitHubClient)
    pushed = "9999999999abcdef9999999999abcdef99999999"
    gh_client.get_comments.return_value = [{"body": base_ack_envelope.replace(
        "0123456789abcdef0123456789abcdef01234567", pushed)}]

    with patch.dict(os.environ, mock_env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            with patch(
                "flow_agent.roles.developer.runner.create_and_verify_pr",
                return_value="PR Creation Output: https://github.com/owner/repo/pull/2",
            ):
                with patch(
                    "flow_agent.roles.developer.runner.get_current_git_sha",
                    return_value="9999999999abcdef9999999999abcdef99999999",
                ):
                    await run_developer(
                        tenant="owner/repo",
                        pr=1,
                        task_prompt="Implement feature",
                        gh_client=gh_client,
                    )

                    gh_client.post_pr_comment.assert_not_called()


@pytest.mark.asyncio
async def test_developer_publish_gh_client_none(mock_env, base_ack_envelope):
    fake_runtime = FakePublishRuntime(envelope_text=base_ack_envelope)
    register_runtime("fake_publish", lambda: fake_runtime)

    with patch.dict(os.environ, mock_env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            with patch(
                "flow_agent.roles.developer.runner.create_and_verify_pr",
                return_value="PR Creation Output: https://github.com/owner/repo/pull/2",
            ) as mock_create_pr:
                await run_developer(
                    tenant="owner/repo",
                    pr=1,
                    task_prompt="Implement feature",
                    gh_client=None,
                )

                mock_create_pr.assert_not_called()


@pytest.mark.asyncio
async def test_developer_publish_invalid_envelope(mock_env):
    fake_runtime = FakePublishRuntime(envelope_text="```flow-ack\n{}\n```")
    register_runtime("fake_publish", lambda: fake_runtime)

    gh_client = MagicMock(spec=RealGitHubClient)

    with patch.dict(os.environ, mock_env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            with pytest.raises(ValueError, match="Expected Ack envelope"):
                await run_developer(
                    tenant="owner/repo",
                    pr=1,
                    task_prompt="Implement feature",
                    gh_client=gh_client,
                )


@pytest.mark.asyncio
async def test_developer_no_ack_when_pr_not_created(mock_env, base_ack_envelope):
    register_runtime("fake_publish", lambda: FakePublishRuntime(envelope_text=base_ack_envelope))
    gh_client = MagicMock(spec=RealGitHubClient)
    gh_client.get_comments.return_value = []

    with patch.dict(os.environ, mock_env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")
            with patch(
                "flow_agent.roles.developer.runner.create_and_verify_pr",
                return_value="Failed to create PR: no commits on flow/issue-1 beyond main",
            ):
                with pytest.raises(RuntimeError, match="no PR"):
                    await run_developer(tenant="owner/repo", pr=1, task_prompt="x", gh_client=gh_client)

    gh_client.post_pr_comment.assert_not_called()
