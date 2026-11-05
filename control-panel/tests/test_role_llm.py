import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.llm_keys import encrypt, get_kek
from control_panel.store import MemoryStore, get_store
from flow_agent.agents.nanobot_config import render_nanobot_run_config

KEK = b"0123456789abcdef0123456789abcdef"


def _setup(role: str) -> tuple[TestClient, MemoryStore, str, str]:
    store = MemoryStore()
    tenant = store.create_tenant("owner/repo", "arsenyspb")
    store.put_llm_key(tenant.id, "gemini", {"provider": "gemini", "ciphertext": encrypt(KEK, tenant.id, "gemini", "k-123")})
    cred, run_id = "atb_run_role", f"run-{role}"
    store.run_creds[run_id] = {
        "run_id": run_id,
        "tenant_id": tenant.id,
        "role": role,
        "cred_hash": hashlib.sha256(cred.encode()).hexdigest(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_kek] = lambda: KEK
    return TestClient(app), store, tenant.id, run_id


def _fetch(client: TestClient, run_id: str):
    return client.post(f"/v1/runs/{run_id}/llm-key", headers={"Authorization": "Bearer atb_run_role"})


def test_unconfigured_role_is_refused():
    client, _, _, run_id = _setup("evaluator")
    try:
        r = _fetch(client, run_id)
        assert r.status_code == 409 and "Control Panel" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_each_role_gets_its_own_model_and_effort():
    client, store, tid, run_id = _setup("evaluator")
    store.set_role_llm_pref(tid, "developer", "gemini", "gemini-3.8-flash", "low")
    store.set_role_llm_pref(tid, "evaluator", "gemini", "gemini-3.1-pro-preview", "high")
    try:
        body = _fetch(client, run_id).json()
        assert body["model"] == "gemini-3.1-pro-preview" and body["reasoning_effort"] == "high"
    finally:
        app.dependency_overrides.clear()


def test_nanobot_preset_carries_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_ROLE_REASONING_EFFORT", "medium")
    cfg, _ = render_nanobot_run_config(
        run_id="r1", role="developer", tenant_provider="gemini", role_model="gemini-3.8-flash",
        system_prompt="x", base_dir=str(tmp_path), workspace_dir=str(tmp_path / "ws"),
    )
    preset = json.loads(cfg.read_text())["modelPresets"]["developer"]
    assert preset == {"model": "gemini-3.8-flash", "provider": "gemini", "reasoningEffort": "medium"}
