import pytest

from flow_agent.llm.providers import (
    canonical_provider,
    key_env_var,
    nanobot_api_base,
    nanobot_config_key,
)


def test_canonical_provider_aliases():
    assert canonical_provider("Anthropic") == "claude"
    assert canonical_provider("xai") == "grok"
    assert canonical_provider("google") == "gemini"
    assert canonical_provider("deepseek") == "deepseek"
    assert canonical_provider("openai") == "openai"


def test_canonical_provider_rejects_unknown():
    with pytest.raises(ValueError, match="Unsupported"):
        canonical_provider("openrouter")


def test_nanobot_mapping():
    assert nanobot_config_key("claude") == "anthropic"
    assert nanobot_config_key("gemini") == "gemini"
    assert nanobot_config_key("grok") == "grok"  # custom entry
    assert nanobot_api_base("grok") == "https://api.x.ai/v1"
    assert nanobot_api_base("claude") == ""


def test_key_env_var():
    assert key_env_var("claude") == "FLOW_LLM_API_KEY_CLAUDE"


def test_manifest_llm_normalizes_provider():
    from atobar_contracts.manifest import RoleLLMConfig, TenantLLMConfig

    cfg = RoleLLMConfig(provider="anthropic", model="claude-sonnet-4-5")
    assert cfg.provider == "claude"

    tenant = TenantLLMConfig(default=cfg)
    assert tenant.for_role("evaluator") is cfg
    assert tenant.for_role("developer") is cfg
