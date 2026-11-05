import os
from unittest.mock import patch

import pytest

from flow_agent.telemetry.session_registry import SessionRegistry


@pytest.mark.asyncio
async def test_j08_ac1_session_registry_uses_cp_endpoint_no_supabase_keys():
    env_vars = {
        "CONTROL_PANEL_URL": "http://fake.cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "fake-cred"
    }

    with patch.dict(os.environ, env_vars, clear=True):
        registry = SessionRegistry("i-123", "owner/repo", 42)
        assert registry.enabled
        assert registry.cp_url == "http://fake.cp"

        with patch("httpx.post") as mock_post:
            await registry.register()

            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert "http://fake.cp/v1/runs/run-123/heartbeat" in args[0]
            assert kwargs["headers"]["Authorization"] == "Bearer fake-cred"
            assert kwargs["json"]["status"] == "Running"

@pytest.mark.asyncio
async def test_j08_ac1_session_registry_degrades_gracefully_when_no_credentials():
    env_vars = {}
    with patch.dict(os.environ, env_vars, clear=True):
        registry = SessionRegistry("i-123", "owner/repo", 42)
        assert not registry.enabled

        with patch("httpx.post") as mock_post:
            await registry.register()
            mock_post.assert_not_called()
