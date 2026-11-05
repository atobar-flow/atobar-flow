import hashlib

from fastapi.testclient import TestClient

from control_panel.app import app

client = TestClient(app)

def test_j08_ac2_heartbeat_upserts_session():
    # We need a fake store dependency
    from control_panel.store import MemoryStore, Tenant, get_store
    fake_store = MemoryStore()
    fake_store.tenants["t-1"] = Tenant("t-1", "owner/repo", "active", "shadow", {})
    fake_store.run_creds["run-123"] = {
        "tenant_id": "t-1",
        "run_id": "run-123",
        "cred_hash": hashlib.sha256(b"fake-cred").hexdigest(),
        "role": "evaluator",
        "expires_at": "2099-01-01T00:00:00Z" # valid
    }
    app.dependency_overrides[get_store] = lambda: fake_store
    try:
        response = client.post(
            "/v1/runs/run-123/heartbeat",
            headers={"Authorization": "Bearer fake-cred"},
            json={
                "status": "Running",
                "current_todo": "evaluating",
                "instance_id": "i-123",
                "target_repo": "owner/repo",
                "issue_id": 1,
            },
        )
        assert response.status_code == 200
        assert fake_store.sessions["i-123"]["role"] == "evaluator"
        assert fake_store.sessions["i-123"]["status"] == "Running"
        assert fake_store.sessions["i-123"]["tenant_id"] == "t-1"
    finally:
        app.dependency_overrides.clear()

def test_j08_ac2_heartbeat_rejects_invalid_cred():
    from control_panel.store import MemoryStore, get_store
    fake_store = MemoryStore()
    fake_store.run_creds["run-123"] = {
        "cred_hash": hashlib.sha256(b"fake-cred").hexdigest(),
    }
    app.dependency_overrides[get_store] = lambda: fake_store

    response = client.post(
        "/v1/runs/run-123/heartbeat",
        headers={"Authorization": "Bearer bad-cred"},
        json={
            "status": "Running",
            "instance_id": "i-123",
            "target_repo": "owner/repo",
            "issue_id": 1,
        }
    )
    assert response.status_code == 401
