"""Compatibility re-export of LLM provider canonicalization and helpers from atobar_contracts."""

from atobar_contracts.providers import (
    CANONICAL_PROVIDERS,
    PROVIDERS,
    Provider,
    _claude_valid,
    _deepseek_valid,
    _gemini_valid,
    _grok_valid,
    canonical_provider,
    key_env_var,
    nanobot_api_base,
    nanobot_config_key,
)

__all__ = [
    "CANONICAL_PROVIDERS",
    "PROVIDERS",
    "Provider",
    "_claude_valid",
    "_deepseek_valid",
    "_gemini_valid",
    "_grok_valid",
    "canonical_provider",
    "key_env_var",
    "nanobot_api_base",
    "nanobot_config_key",
]
