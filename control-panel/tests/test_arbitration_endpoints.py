import hashlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.store import MemoryStore, get_store

client = TestClient(app)


def test_arbitration_data_endpoint_auth_and_shape():
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-arb", "admin")

    run_cred = "cred-arb-data-secret"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-arb-1",
        tenant_id=tenant.id,
        role="arbitrator",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    store.record_observations([
        {
            "tenant_id": tenant.id,
            "key": "test_pass_rate",
            "value": 1.0,
            "commit_sha": "a" * 40,
            "pr": 42,
            "workflow_path": ".github/workflows/ci.yml",
            "run_id": "gh-run-101",
        }
    ])

    app.dependency_overrides[get_store] = lambda: store

    # 1. 401 on missing / bad auth
    resp_no_auth = client.get("/v1/runs/run-arb-1/arbitration-data")
    assert resp_no_auth.status_code == 401

    resp_bad_auth = client.get(
        "/v1/runs/run-arb-1/arbitration-data",
        headers={"Authorization": "Bearer wrong-cred"},
    )
    assert resp_bad_auth.status_code == 401

    # 2. 200 on valid auth, correct shape
    resp_ok = client.get(
        "/v1/runs/run-arb-1/arbitration-data",
        headers={"Authorization": f"Bearer {run_cred}"},
    )
    assert resp_ok.status_code == 200
    data = resp_ok.json()
    assert data["tenant_id"] == tenant.id
    assert len(data["recent_observations"]) == 1
    obs = data["recent_observations"][0]
    assert obs["key"] == "test_pass_rate"
    assert obs["value"] == 1.0
    assert obs["commit_sha"] == "a" * 40
    assert obs["pr_number"] == 42
    assert obs["workflow_path"] == ".github/workflows/ci.yml"
    assert "run_id" in obs


def test_arbitration_decisions_endpoint_validation_and_persistence():
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-arb", "admin")

    run_cred = "cred-arb-decision-secret"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-arb-2",
        tenant_id=tenant.id,
        role="arbitrator",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    app.dependency_overrides[get_store] = lambda: store

    valid_payload = {
        "pr_number": 42,
        "head_sha": "b" * 40,
        "mode": "shadow",
        "decision": "pass",
        "reasons": "All required metrics met target thresholds",
    }

    # 1. 401 on bad auth
    resp_bad = client.post(
        "/v1/runs/run-arb-2/arbitration-decisions",
        headers={"Authorization": "Bearer bad"},
        json=valid_payload,
    )
    assert resp_bad.status_code == 401

    # 2. 422 on invalid head_sha (not 40-hex)
    resp_invalid_sha = client.post(
        "/v1/runs/run-arb-2/arbitration-decisions",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={**valid_payload, "head_sha": "not-a-valid-40-hex-sha"},
    )
    assert resp_invalid_sha.status_code == 422

    # 3. 422 on invalid mode enum
    resp_invalid_mode = client.post(
        "/v1/runs/run-arb-2/arbitration-decisions",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={**valid_payload, "mode": "invalid_mode"},
    )
    assert resp_invalid_mode.status_code == 422

    # 4. Valid post persists with tenant resolved from credential (ignoring any tenant in body)
    resp_ok = client.post(
        "/v1/runs/run-arb-2/arbitration-decisions",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={**valid_payload, "tenant_id": "malicious-fake-tenant"},
    )
    assert resp_ok.status_code == 200

    # Verify persistence via Store method
    assert hasattr(store, "arbitration_decisions")
    assert len(store.arbitration_decisions) == 1
    saved = store.arbitration_decisions[0]
    assert saved["tenant_id"] == tenant.id  # Resolved from credential!
    assert saved["pr_number"] == 42
    assert saved["head_sha"] == "b" * 40
    assert saved["mode"] == "shadow"
    assert saved["decision"] == "pass"
    assert saved["reasons"] == "All required metrics met target thresholds"


def test_arbitration_data_always_includes_default_branch_baseline_when_pr_observations_exceed_50():
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-arb-base", "admin")

    run_cred = "cred-arb-baseline-secret"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-arb-base",
        tenant_id=tenant.id,
        role="arbitrator",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    # Record 1 default-branch baseline observation first (pr_number=None)
    store.record_observations([
        {
            "tenant_id": tenant.id,
            "key": "dependency_count",
            "value": 9.0,
            "commit_sha": "0" * 40,
            "pr_number": None,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "run_id": 1,
        }
    ])

    # Record 60 PR observations after it
    store.record_observations([
        {
            "tenant_id": tenant.id,
            "key": "dependency_count",
            "value": 9.0,
            "commit_sha": f"{i:040x}",
            "pr_number": 44,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "run_id": i + 10,
        }
        for i in range(1, 61)
    ])

    app.dependency_overrides[get_store] = lambda: store
    resp = client.get(
        "/v1/runs/run-arb-base/arbitration-data",
        headers={"Authorization": f"Bearer {run_cred}"},
    )
    assert resp.status_code == 200
    obs_list = resp.json()["recent_observations"]
    baseline_obs = [o for o in obs_list if o.get("pr_number") is None]
    assert len(baseline_obs) == 1
    assert baseline_obs[0]["commit_sha"] == "0" * 40
    assert baseline_obs[0]["value"] == 9.0

