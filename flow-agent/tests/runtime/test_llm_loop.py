import pytest

from flow_agent.llm import FakeLLMClient, Reply, ToolCall
from flow_agent.roles.base import RoleSpec
from flow_agent.runtime.llm_loop import llm_loop


@pytest.mark.asyncio
async def test_ac2_loop_driven_by_fake_client_with_2_tool_rolespec():
    executed_tools = []

    async def mock_executor(name: str, args: dict):
        executed_tools.append((name, args))
        return f"Result of {name}"

    r1 = Reply(
        tool_calls=[
            ToolCall(name="tool_a", args={"arg1": "val1"}),
        ]
    )
    r2 = Reply(
        tool_calls=[
            ToolCall(name="tool_b", args={"count": 42}),
        ]
    )
    r3 = Reply(text="All tasks finished. ✅ Complete")

    fake = FakeLLMClient(canned_replies=[r1, r2, r3])

    spec = RoleSpec(
        name="test_role",
        system_prompt="Test system prompt",
        tools=frozenset({"tool_a", "tool_b"}),
        llm=fake,
        done=lambda reply: bool(reply.text and "✅ Complete" in reply.text),
    )

    final_reply = await llm_loop(spec, task_prompt="Do tasks", tool_executor=mock_executor)

    assert final_reply.text == "All tasks finished. ✅ Complete"
    assert len(executed_tools) == 2
    assert executed_tools[0] == ("tool_a", {"arg1": "val1"})
    assert executed_tools[1] == ("tool_b", {"count": 42})


@pytest.mark.asyncio
async def test_ac3_tool_outside_rolespec_refused_not_executed():
    executed_tools = []

    async def mock_executor(name: str, args: dict):
        executed_tools.append((name, args))
        return f"Executed {name}"

    # Model attempts to call 'forbidden_tool' which is outside allowed tools
    r1 = Reply(
        tool_calls=[
            ToolCall(name="forbidden_tool", args={"cmd": "rm -rf /"}),
        ]
    )
    r2 = Reply(text="I understand the tool was forbidden. ✅ Complete")

    fake = FakeLLMClient(canned_replies=[r1, r2])

    spec = RoleSpec(
        name="evaluator",
        system_prompt="Eval system prompt",
        tools=frozenset({"allowed_tool"}),
        llm=fake,
        done=lambda reply: bool(reply.text and "✅ Complete" in reply.text),
    )

    final_reply = await llm_loop(spec, task_prompt="Evaluate PR", tool_executor=mock_executor)

    # Tool executor was NEVER called
    assert len(executed_tools) == 0

    # The tool result passed back to the LLM in message history recorded the refusal
    last_call = fake.history[1]
    messages = last_call["messages"]
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "not allowed" in str(tool_messages[0].content).lower()
    assert "evaluator" in str(tool_messages[0].content)

    assert "✅ Complete" in (final_reply.text or "")
