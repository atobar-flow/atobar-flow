from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from control_panel.llm_keys import (
    PROVIDERS,
    _claude_valid,
    _deepseek_valid,
    _gemini_valid,
    _grok_valid,
    decrypt,
    encrypt,
)

# --- AC-2: Control Panel accepts and validates each provider key ---

def test_ac2_providers_registered():
    assert "gemini" in PROVIDERS
    assert "claude" in PROVIDERS
    assert "deepseek" in PROVIDERS
    assert "grok" in PROVIDERS


@patch("control_panel.llm_keys.httpx.get")
def test_ac2_claude_validation(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": [{"id": "claude-1"}]}
    mock_get.return_value = mock_resp

    assert _claude_valid("sk-ant-valid") == (True, ["claude-1"])

    mock_resp.status_code = 401
    assert _claude_valid("sk-ant-invalid") == (False, [])

    mock_resp.status_code = 400
    mock_resp.text = (
        '{"type":"error","error":{"type":"invalid_request_error",'
        '"message":"This API key is not scoped to a workspace"}}'
    )
    with pytest.raises(ValueError, match="not scoped to a workspace"):
        _claude_valid("sk-ant-usr-unscoped")


def test_tenant_llm_keys_migration_allows_all_canonical_providers():
    sql = Path("infra/supabase/migrations/0018_tenant_llm_keys_providers.sql").read_text()
    for provider in PROVIDERS:
        assert f"'{provider}'" in sql


@patch("control_panel.llm_keys.httpx.get")
def test_ac2_deepseek_validation(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": [{"id": "deepseek-chat"}]}
    mock_get.return_value = mock_resp

    assert _deepseek_valid("sk-deepseek-valid") == (True, ["deepseek-chat"])

    mock_resp.status_code = 401
    assert _deepseek_valid("sk-deepseek-invalid") == (False, [])


@patch("control_panel.llm_keys.httpx.get")
def test_ac2_grok_validation(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": [{"id": "grok-4"}]}
    mock_get.return_value = mock_resp

    assert _grok_valid("xai-valid") == (True, ["grok-4"])

    mock_resp.status_code = 403
    assert _grok_valid("xai-invalid") == (False, [])


def test_ac2_all_providers_encryption_roundtrip():
    kek = b"0" * 32
    tenant_id = "tenant-123"

    for provider in ("gemini", "claude", "deepseek", "grok"):
        raw_key = f"fake-test-key-{provider}-secret"
        encrypted = encrypt(kek, tenant_id, provider, raw_key)
        decrypted = decrypt(kek, tenant_id, provider, encrypted)
        assert decrypted == raw_key


@patch("control_panel.llm_keys.httpx.get")
def test_gemini_validation_paginates_all_pages(mock_get):
    page1 = MagicMock()
    page1.status_code = 200
    page1.raise_for_status.return_value = None
    page1.json.return_value = {
        "models": [{"name": "models/gemini-2.5-flash"}],
        "nextPageToken": "page-2-token",
    }

    page2 = MagicMock()
    page2.status_code = 200
    page2.raise_for_status.return_value = None
    page2.json.return_value = {
        "models": [{"name": "models/gemini-4-argon"}],
    }

    mock_get.side_effect = [page1, page2]
    valid, models = _gemini_valid("fake-gemini-key")
    assert valid is True
    assert models == ["gemini-2.5-flash", "gemini-4-argon"]
    assert mock_get.call_count == 2

