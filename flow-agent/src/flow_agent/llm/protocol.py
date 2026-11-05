"""Provider-neutral LLM client protocol and message/reply models."""

from typing import Any, Literal, Optional, Protocol, Union, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: Optional[str] = None


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached_input_tokens: Optional[int] = None


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "system", "tool"]
    content: Union[str, list[dict[str, Any]]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for provider-neutral LLM clients."""

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        system_instruction: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Reply:
        """Sends a conversation history and optional tool specs to the model, returning a Reply."""
        ...
