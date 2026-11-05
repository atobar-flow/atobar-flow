import os
from unittest.mock import patch

import pytest

from flow_agent.agents.base import AgentRuntime, RoleResult, RoleTask, RuntimeHooks, UsageLedger
from flow_agent.agents.registry import register_runtime
from flow_agent.roles.developer.runner import run_developer
from flow_agent.runtime.tools import allowlist_for


class FakeMockRuntime(AgentRuntime):
    def __init__(self):
        self.recorded_task = None
        self.recorded_hooks = None

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        self.recorded_task = task
        self.recorded_hooks = hooks
        return RoleResult(
            final_text="Fake result",
            envelope_text="```flow-ack\n{}\n```",
            usage=UsageLedger(),
            halt="done",
        )


@pytest.mark.asyncio
async def test_developer_runtime_dispatch_custom_runtime():
    fake_runtime = FakeMockRuntime()
    register_runtime("fake_dev", lambda: fake_runtime)

    env = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-123",
        "FLOW_AGENT_RUNTIME": "fake_dev",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch:
            from flow_agent.runtime.key_delivery import TenantLLMKey
            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="secret-key", model="gemini-3.8-flash")

            await run_developer(
                tenant="owner/repo",
                pr=1,
                task_prompt="Implement feature",
            )
            assert os.environ.get("FLOW_LLM_API_KEY") == "secret-key"

    assert fake_runtime.recorded_task is not None
    assert fake_runtime.recorded_task.allowed_tools == allowlist_for("developer")
    assert fake_runtime.recorded_task.done_envelope == "flow-ack"


@pytest.mark.asyncio
async def test_developer_missing_credentials_fails_closed():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="Failed to fetch tenant LLM key"):
            await run_developer(
                tenant="owner/repo",
                pr=1,
                task_prompt="Implement feature",
            )
