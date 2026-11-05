"""Bring-your-own LLM keys: AES-256-GCM envelope under a KEK from AWS SM.

Ciphertext is bound to (tenant_id, provider) via AEAD associated data, so a row
copied to another tenant or provider fails to decrypt. Plaintext keys never
leave this module except to the provider's validation endpoint or a tenant's
role sandbox.
"""

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_VERSION = 1
NONCE_BYTES = 12


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    docs_url: str
    validate: Callable[[str], tuple[bool, list[str]]]


def _gemini_valid(api_key: str) -> tuple[bool, list[str]]:
    """Free metadata call; no tokens are billed. Paginates all pages so new models (e.g. gemini-4-argon) are returned."""
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
    """Validate Anthropic API key via models endpoint."""
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
    """Validate DeepSeek API key via models endpoint."""
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
    """Validate xAI Grok API key via models endpoint."""
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


def get_kek() -> Optional[bytes]:
    raw = os.environ.get("CP_LLM_KEK", "")
    if not raw:
        return None
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError("CP_LLM_KEK must be 32 bytes, base64-encoded")
    return key


def get_kms_client() -> Optional[Any]:
    key_id = os.environ.get("CP_KMS_KEY_ID", "").strip()
    if not key_id:
        return None
    import boto3

    region = os.environ.get("AWS_REGION", "us-west-2")
    return boto3.client("kms", region_name=region)


@dataclass(frozen=True)
class SealedKey:
    ciphertext: str
    wrapped_dek: Optional[str] = None
    kek_version: int = 1
    kms_key_id: Optional[str] = None
    encryption_mode: str = "local_kek"


def _aad(tenant_id: str, provider: str) -> bytes:
    return f"atobar-flow/llm-key/v{KEK_VERSION}/{tenant_id}/{provider}".encode()


def _kms_aad(tenant_id: str, provider: str) -> bytes:
    return f"{tenant_id}:{provider}".encode("utf-8")


def encrypt(kek: bytes, tenant_id: str, provider: str, api_key: str) -> str:
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(kek).encrypt(nonce, api_key.encode(), _aad(tenant_id, provider))
    return base64.b64encode(nonce + ct).decode()


def decrypt(kek: bytes, tenant_id: str, provider: str, ciphertext: str) -> str:
    blob = base64.b64decode(ciphertext)
    return AESGCM(kek).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _aad(tenant_id, provider)).decode()


def seal_llm_key(
    plaintext: str,
    *,
    tenant_id: str,
    provider: str,
    kek: Optional[bytes] = None,
    kms_client: Optional[Any] = None,
    kms_key_id: Optional[str] = None,
) -> SealedKey:
    resolved_kms = kms_client if kms_client is not None else get_kms_client()
    resolved_key_id = kms_key_id or os.environ.get("CP_KMS_KEY_ID", "").strip() or "alias/flow-tenant-llm-cmk"
    if resolved_kms is not None:
        context = {"tenant_id": str(tenant_id), "provider": provider}
        resp = resolved_kms.generate_data_key(
            KeyId=resolved_key_id,
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        dek = bytes(resp["Plaintext"])
        wrapped_blob = bytes(resp["CiphertextBlob"])
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), _kms_aad(str(tenant_id), provider))
        return SealedKey(
            ciphertext=base64.b64encode(nonce + ct).decode("ascii"),
            wrapped_dek=base64.b64encode(wrapped_blob).decode("ascii"),
            kek_version=2,
            kms_key_id=str(resp.get("KeyId") or resolved_key_id),
            encryption_mode="kms_envelope",
        )

    resolved_kek = kek if kek is not None else get_kek()
    if resolved_kek is None:
        raise ValueError("Neither KMS client nor CP_LLM_KEK is configured")
    ct_b64 = encrypt(resolved_kek, str(tenant_id), provider, plaintext)
    return SealedKey(
        ciphertext=ct_b64,
        wrapped_dek=None,
        kek_version=1,
        kms_key_id=None,
        encryption_mode="local_kek",
    )


def open_llm_key(
    row_or_ciphertext: Union[str, dict[str, Any], SealedKey],
    *,
    tenant_id: str,
    provider: str,
    kek: Optional[bytes] = None,
    kms_client: Optional[Any] = None,
    wrapped_dek: Optional[str] = None,
    kek_version: Optional[int] = None,
) -> str:
    if isinstance(row_or_ciphertext, SealedKey):
        ciphertext = row_or_ciphertext.ciphertext
        wrapped_dek = row_or_ciphertext.wrapped_dek
        kek_version = row_or_ciphertext.kek_version
    elif isinstance(row_or_ciphertext, dict):
        ciphertext = str(row_or_ciphertext["ciphertext"])
        wrapped_dek = row_or_ciphertext.get("wrapped_dek") or wrapped_dek
        kek_version = int(row_or_ciphertext.get("kek_version") or kek_version or (2 if wrapped_dek else 1))
    else:
        ciphertext = str(row_or_ciphertext)
        kek_version = int(kek_version or (2 if wrapped_dek else 1))

    if kek_version == 2 or wrapped_dek:
        resolved_kms = kms_client if kms_client is not None else get_kms_client()
        if resolved_kms is None or not wrapped_dek:
            raise ValueError("KMS client and wrapped_dek are required for kek_version=2")
        context = {"tenant_id": str(tenant_id), "provider": provider}
        resp = resolved_kms.decrypt(
            CiphertextBlob=base64.b64decode(wrapped_dek),
            EncryptionContext=context,
        )
        dek = bytes(resp["Plaintext"])
        blob = base64.b64decode(ciphertext)
        return AESGCM(dek).decrypt(
            blob[:NONCE_BYTES],
            blob[NONCE_BYTES:],
            _kms_aad(str(tenant_id), provider),
        ).decode("utf-8")

    resolved_kek = kek if kek is not None else get_kek()
    if resolved_kek is None:
        raise ValueError("CP_LLM_KEK is required for kek_version=1")
    return decrypt(resolved_kek, str(tenant_id), provider, ciphertext)


def last4(api_key: str) -> str:
    return api_key[-4:]
