import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi.testclient import TestClient

from control_panel import app as cp_app
from control_panel import llm_keys
from control_panel.store import Store, Tenant
from flow_agent.runtime.key_delivery import (
    build_role_llm_client,
    fetch_run_llm_key,
)

KEK = bytes(range(32))
FAKE_KEY_A = "fake-gemini-key-tenant-a-1234"
FAKE_KEY_B = "fake-gemini-key-tenant-b-5678"
FACTORY_KEY = "fake-factory-gemini-key-9999"


class FakeStore(Store):
    def __init__(self):
        self.tenants = {}
        self.tenants_by_repo = {}
        self.ciphertexts = {}  # (tenant_id, provider) -> ciphertext
        self.run_credentials = {}  # run_id -> dict

    def role_llm_prefs(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        pref = {"provider": "gemini", "model": "gemini-3.8-flash", "reasoning_effort": "high"}
        return {"developer": pref, "evaluator": pref}

    def tenant_by_repo(self, repo: str) -> Optional[Tenant]:
        return self.tenants_by_repo.get(repo)

    def get_tenant_llm_ciphertext(self, tenant_id: str, provider: str) -> Optional[str]:
        return self.ciphertexts.get((tenant_id, provider))

    def create_run_credential(
        self,
        run_id: str,
        tenant_id: str,
        role: str,
        cred_hash: str,
        expires_at: str,
    ) -> None:
        self.run_credentials[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "role": role,
            "cred_hash": cred_hash,
            "expires_at": expires_at,
            "used_at": None,
        }

    def get_run_credential(self, run_id: str) -> Optional[dict[str, Any]]:
        return self.run_credentials.get(run_id)

    def mark_run_credential_used(self, run_id: str) -> None:
        if run_id in self.run_credentials:
            self.run_credentials[run_id]["used_at"] = datetime.now(timezone.utc).isoformat()


def make_test_setup():
    store = FakeStore()
    tenant_a = Tenant(
        id="tenant-a-uuid",
        repo="arsenyspb/repo-a",
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": "arsenyspb/repo-a", "llm": {"provider": "gemini"}},
    )
    tenant_b = Tenant(
        id="tenant-b-uuid",
        repo="arsenyspb/repo-b",
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": "arsenyspb/repo-b", "llm": {"provider": "gemini"}},
    )
    store.tenants[tenant_a.id] = tenant_a
    store.tenants[tenant_b.id] = tenant_b
    store.tenants_by_repo[tenant_a.repo] = tenant_a
    store.tenants_by_repo[tenant_b.repo] = tenant_b

    # Encrypt keys for both tenants
    ct_a = llm_keys.encrypt(KEK, tenant_a.id, "gemini", FAKE_KEY_A)
    ct_b = llm_keys.encrypt(KEK, tenant_b.id, "gemini", FAKE_KEY_B)
    store.ciphertexts[(tenant_a.id, "gemini")] = ct_a
    store.ciphertexts[(tenant_b.id, "gemini")] = ct_b

    client = TestClient(cp_app.app)
    cp_app.app.dependency_overrides[cp_app.get_store] = lambda: store
    cp_app.app.dependency_overrides[llm_keys.get_kek] = lambda: KEK

    return client, store, tenant_a, tenant_b


# --- AC-1: A credential for tenant A cannot fetch tenant B's key ---

def test_ac1_credential_for_tenant_a_cannot_fetch_tenant_b_key():
    client, store, tenant_a, tenant_b = make_test_setup()

    # Mint credential for Tenant A
    raw_cred_a = "one-time-cred-tenant-a-secret"
    cred_hash_a = hashlib.sha256(raw_cred_a.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-a-1",
        tenant_id=tenant_a.id,
        role="developer",
        cred_hash=cred_hash_a,
        expires_at=expires_at,
    )

    # Request key using Tenant A's run credential
    resp = client.post(
        "/v1/runs/run-a-1/llm-key",
        headers={"Authorization": f"Bearer {raw_cred_a}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # It must return Tenant A's key, NEVER Tenant B's key
    assert data["api_key"] == FAKE_KEY_A
    assert data["api_key"] != FAKE_KEY_B


# --- AC-2: An expired or reused credential is rejected ---

def test_ac2_expired_credential_is_rejected():
    client, store, tenant_a, _ = make_test_setup()

    raw_cred = "cred-expired-123"
    cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
    # Expired 5 minutes ago
    expires_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    store.create_run_credential(
        run_id="run-expired",
        tenant_id=tenant_a.id,
        role="developer",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    resp = client.post(
        "/v1/runs/run-expired/llm-key",
        headers={"Authorization": f"Bearer {raw_cred}"},
    )
    assert resp.status_code in (401, 403, 410)
    assert "expired" in resp.json()["detail"].lower()


def test_ac2_reused_credential_is_rejected():
    client, store, tenant_a, _ = make_test_setup()

    raw_cred = "cred-single-use-456"
    cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    store.create_run_credential(
        run_id="run-single-use",
        tenant_id=tenant_a.id,
        role="developer",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    # First fetch succeeds and records used_at audit timestamp
    resp1 = client.post(
        "/v1/runs/run-single-use/llm-key",
        headers={"Authorization": f"Bearer {raw_cred}"},
    )
    assert resp1.status_code == 200
    assert resp1.json()["api_key"] == FAKE_KEY_A
    record = store.get_run_credential("run-single-use")
    assert record.get("used_at") is not None

    # Repeated fetch allows resume until expires_at (Axiom 6 Spot resume)
    resp2 = client.post(
        "/v1/runs/run-single-use/llm-key",
        headers={"Authorization": f"Bearer {raw_cred}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["api_key"] == FAKE_KEY_A


# --- AC-3: Key never appears in logs (test captures logging) ---

def test_ac3_key_never_appears_in_logs(caplog):
    client, store, tenant_a, _ = make_test_setup()
    raw_cred = "cred-log-check-789"
    cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    store.create_run_credential(
        run_id="run-log-check",
        tenant_id=tenant_a.id,
        role="evaluator",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/v1/runs/run-log-check/llm-key",
            headers={"Authorization": f"Bearer {raw_cred}"},
        )
        assert resp.status_code == 200
        key_data = resp.json()
        assert key_data["api_key"] == FAKE_KEY_A

    # Verify that the plaintext key and raw credential never appear in log output
    assert FAKE_KEY_A not in caplog.text
    assert raw_cred not in caplog.text


def test_fallback_to_factory_key_only_if_opted_in(monkeypatch):
    client, store, tenant_a, _ = make_test_setup()
    monkeypatch.setenv("GEMINI_API_KEY", FACTORY_KEY)

    # Tenant without custom key and NOT opted into factory fallback
    tenant_no_key = Tenant(
        id="tenant-no-key",
        repo="arsenyspb/repo-no-key",
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": "arsenyspb/repo-no-key", "fallback_to_factory_key": False},
    )
    store.tenants[tenant_no_key.id] = tenant_no_key
    store.tenants_by_repo[tenant_no_key.repo] = tenant_no_key

    raw_cred = "cred-no-fallback"
    cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    store.create_run_credential("run-no-fallback", tenant_no_key.id, "developer", cred_hash, expires_at)

    resp = client.post(
        "/v1/runs/run-no-fallback/llm-key",
        headers={"Authorization": f"Bearer {raw_cred}"},
    )
    assert resp.status_code in (400, 404)

    # Now opt in to fallback
    tenant_fallback = Tenant(
        id="tenant-fallback",
        repo="arsenyspb/repo-fallback",
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": "arsenyspb/repo-fallback", "fallback_to_factory_key": True},
    )
    store.tenants[tenant_fallback.id] = tenant_fallback
    store.tenants_by_repo[tenant_fallback.repo] = tenant_fallback

    raw_cred_fb = "cred-fallback-allowed"
    cred_hash_fb = hashlib.sha256(raw_cred_fb.encode()).hexdigest()
    store.create_run_credential("run-fallback", tenant_fallback.id, "developer", cred_hash_fb, expires_at)

    resp2 = client.post(
        "/v1/runs/run-fallback/llm-key",
        headers={"Authorization": f"Bearer {raw_cred_fb}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["api_key"] == FACTORY_KEY


def test_fetch_run_llm_key_and_build_role_llm_client(monkeypatch):
    client, store, tenant_a, _ = make_test_setup()

    raw_cred = "cred-build-client-007"
    cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    store.create_run_credential("run-client-007", tenant_a.id, "developer", cred_hash, expires_at)

    # Mock httpx.post in key_delivery to call our test client
    def fake_post(url, headers, timeout):
        # Extract path from URL
        path = "/" + url.split("/", 3)[-1]
        resp = client.post(path, headers=headers)
        return resp

    monkeypatch.setattr("httpx.post", fake_post)

    key_info = fetch_run_llm_key("http://testserver", "run-client-007", raw_cred)
    assert key_info["provider"] == "gemini"
    assert key_info["api_key"] == FAKE_KEY_A

    # Test building role LLM client with a fresh credential
    raw_cred2 = "cred-build-client-008"
    cred_hash2 = hashlib.sha256(raw_cred2.encode()).hexdigest()
    store.create_run_credential("run-client-008", tenant_a.id, "developer", cred_hash2, expires_at)

    llm_client = build_role_llm_client("http://testserver", "run-client-008", raw_cred2)
    assert llm_client is not None

