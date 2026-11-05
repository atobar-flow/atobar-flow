from unittest.mock import patch

import pytest

from flow_agent.llm import Reply, ToolCall, Usage
from flow_agent.roles.base import RoleSpec
from flow_agent.runtime.llm_loop import llm_loop


class FakeLLMWithUsage:
    def __init__(self, replies: list[Reply]):
        self.replies = list(replies)

    def chat(self, *args, **kwargs) -> Reply:
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_llm_loop_emits_model_call_and_tool_call_events():
    emitted_events = []

    def fake_emit(kind, **payload):
        emitted_events.append((kind, payload))

    async def mock_executor(name: str, args: dict):
        return "sample output"

    r1 = Reply(
        text="Starting tool call",
        tool_calls=[ToolCall(name="sample_tool", args={"foo": "bar"})],
        usage=Usage(input_tokens=120, output_tokens=30, cost_usd=0.005, cached_input_tokens=10),
    )
    r2 = Reply(
        text="All done. ✅ Complete",
        usage=Usage(input_tokens=50, output_tokens=10, cost_usd=0.001),
    )

    llm = FakeLLMWithUsage([r1, r2])
    spec = RoleSpec(
        name="developer",
        system_prompt="sys",
        tools=frozenset(["sample_tool"]),
        llm=llm,
        done=lambda reply: bool(reply.text and "✅ Complete" in reply.text),
    )

    with patch("flow_agent.runtime.llm_loop.emit", side_effect=fake_emit):
        await llm_loop(spec, task_prompt="Test task", tool_executor=mock_executor)

    kinds = [e[0] for e in emitted_events]
    assert "model_call" in kinds
    assert "tool_call" in kinds

    # Verify model_call payload
    model_call = next(e[1] for e in emitted_events if e[0] == "model_call")
    assert model_call["input_tokens"] == 120
    assert model_call["output_tokens"] == 30
    assert model_call["cost_usd"] == 0.005
    assert model_call["cached_input_tokens"] == 10

    # Verify tool_call payload
    tool_call = next(e[1] for e in emitted_events if e[0] == "tool_call")
    assert tool_call["name"] == "sample_tool"
    assert "args_head" in tool_call
    assert "result_head" in tool_call
    assert "duration_ms" in tool_call
    assert tool_call["ok"] is True
    assert tool_call["error"] is None
