"""Gemini LLM adapter wrapping google.genai."""

from typing import Any, Optional

from google.genai import Client, types

from flow_agent.llm.pricing import calculate_cost
from flow_agent.llm.protocol import LLMClient, Message, Reply, ToolCall, ToolSpec, Usage


class GeminiClient(LLMClient):
    """Adapter for Google Gemini models via google.genai SDK."""

    def __init__(
        self,
        model: str,
        api_key: str = "",
        _genai_client: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        if _genai_client is not None:
            self._client = _genai_client
        else:
            self._client = Client(api_key=api_key)

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        contents: list[types.Content] = []
        for m in messages:
            role = "model" if m.role == "assistant" else "user"
            parts: list[types.Part] = []
            if isinstance(m.content, str):
                parts.append(types.Part.from_text(text=m.content))
            elif isinstance(m.content, list):
                for item in m.content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(types.Part.from_text(text=item["text"]))
                    elif isinstance(item, dict) and "data" in item and "mime_type" in item:
                        parts.append(types.Part.from_bytes(data=item["data"], mime_type=item["mime_type"]))
            contents.append(types.Content(role=role, parts=parts))

        genai_tools: Optional[list[Any]] = None
        if tools:
            declarations = [
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters_json_schema=t.parameters,
                )
                for t in tools
            ]
            genai_tools = [types.Tool(function_declarations=declarations)]

        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            tools=genai_tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        response = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        tool_calls: list[ToolCall] = []
        if getattr(response, "function_calls", None):
            for call in response.function_calls:
                args = dict(call.args) if call.args else {}
                tool_calls.append(ToolCall(name=call.name, args=args))

        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = None
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata:
            input_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0
            cached_input_tokens = getattr(usage_metadata, "cached_content_token_count", None)

        cost = calculate_cost(self.model, input_tokens, output_tokens)

        return Reply(
            text=getattr(response, "text", None),
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                cached_input_tokens=cached_input_tokens,
            ),
        )
