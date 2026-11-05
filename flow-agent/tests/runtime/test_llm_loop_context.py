from typing import Any, Optional

import pytest

from flow_agent.llm import LLMClient, Message, Reply, ToolCall, ToolSpec
from flow_agent.roles.base import RoleSpec
from flow_agent.runtime.llm_loop import llm_loop


class RecordingLLMClient(LLMClient):
    def __init__(self, replies: list[Reply]):
        self.replies = list(replies)
        self.recorded_messages: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        self.recorded_messages.append(list(messages))
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_assistant_reply_present_in_messages_on_second_iteration():
    async def mock_executor(name: str, args: dict[str, Any]):
        return "tool execution output"

    r1 = Reply(
        text="I will now inspect the repository structure.",
        tool_calls=[ToolCall(name="tool_a", args={})],
    )
    r2 = Reply(
        text="Done with the task. ✅ Complete",
    )

    client = RecordingLLMClient([r1, r2])

    spec = RoleSpec(
        name="test_role",
        system_prompt="Test system prompt",
        tools=frozenset({"tool_a"}),
        llm=client,
        done=lambda reply: bool(reply.text and "✅ Complete" in reply.text),
    )

    await llm_loop(spec, task_prompt="Start task", tool_executor=mock_executor)

    assert len(client.recorded_messages) == 2
    second_turn_messages = client.recorded_messages[1]

    # Verify assistant turn is present in messages before tool result
    assistant_turns = [m for m in second_turn_messages if m.role == "assistant"]
    assert len(assistant_turns) >= 1
    assert assistant_turns[0].content == "I will now inspect the repository structure."
