"""OpenAI-compatible LLM adapter for providers like DeepSeek and Grok."""

import json
from typing import Any, Optional

import httpx

from flow_agent.llm.pricing import calculate_cost
from flow_agent.llm.protocol import LLMClient, Message, Reply, ToolCall, ToolSpec, Usage


class OpenAICompatibleClient(LLMClient):
    """Adapter for OpenAI-compatible chat completion APIs."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        _http_client: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = _http_client or httpx.Client(timeout=60.0)

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        formatted_messages: list[dict[str, Any]] = []

        if system_instruction:
            formatted_messages.append({"role": "system", "content": system_instruction})

        for m in messages:
            formatted_messages.append({"role": m.role, "content": m.content})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "temperature": temperature,
        }

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        resp = self._http.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        choice = data.get("choices", [{}])[0]
        choice_msg = choice.get("message", {})

        text = choice_msg.get("content")

        tool_calls: list[ToolCall] = []
        raw_tool_calls = choice_msg.get("tool_calls", [])
        if raw_tool_calls:
            for rtc in raw_tool_calls:
                fn = rtc.get("function", {})
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=rtc.get("id"),
                        name=fn.get("name", ""),
                        args=args,
                    )
                )

        usage_data = data.get("usage", {})
        inp = int(usage_data.get("prompt_tokens", 0))
        out = int(usage_data.get("completion_tokens", 0))
        cost = calculate_cost(self.model, inp, out)

        return Reply(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(input_tokens=inp, output_tokens=out, cost_usd=cost),
        )


class DeepSeekClient(OpenAICompatibleClient):
    """Adapter for DeepSeek models."""

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        _http_client: Optional[Any] = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            _http_client=_http_client,
        )


class GrokClient(OpenAICompatibleClient):
    """Adapter for xAI Grok models."""

    def __init__(
        self,
        model: str = "grok-beta",
        api_key: str = "",
        base_url: str = "https://api.x.ai/v1",
        _http_client: Optional[Any] = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            _http_client=_http_client,
        )
