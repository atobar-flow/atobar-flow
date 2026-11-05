"""Canonical LLM provider names, env var helpers, and httpx key validation."""

from dataclasses import dataclass
from typing import Callable, Optional

import httpx

CANONICAL_PROVIDERS = frozenset({"gemini", "claude", "deepseek", "grok", "openai"})

_ALIASES = {
    "anthropic": "claude",
    "xai": "grok",
    "x.ai": "grok",
    "google": "gemini",
    "googleai": "gemini",
    "google_ai": "gemini",
}

_NANOBOT_NATIVE_KEYS = {
    "gemini": "gemini",
    "claude": "anthropic",
    "deepseek": "deepseek",
    "openai": "openai",
}

_CUSTOM_API_BASES = {
    "grok": "https://api.x.ai/v1",
}


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    docs_url: str
    validate: Callable[[str], tuple[bool, list[str]]]


def canonical_provider(name: str) -> str:
    """Normalizes a provider alias to its canonical name."""
    p = name.strip().lower()
    p = _ALIASES.get(p, p)
    if p not in CANONICAL_PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider: {name!r}. "
            f"Supported: {sorted(CANONICAL_PROVIDERS)}"
        )
    return p


def key_env_var(provider: str) -> str:
    """Env var carrying the in-RAM API key for a provider inside the sandbox."""
    return f"FLOW_LLM_API_KEY_{provider.upper()}"


def nanobot_config_key(provider: str) -> str:
    """Returns the key under which a provider appears in nanobot's providers map."""
    return _NANOBOT_NATIVE_KEYS.get(provider, provider)


def nanobot_api_base(provider: str) -> str:
    """apiBase to emit for custom (non-nanobot-native) providers, else empty."""
    return _CUSTOM_API_BASES.get(provider, "")


def _gemini_valid(api_key: str) -> tuple[bool, list[str]]:
    models: list[str] = []
    page_token: Optional[str] = None
    for _ in range(10):
        params: dict[str, str | int] = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            params=params,
            timeout=15,
        )
        if resp.status_code in (400, 401, 403):
            return False, []
        resp.raise_for_status()
        data = resp.json()
        for m in data.get("models", []):
            name = m["name"].split("/")[-1]
            if name not in models:
                models.append(name)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return True, models


def _claude_valid(api_key: str) -> tuple[bool, list[str]]:
    resp = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        timeout=15,
    )
    if resp.status_code in (400, 401, 403):
        if (
            resp.status_code == 400
            and isinstance(getattr(resp, "text", None), str)
            and "not scoped to a workspace" in resp.text
        ):
            raise ValueError(
                "Anthropic Claude rejected this key: key is not scoped to a workspace "
                "(select a workspace when creating the key in the Anthropic Console)"
            )
        return False, []
    resp.raise_for_status()
    data = resp.json()
    models = [m["id"] for m in data.get("data", [])]
    return True, models


def _deepseek_valid(api_key: str) -> tuple[bool, list[str]]:
    resp = httpx.get(
        "https://api.deepseek.com/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if resp.status_code in (400, 401, 403):
        return False, []
    resp.raise_for_status()
    data = resp.json()
    models = [m["id"] for m in data.get("data", [])]
    return True, models


def _grok_valid(api_key: str) -> tuple[bool, list[str]]:
    resp = httpx.get(
        "https://api.x.ai/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if resp.status_code in (400, 401, 403):
        return False, []
    resp.raise_for_status()
    data = resp.json()
    models = [m["id"] for m in data.get("data", [])]
    return True, models


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider("gemini", "Google Gemini", "https://aistudio.google.com/app/apikey", _gemini_valid),
    "claude": Provider("claude", "Anthropic Claude", "https://console.anthropic.com/settings/keys", _claude_valid),
    "deepseek": Provider("deepseek", "DeepSeek", "https://platform.deepseek.com/api_keys", _deepseek_valid),
    "grok": Provider("grok", "xAI Grok", "https://console.x.ai/", _grok_valid),
}
