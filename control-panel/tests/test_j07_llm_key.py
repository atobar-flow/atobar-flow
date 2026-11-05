import hashlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.store import MemoryStore, get_store

client = TestClient(app)

def test_j07_ac3_llm_key_endpoint():
    fake_store = MemoryStore()

    tenant = fake_store.create_tenant("owner/repo", "arsenyspb")

    # Store LLM key for the tenant
    from control_panel.llm_keys import encrypt
    fake_kek = b"0123456789abcdef0123456789abcdef"
    cipher = encrypt(fake_kek, tenant.id, "gemini", "fake-key-123")
    fake_store.put_llm_key(tenant.id, "gemini", {"provider": "gemini", "ciphertext": cipher})

    cred = "atb_run_cred1"
    run_id = "run-1"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    # Needs to match create_run_credential in MemoryStore
    fake_store.run_creds[run_id] = {
        "run_id": run_id,
        "tenant_id": tenant.id,
        "role": "developer",
        "cred_hash": hashlib.sha256(cred.encode("utf-8")).hexdigest(),
        "expires_at": expires
    }

    fake_store.set_role_llm_pref(tenant.id, "developer", "gemini", "gemini-3.8-flash", "high")
    app.dependency_overrides[get_store] = lambda: fake_store

    from control_panel.llm_keys import get_kek
    app.dependency_overrides[get_kek] = lambda: fake_kek

    # Valid call
    resp = client.post(f"/v1/runs/{run_id}/llm-key", headers={"Authorization": f"Bearer {cred}"})
    assert resp.status_code == 200
    assert resp.json()["api_key"] == "fake-key-123"

    # 2nd call -> succeeds until expires_at (Spot resume)
    resp2 = client.post(f"/v1/runs/{run_id}/llm-key", headers={"Authorization": f"Bearer {cred}"})
    assert resp2.status_code == 200
    assert resp2.json()["api_key"] == "fake-key-123"

    # Wrong cred -> 401
    resp3 = client.post(f"/v1/runs/{run_id}/llm-key", headers={"Authorization": "Bearer bad"})
    assert resp3.status_code == 401

    # No cred -> 401
    resp4 = client.post(f"/v1/runs/{run_id}/llm-key")
    assert resp4.status_code == 401

