"""Fake LLM client for tests."""

from typing import Optional

from flow_agent.llm.protocol import LLMClient, Message, Reply, ToolSpec


class FakeLLMClient(LLMClient):
    """Fake LLM client returning canned replies and recording call history."""

    def __init__(
        self,
        canned_replies: Optional[list[Reply]] = None,
        default_reply: Optional[Reply] = None,
    ) -> None:
        self.canned_replies = list(canned_replies) if canned_replies else []
        self.default_reply = default_reply or Reply(text="OK")
        self.history: list[dict[str, object]] = []

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        self.history.append({
            "messages": messages,
            "tools": tools,
            "system_instruction": system_instruction,
            "temperature": temperature,
        })
        if self.canned_replies:
            return self.canned_replies.pop(0)
        return self.default_reply
