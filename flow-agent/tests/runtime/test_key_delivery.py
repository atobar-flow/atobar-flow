from unittest.mock import MagicMock, patch

from flow_agent.runtime.key_delivery import TenantLLMKey, fetch_run_llm_key


def test_fetch_run_llm_key_returns_tenant_llm_key():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"provider": "gemini", "api_key": "test_key_123"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        key = fetch_run_llm_key("https://fake-cp", "run-1", "cred-1")

    assert isinstance(key, TenantLLMKey)
    assert key.provider == "gemini"
    assert key.api_key == "test_key_123"
    # Ensure it supports dictionary-like or property-based access
    assert key["provider"] == "gemini"
    assert key["api_key"] == "test_key_123"
