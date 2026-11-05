import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow_agent.agents.base import RoleResult, UsageLedger
from flow_agent.roles.developer.runner import run_developer
from flow_agent.runtime.key_delivery import TenantLLMKey


@pytest.mark.asyncio
async def test_j07_ac2_developer_uses_byo_key_and_falls_back():
    env_vars = {
        "CONTROL_PANEL_URL": "http://fake-cp",
        "FLOW_RUN_ID": "run-123",
        "FLOW_RUN_CREDENTIAL": "cred-123",
        "FLOW_LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "factory_fallback_key"
    }

    fake_runtime = MagicMock()
    fake_runtime.run = AsyncMock(
        return_value=RoleResult(
            final_text="",
            envelope_text=None,
            usage=UsageLedger(),
            halt="done",
        )
    )

    with patch.dict(os.environ, env_vars, clear=True):
        with patch("flow_agent.roles.developer.runner.fetch_run_llm_key") as mock_fetch, \
             patch("flow_agent.roles.developer.runner.get_runtime", return_value=fake_runtime):

            mock_fetch.return_value = TenantLLMKey(provider="gemini", api_key="tenant-secret", model="gemini-4-argon")

            # Success path (tenant key)
            await run_developer(
                tenant="owner/repo",
                pr=1,
                task_prompt="test",
            )
            mock_fetch.assert_called_once_with(
                control_panel_url="http://fake-cp",
                run_id="run-123",
                credential="cred-123"
            )
            fake_runtime.run.assert_called_once()

            # AC-4: GEMINI_API_KEY removed from env
            assert "GEMINI_API_KEY" not in os.environ

            # Reset and simulate failure when key not found
            os.environ["GEMINI_API_KEY"] = "factory_fallback_key"
            mock_fetch.reset_mock()
            fake_runtime.run.reset_mock()
            mock_fetch.side_effect = Exception("key not found")

            with pytest.raises(Exception, match="key not found"):
                await run_developer(
                    tenant="owner/repo",
                    pr=1,
                    task_prompt="test",
                )

def test_j07_ac1_dispatch_flow_passes_credential():
    from atobar_contracts.conductor_io import apply_actions
    from atobar_contracts.machine import DispatchAction

    gh = MagicMock()
    store = MagicMock()

    store.tenant_by_repo.return_value = MagicMock(id="t-1")

    with patch("secrets.token_hex") as mock_hex:
        mock_hex.side_effect = ["id123", "cred123"]
        apply_actions("owner/repo", 42, [DispatchAction(role="developer", head_sha="a"*40)], gh, store)

        # Check store
        store.create_run_credential.assert_called_once()
        args, kwargs = store.create_run_credential.call_args
        assert args[0] == "run_id123"
        assert args[1] == "t-1"
        assert args[2] == "developer"

        # Check gh payload
        gh.dispatch_flow_run.assert_called_once()
        payload = gh.dispatch_flow_run.call_args[0][1]
        assert payload["run_id"] == "run_id123"
        assert payload["run_credential"] == "atb_run_cred123"


def test_j42_wp6_clients_send_x_instance_id_header(monkeypatch):
    from flow_agent.runtime.key_delivery import fetch_run_llm_key
    from flow_agent.telemetry.event_sink import EventSink
    from flow_agent.tools.vision_tools import _upload_screenshot_to_cp

    monkeypatch.setenv("INSTANCE_ID", "i-spot-wp6")
    monkeypatch.setenv("CONTROL_PANEL_URL", "https://cp.example.invalid")
    monkeypatch.setenv("FLOW_RUN_ID", "run-wp6")
    monkeypatch.setenv("FLOW_RUN_CREDENTIAL", "cred-wp6")

    seen_headers: list[dict[str, str]] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen_headers.append(dict(headers or {}))
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "provider": "gemini",
            "api_key": "k",
            "url": "https://cp.example.invalid/v1/runs/run-wp6/screenshots/s.png",
            "markdown": "![Screenshot](https://cp.example.invalid/v1/runs/run-wp6/screenshots/s.png)",
        }
        return resp

    with patch("httpx.post", side_effect=fake_post):
        fetch_run_llm_key("https://cp.example.invalid", "run-wp6", "cred-wp6")
        sink = EventSink("https://cp.example.invalid", "run-wp6", "cred-wp6")
        sink.emit("test.event", ok=True)
        sink.flush()
        _upload_screenshot_to_cp("s.png", b"\x89PNG\r\n\x1a\n")

    assert len(seen_headers) == 3
    for hdrs in seen_headers:
        assert hdrs.get("X-Instance-ID") == "i-spot-wp6"
