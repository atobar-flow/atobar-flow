"""Provider-neutral LLM client interface and adapters."""

from flow_agent.llm.factory import get_client
from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.gemini import GeminiClient
from flow_agent.llm.pricing import calculate_cost
from flow_agent.llm.protocol import (
    LLMClient,
    Message,
    Reply,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    "FakeLLMClient",
    "GeminiClient",
    "LLMClient",
    "Message",
    "Reply",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "calculate_cost",
    "get_client",
]
