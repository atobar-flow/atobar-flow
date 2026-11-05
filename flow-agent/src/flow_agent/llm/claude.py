"""Anthropic Claude LLM adapter via Anthropic Messages API."""

from typing import Any, Optional

import httpx

from flow_agent.llm.pricing import calculate_cost
from flow_agent.llm.protocol import LLMClient, Message, Reply, ToolCall, ToolSpec, Usage


class ClaudeClient(LLMClient):
    """Adapter for Anthropic Claude models via Anthropic Messages HTTP API."""

    def __init__(
        self,
        model: str = "claude-3-5-sonnet",
        api_key: str = "",
        _http_client: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._http = _http_client or httpx.Client(timeout=60.0)

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        anthropic_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            role = "assistant" if m.role == "assistant" else "user"
            content: Any = m.content
            if isinstance(content, list):
                anthropic_messages.append({"role": role, "content": content})
            else:
                anthropic_messages.append({"role": role, "content": str(content)})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
            "temperature": temperature,
        }

        if system_instruction:
            payload["system"] = system_instruction

        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        resp = self._http.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id"),
                        name=block.get("name", ""),
                        args=block.get("input", {}),
                    )
                )

        usage_data = data.get("usage", {})
        inp = int(usage_data.get("input_tokens", 0))
        out = int(usage_data.get("output_tokens", 0))
        cost = calculate_cost(self.model, inp, out)

        return Reply(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=Usage(input_tokens=inp, output_tokens=out, cost_usd=cost),
        )
