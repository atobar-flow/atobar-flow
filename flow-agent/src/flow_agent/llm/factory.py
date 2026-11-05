"""Factory for instantiating provider-neutral LLM clients."""

from flow_agent.llm.claude import ClaudeClient
from flow_agent.llm.fake import FakeLLMClient
from flow_agent.llm.gemini import GeminiClient
from flow_agent.llm.openai_compatible import DeepSeekClient, GrokClient
from flow_agent.llm.protocol import LLMClient


def get_client(provider: str, model: str, api_key: str = "") -> LLMClient:
    """Returns an LLMClient instance for the given provider and model."""
    provider_clean = provider.strip().lower()

    if provider_clean == "gemini":
        return GeminiClient(model=model, api_key=api_key)
    if provider_clean in ("claude", "anthropic"):
        return ClaudeClient(model=model, api_key=api_key)
    if provider_clean == "deepseek":
        return DeepSeekClient(model=model, api_key=api_key)
    if provider_clean in ("grok", "xai"):
        return GrokClient(model=model, api_key=api_key)
    if provider_clean == "fake":
        return FakeLLMClient()

    raise ValueError(
        f"Unsupported LLM provider: {provider!r}. Supported providers: gemini, claude, deepseek, grok"
    )
