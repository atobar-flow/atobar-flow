from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.nanobot_runtime import NanobotRuntime
from atobar_contracts.envelopes import Ack, render


class Fake429(Exception):
    def __init__(self, message: str, delay_str: str):
        super().__init__(message)
        self.status_code = 429
        self.response_json = {
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay_str},
                ],
            }
        }


@pytest.mark.asyncio
async def test_retry_delay_honored_up_to_120s(tmp_path):
    import asyncio
    real_sleep = asyncio.sleep
    sleep_calls = []

    async def fake_sleep(dur):
        sleep_calls.append(dur)
        await real_sleep(0)

    fake_bot = MagicMock()
    # Fail on first attempt with 45s delay, succeed on second
    ack_env = render(Ack(
        tenant="owner/repo",
        pr=1,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="run-1",
        addressed_findings=[],
        dispute=[],
    ))
    success_res = MagicMock(content=ack_env, messages=[])
    fake_bot.run = AsyncMock(side_effect=[
        Fake429("Rate limit", "45s"),
        success_res,
    ])

    task = RoleTask(
        role="developer",
        system_prompt="sys",
        context="ctx",
        task_prompt="task",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        with patch("asyncio.sleep", side_effect=fake_sleep):
            await runtime.run(task, hooks=RuntimeHooks())

    assert len(sleep_calls) >= 1
    # 45s delay must be honored, not truncated to 2.0s
    assert 45.0 in sleep_calls


@pytest.mark.asyncio
async def test_retry_delay_capped_at_120s(tmp_path):
    import asyncio
    real_sleep = asyncio.sleep
    sleep_calls = []

    async def fake_sleep(dur):
        sleep_calls.append(dur)
        await real_sleep(0)

    fake_bot = MagicMock()
    # Fail on first attempt with 200s delay, succeed on second
    ack_env = render(Ack(
        tenant="owner/repo",
        pr=1,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="run-1",
        addressed_findings=[],
        dispute=[],
    ))
    success_res = MagicMock(content=ack_env, messages=[])
    fake_bot.run = AsyncMock(side_effect=[
        Fake429("Rate limit", "200s"),
        success_res,
    ])

    task = RoleTask(
        role="developer",
        system_prompt="sys",
        context="ctx",
        task_prompt="task",
        workspace_dir=str(tmp_path),
        allowed_tools=frozenset(["read_file"]),
        model_name="gemini-2.5-flash",
        done_envelope="flow-ack",
    )

    runtime = NanobotRuntime()
    with patch("nanobot.Nanobot.from_config", return_value=fake_bot):
        with patch("asyncio.sleep", side_effect=fake_sleep):
            await runtime.run(task, hooks=RuntimeHooks())

    assert len(sleep_calls) >= 1
    # Capped at 120.0s
    assert 120.0 in sleep_calls
