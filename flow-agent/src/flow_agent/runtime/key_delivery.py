"""Fetch single-use tenant LLM keys into sandbox memory without persisting to disk or logging."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from flow_agent.llm.factory import get_client
from flow_agent.llm.protocol import LLMClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantLLMKey:
    """Decrypted single-use LLM key credentials held in-memory."""

    provider: str
    api_key: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        if item == "provider":
            return self.provider
        if item == "api_key":
            return self.api_key
        raise KeyError(item)


def fetch_run_llm_key(
    control_panel_url: str,
    run_id: str,
    credential: str,
    timeout: float = 15.0,
) -> TenantLLMKey:
    """Fetches the decrypted tenant LLM key from Control Panel using a single-use credential.

    Plaintext keys are held in memory only and never written to disk or logged.
    """
    url = f"{control_panel_url.rstrip('/')}/v1/runs/{run_id}/llm-key"
    headers = {"Authorization": f"Bearer {credential}"}
    inst_id = os.environ.get("INSTANCE_ID", "").strip()
    if inst_id:
        headers["X-Instance-ID"] = inst_id

    # Redact sensitive parameters from logging
    logger.info("Requesting LLM key for run %s from Control Panel", run_id)

    try:
        resp = httpx.post(url, headers=headers, timeout=timeout)
        resp.raise_for_status()

        data: dict[str, Any] = resp.json()
        provider = str(data.get("provider", "gemini"))
        api_key = str(data.get("api_key", ""))
        model = data.get("model")
        reasoning_effort = data.get("reasoning_effort")

        logger.info("Retrieved LLM key for provider %s (run %s)", provider, run_id)
        try:
            from flow_agent.telemetry.event_sink import emit
            emit("llm_key", ok=True, provider=provider)
        except Exception:
            pass
        return TenantLLMKey(
            provider=provider, api_key=api_key, model=model, reasoning_effort=reasoning_effort
        )
    except Exception as exc:
        try:
            from flow_agent.telemetry.event_sink import emit
            emit("llm_key", ok=False, error=str(exc)[:500])
        except Exception:
            pass
        raise


def export_llm_keys(key_info: TenantLLMKey) -> None:
    """Exports the single active role's delivered key into sandbox env vars consumed by external runtimes."""
    os.environ["FLOW_LLM_API_KEY"] = key_info.api_key
    os.environ["FLOW_LLM_PROVIDER"] = key_info.provider
    if "GEMINI_API_KEY" in os.environ:
        del os.environ["GEMINI_API_KEY"]
    if key_info.model:
        os.environ["FLOW_ROLE_MODEL"] = key_info.model
    if key_info.reasoning_effort:
        os.environ["FLOW_ROLE_REASONING_EFFORT"] = key_info.reasoning_effort


def clear_llm_keys() -> None:
    """Purges all role-scoped LLM API keys and model env vars before switching roles on a warm VM."""
    to_remove = [
        k
        for k in list(os.environ.keys())
        if k.startswith("FLOW_LLM_API_KEY")
        or k in ("FLOW_LLM_PROVIDER", "FLOW_ROLE_MODEL", "FLOW_ROLE_REASONING_EFFORT", "GEMINI_API_KEY")
    ]
    for k in to_remove:
        os.environ.pop(k, None)


def fetch_run_wiki(
    control_panel_url: str,
    run_id: str,
    credential: str,
    timeout: float = 10.0,
) -> Optional[dict[str, Any]]:
    """Fetches the tenant's SotaWiki from the Control Panel using the run credential.

    Returns None if the wiki has not been generated or cannot be fetched.
    """
    if not (control_panel_url and run_id and credential):
        return None
    url = f"{control_panel_url.rstrip('/')}/v1/runs/{run_id}/wiki"
    headers = {"Authorization": f"Bearer {credential}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.debug("Could not fetch SotaWiki for run %s: %s", run_id, exc)
    return None



def require_model(key_info: TenantLLMKey) -> str:
    """The role's model as set in the Control Panel; there is no default."""
    if not key_info.model:
        raise ValueError("No model delivered for this role; configure it in the Control Panel")
    return key_info.model


def build_role_llm_client(
    control_panel_url: str,
    run_id: str,
    credential: str,
) -> LLMClient:
    """Delivers the tenant's LLM key into memory and constructs the role LLMClient."""
    key_info = fetch_run_llm_key(control_panel_url, run_id, credential)
    return get_client(
        provider=key_info.provider,
        model=require_model(key_info),
        api_key=key_info.api_key,
    )
