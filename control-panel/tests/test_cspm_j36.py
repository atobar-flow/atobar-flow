"""CSPM J36 security remediation test suite (#308 AC-1 through AC-5)."""

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from control_panel import app as cp
from control_panel import llm_keys, web
from control_panel.llm_keys import Provider
from control_panel.store import MemoryStore, get_store
from control_panel.tokens import hash_token


class FakeKMSClient:
    """Deterministic in-memory AWS KMS fake enforcing EncryptionContext binding."""

    def __init__(self) -> None:
        self._master_key = secrets.token_bytes(32)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def generate_data_key(
        self,
        *,
        KeyId: str,
        KeySpec: str = "AES_256",
        EncryptionContext: dict[str, str],
    ) -> dict[str, Any]:
        assert KeySpec == "AES_256"
        self.calls.append(("generate_data_key", {"KeyId": KeyId, "EncryptionContext": dict(EncryptionContext)}))
        dek = secrets.token_bytes(32)
        aad = "|".join(f"{k}={v}" for k, v in sorted(EncryptionContext.items())).encode("utf-8")
        nonce = secrets.token_bytes(12)
        wrapped = nonce + AESGCM(self._master_key).encrypt(nonce, dek, aad)
        return {
            "Plaintext": dek,
            "CiphertextBlob": wrapped,
            "KeyId": KeyId,
        }

    def decrypt(
        self,
        *,
        CiphertextBlob: bytes,
        EncryptionContext: dict[str, str],
    ) -> dict[str, Any]:
        self.calls.append(("decrypt", {"EncryptionContext": dict(EncryptionContext)}))
        aad = "|".join(f"{k}={v}" for k, v in sorted(EncryptionContext.items())).encode("utf-8")
        nonce, ct = CiphertextBlob[:12], CiphertextBlob[12:]
        dek = AESGCM(self._master_key).decrypt(nonce, ct, aad)
        return {"Plaintext": dek}


class FakeGitHubTokenDispatcher:
    def __init__(self) -> None:
        self.minted_for: list[str] = []

    def dispatch(self, event_type: str, client_payload: dict[str, Any]) -> None:
        pass

    def get_collaborator_permission(self, repo: str, username: str) -> str | None:
        return "write"

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        return "Issue title\n\nBody"

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        pass

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        pass

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        pass

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        return True

    def comment_on_manifest_pr(self, repo: str, comment: str) -> bool:
        return True

    def mint_repo_token(self, repo: str, role: str | None = None) -> dict[str, str]:
        self.minted_for.append(repo)
        return {
            "token": f"ghs_scoped_{repo.replace('/', '_')}",
            "expires_at": "2099-01-01T00:00:00Z",
        }


RUN_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    (
        "POST",
        "/v1/runs/{run_id}/heartbeat",
        {"status": "Running", "instance_id": "i-claim-1", "target_repo": "evil/spoofed", "issue_id": 1},
    ),
    ("POST", "/v1/runs/{run_id}/llm-key", None),
    ("POST", "/v1/runs/{run_id}/events", {"events": []}),
    (
        "POST",
        "/v1/runs/{run_id}/screenshots",
        {"filename": "test.png", "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode()},
    ),
    ("GET", "/v1/runs/{run_id}/arbitration-data", None),
    (
        "POST",
        "/v1/runs/{run_id}/arbitration-decisions",
        {"pr_number": 1, "head_sha": "a" * 40, "mode": "shadow", "decision": "pass"},
    ),
    ("GET", "/v1/runs/{run_id}/wiki", None),
    (
        "POST",
        "/v1/runs/{run_id}/turn-complete",
        {"comment_body": "done", "issue_number": 1, "instance_id": "i-claim-1"},
    ),
    ("POST", "/v1/runs/{run_id}/github-token", None),
]


@pytest.mark.parametrize("method,path_tpl,body", RUN_ENDPOINTS)
def test_ac1_unified_run_credential_expiry_and_instance_binding(method, path_tpl, body, tmp_path, monkeypatch):
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(tmp_path / "screens"))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "admin")
    store.wikis[tenant.id] = {"purpose": "test"}
    kek = b"1" * 32
    store.put_llm_key(
        tenant.id,
        "gemini",
        {"provider": "gemini", "ciphertext": llm_keys.encrypt(kek, tenant.id, "gemini", "secret-gemini-key")},
    )
    store.set_role_llm_pref(tenant.id, "developer", "gemini", "gemini-3.8-flash", "high")

    disp = FakeGitHubTokenDispatcher()
    cp.app.dependency_overrides[get_store] = lambda: store
    cp.app.dependency_overrides[cp.get_dispatcher] = lambda: disp
    cp.app.dependency_overrides[llm_keys.get_kek] = lambda: kek

    client = TestClient(cp.app)
    try:
        # 1. Active credential setup
        raw_cred = "atb_run_valid_cred"
        cred_hash = hashlib.sha256(raw_cred.encode()).hexdigest()
        future_exp = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        store.create_run_credential("run-valid", tenant.id, "developer", cred_hash, future_exp)

        path = path_tpl.format(run_id="run-valid")

        # Missing token -> 401
        r_missing = client.request(method, path, json=body)
        assert r_missing.status_code == 401

        # Wrong token -> 401
        r_wrong = client.request(method, path, json=body, headers={"Authorization": "Bearer wrong-token"})
        assert r_wrong.status_code == 401

        # Expired credential -> 410 Gone
        past_exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.create_run_credential("run-expired", tenant.id, "developer", cred_hash, past_exp)
        r_exp = client.request(
            method,
            path_tpl.format(run_id="run-expired"),
            json=body,
            headers={"Authorization": f"Bearer {raw_cred}"},
        )
        assert r_exp.status_code == 410

        # First call with instance_id i-claim-1 binds claimed_instance_id
        r_claim = client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {raw_cred}", "X-Instance-ID": "i-claim-1"},
        )
        assert r_claim.status_code in (200, 201)

        # Subsequent call with different instance_id -> 403 Forbidden
        bad_body = dict(body) if isinstance(body, dict) else None
        if bad_body and "instance_id" in bad_body:
            bad_body["instance_id"] = "i-attacker-999"
        r_hijack = client.request(
            method,
            path,
            json=bad_body,
            headers={"Authorization": f"Bearer {raw_cred}", "X-Instance-ID": "i-attacker-999"},
        )
        assert r_hijack.status_code == 403
    finally:
        cp.app.dependency_overrides.clear()


def test_ac1_heartbeat_enforces_authoritative_target_repo():
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "admin")
    raw_cred = "atb_run_hb_cred"
    store.create_run_credential(
        "run-hb",
        tenant.id,
        "developer",
        hashlib.sha256(raw_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    cp.app.dependency_overrides[get_store] = lambda: store
    client = TestClient(cp.app)
    try:
        res = client.post(
            "/v1/runs/run-hb/heartbeat",
            headers={"Authorization": f"Bearer {raw_cred}"},
            json={
                "status": "Running",
                "instance_id": "i-hb-1",
                "target_repo": "attacker/spoofed-repo",
                "issue_id": 42,
            },
        )
        assert res.status_code == 200
        assert store.sessions["i-hb-1"]["target_repo"] == "arsenyspb/ollama-monitor"
    finally:
        cp.app.dependency_overrides.clear()


def test_ac2_private_screenshots_encrypted_on_disk_and_signed_urls(tmp_path, monkeypatch):
    screen_dir = tmp_path / "private-screens"
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(screen_dir))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "admin")
    other_tenant = store.create_tenant("arsenyspb/other-private", "admin")

    raw_cred = "atb_run_screen_cred"
    store.create_run_credential(
        "run-scr-1",
        tenant.id,
        "evaluator",
        hashlib.sha256(raw_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )

    secret = "s" * 40
    cfg = web.OAuthConfig("cid", "csecret", secret, frozenset(), "https://cp.example", "atobar-flow/atobar-flow")
    cp.app.dependency_overrides[get_store] = lambda: store
    cp.app.dependency_overrides[web.get_oauth_config] = lambda: cfg

    client = TestClient(cp.app)
    try:
        png_bytes = b"\x89PNG\r\n\x1a\nconfidential-ui-pixels"
        b64_data = base64.b64encode(png_bytes).decode("ascii")

        r_up = client.post(
            "/v1/runs/run-scr-1/screenshots",
            headers={"Authorization": f"Bearer {raw_cred}"},
            json={"filename": "dashboard.png", "data_base64": b64_data, "caption": "Main view"},
        )
        assert r_up.status_code == 201
        data = r_up.json()
        signed_url = data["url"]
        parsed = urlparse(signed_url)
        qs = parse_qs(parsed.query)
        assert "exp" in qs and "sig" in qs

        # Disk file is encrypted (no PNG magic bytes on disk) and directory is 0700
        disk_file = screen_dir / "run-scr-1" / "dashboard.png"
        assert disk_file.is_file()
        assert b"\x89PNG" not in disk_file.read_bytes()
        assert (screen_dir.stat().st_mode & 0o777) == 0o700

        # run_events stores only metadata, never data_base64
        events = store.run_events_for_run("run-scr-1")
        assert len(events) == 1
        evt_payload = events[0]["payload"]
        assert "data_base64" not in evt_payload
        assert evt_payload["sha256"] == hashlib.sha256(png_bytes).hexdigest()
        assert evt_payload["size_bytes"] == len(png_bytes)

        # Unauthenticated GET without query params -> 401
        r_unauth = client.get("/v1/runs/run-scr-1/screenshots/dashboard.png")
        assert r_unauth.status_code == 401

        # Tampered sig -> 403
        r_tampered = client.get(
            f"/v1/runs/run-scr-1/screenshots/dashboard.png?exp={qs['exp'][0]}&sig={'0' * 64}"
        )
        assert r_tampered.status_code == 403

        # Expired signed URL -> 403
        from control_panel.tokens import sign_screenshot_url

        expired_ts = int(time.time()) - 10
        expired_sig = sign_screenshot_url(tenant.id, "run-scr-1", "dashboard.png", expired_ts)
        r_expired = client.get(
            f"/v1/runs/run-scr-1/screenshots/dashboard.png?exp={expired_ts}&sig={expired_sig}"
        )
        assert r_expired.status_code == 403

        # Valid signed URL -> 200 with strict private headers
        r_valid_sig = client.get(f"{parsed.path}?{parsed.query}")
        assert r_valid_sig.status_code == 200
        assert r_valid_sig.content == png_bytes
        assert r_valid_sig.headers["cache-control"] == "private, no-store, max-age=0"
        assert r_valid_sig.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in r_valid_sig.headers["content-security-policy"]

        # Browser session without access to tenant repo -> 403
        unauth_cookie = web.sign(
            secret,
            {
                "login": "bob",
                "repos": [other_tenant.repo],
                "manage": [],
                "admin": False,
                "csrf": "c1",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
        )
        client.cookies.set(web.SESSION_COOKIE, unauth_cookie)
        assert client.get("/v1/runs/run-scr-1/screenshots/dashboard.png").status_code == 403

        # Browser session with access to tenant repo -> 200
        collab_cookie = web.sign(
            secret,
            {
                "login": "alice",
                "repos": [tenant.repo],
                "manage": [],
                "admin": False,
                "csrf": "c2",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
        )
        client.cookies.set(web.SESSION_COOKIE, collab_cookie)
        r_collab = client.get("/v1/runs/run-scr-1/screenshots/dashboard.png")
        assert r_collab.status_code == 200
        assert r_collab.content == png_bytes
    finally:
        cp.app.dependency_overrides.clear()


def test_ac3_kms_envelope_encryption_and_single_role_delivery():
    fake_kms = FakeKMSClient()
    legacy_kek = b"2" * 32
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "admin")

    # 1. Seal with KMS envelope (kek_version=2)
    sealed = llm_keys.seal_llm_key(
        "sk-ant-kms-secret-9999",
        tenant_id=tenant.id,
        provider="claude",
        kms_client=fake_kms,
        kms_key_id="alias/flow-tenant-llm-cmk",
    )
    assert sealed.kek_version == 2
    assert sealed.encryption_mode == "kms_envelope"
    assert sealed.wrapped_dek is not None
    assert sealed.kms_key_id == "alias/flow-tenant-llm-cmk"
    assert fake_kms.calls[-1] == (
        "generate_data_key",
        {
            "KeyId": "alias/flow-tenant-llm-cmk",
            "EncryptionContext": {"tenant_id": tenant.id, "provider": "claude"},
        },
    )

    # Wrong tenant or provider context fails decryption
    with pytest.raises(Exception):
        llm_keys.open_llm_key(
            sealed,
            tenant_id="wrong-tenant",
            provider="claude",
            kms_client=fake_kms,
        )

    store.put_llm_key(
        tenant.id,
        "claude",
        {
            "provider": "claude",
            "ciphertext": sealed.ciphertext,
            "wrapped_dek": sealed.wrapped_dek,
            "kek_version": sealed.kek_version,
            "kms_key_id": sealed.kms_key_id,
            "encryption_mode": sealed.encryption_mode,
        },
    )

    # 2. Also store a legacy kek_version=1 key for gemini
    legacy_ct = llm_keys.encrypt(legacy_kek, tenant.id, "gemini", "gemini-legacy-secret-1111")
    store.put_llm_key(
        tenant.id,
        "gemini",
        {
            "provider": "gemini",
            "ciphertext": legacy_ct,
            "kek_version": 1,
            "encryption_mode": "local_kek",
        },
    )

    store.set_role_llm_pref(tenant.id, "developer", "claude", "claude-sonnet-4-5", "high")
    store.set_role_llm_pref(tenant.id, "evaluator", "gemini", "gemini-3.8-flash", "medium")

    raw_dev_cred = "atb_run_dev_kms"
    store.create_run_credential(
        "run-dev-kms",
        tenant.id,
        "developer",
        hashlib.sha256(raw_dev_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    raw_eval_cred = "atb_run_eval_legacy"
    store.create_run_credential(
        "run-eval-legacy",
        tenant.id,
        "evaluator",
        hashlib.sha256(raw_eval_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )

    cp.app.dependency_overrides[get_store] = lambda: store
    cp.app.dependency_overrides[llm_keys.get_kek] = lambda: legacy_kek
    cp.app.dependency_overrides[llm_keys.get_kms_client] = lambda: fake_kms

    client = TestClient(cp.app)
    try:
        # Developer run receives ONLY claude key (no multi-provider "keys" fan-out)
        r_dev = client.post(
            "/v1/runs/run-dev-kms/llm-key",
            headers={"Authorization": f"Bearer {raw_dev_cred}"},
        )
        assert r_dev.status_code == 200
        dev_body = r_dev.json()
        assert dev_body == {
            "provider": "claude",
            "api_key": "sk-ant-kms-secret-9999",
            "model": "claude-sonnet-4-5",
            "reasoning_effort": "high",
        }
        assert "keys" not in dev_body

        # Evaluator run decrypts legacy kek_version=1 row seamlessly and receives ONLY gemini key
        r_eval = client.post(
            "/v1/runs/run-eval-legacy/llm-key",
            headers={"Authorization": f"Bearer {raw_eval_cred}"},
        )
        assert r_eval.status_code == 200
        eval_body = r_eval.json()
        assert eval_body == {
            "provider": "gemini",
            "api_key": "gemini-legacy-secret-1111",
            "model": "gemini-3.8-flash",
            "reasoning_effort": "medium",
        }
        assert "keys" not in eval_body

        # Audit log recorded llm_key.delivered events
        audit_events = [e["event_type"] for e in store.security_audit_log]
        assert "llm_key.delivered" in audit_events
    finally:
        cp.app.dependency_overrides.clear()


def test_ac4_github_token_minting_and_rate_limit():
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "admin")
    raw_cred = "atb_run_ghtok_cred"
    store.create_run_credential(
        "run-ghtok-1",
        tenant.id,
        "developer",
        hashlib.sha256(raw_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    disp = FakeGitHubTokenDispatcher()

    class _FreshStorePerRequest:
        # Production get_store() builds a new store object per request; the limiter must not key on it.
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

    cp.app.dependency_overrides[get_store] = lambda: _FreshStorePerRequest()
    cp.app.dependency_overrides[cp.get_dispatcher] = lambda: disp

    client = TestClient(cp.app)
    try:
        # 1st mint succeeds and is scoped to tenant.repo
        r1 = client.post(
            "/v1/runs/run-ghtok-1/github-token",
            headers={"Authorization": f"Bearer {raw_cred}"},
        )
        assert r1.status_code == 200
        assert r1.json()["token"] == "ghs_scoped_arsenyspb_ollama-monitor"
        assert disp.minted_for == ["arsenyspb/ollama-monitor"]
        assert any(e["event_type"] == "github_token.minted" for e in store.security_audit_log)

        # Immediate 2nd mint within 60s rate limit -> 429 Too Many Requests
        r2 = client.post(
            "/v1/runs/run-ghtok-1/github-token",
            headers={"Authorization": f"Bearer {raw_cred}"},
        )
        assert r2.status_code == 429
    finally:
        cp.app.dependency_overrides.clear()


def test_ac4_dispatcher_mint_repo_token_scopes_to_single_repo(monkeypatch):
    from control_panel.github import GitHubAppDispatcher

    disp = GitHubAppDispatcher("12345", "67890", "unused-key", "atobar-flow/atobar-flow")
    monkeypatch.setattr(disp, "_jwt", lambda: "fake-jwt")

    captured_posts: list[tuple[str, dict[str, Any]]] = []

    class FakeResp:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return self._payload

    monkeypatch.setattr(
        "control_panel.github.httpx.get",
        lambda url, **kw: FakeResp(200, {"id": 999, "account": {"login": "arsenyspb"}}),
    )

    def fake_post(url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> FakeResp:
        captured_posts.append((url, dict(json or {})))
        return FakeResp(201, {"token": "ghs_repo_scoped_123", "expires_at": "2099-01-01T00:00:00Z"})

    monkeypatch.setattr("control_panel.github.httpx.post", fake_post)
    res = disp.mint_repo_token("arsenyspb/ollama-monitor")
    assert res == {"token": "ghs_repo_scoped_123", "expires_at": "2099-01-01T00:00:00Z"}
    assert len(captured_posts) == 1
    post_url, post_json = captured_posts[0]
    assert post_url.endswith("/app/installations/999/access_tokens")
    assert post_json["repositories"] == ["ollama-monitor"]
    assert post_json["permissions"]["contents"] == "write"
    assert post_json["permissions"]["pull_requests"] == "write"


def test_ac5_web_csrf_token_required_on_all_post_routes():
    store = MemoryStore()
    store.create_tenant("arsenyspb/ollama-monitor", "alice")
    fake_kms = FakeKMSClient()
    secret = "s" * 40
    cfg = web.OAuthConfig("cid", "csecret", secret, frozenset({"alice"}), "https://cp.example", "atobar-flow/atobar-flow")

    class FakeGH:
        def exchange_code(self, cfg, code):
            return "gho_tok"

        def login(self, token):
            return "alice"

        def repo_permissions(self, token):
            return {
                "arsenyspb/ollama-monitor": {"pull": True, "admin": True},
                "alice/new-repo": {"pull": True, "admin": True},
            }

    providers = {
        "gemini": Provider("gemini", "Google Gemini", "https://example", lambda k: (True, ["gemini-3.8-flash"]))
    }
    cp.app.dependency_overrides[get_store] = lambda: store
    cp.app.dependency_overrides[web.get_oauth_config] = lambda: cfg
    cp.app.dependency_overrides[web.get_github_user] = lambda: FakeGH()
    cp.app.dependency_overrides[web.get_kek] = lambda: b"3" * 32
    cp.app.dependency_overrides[web.get_kms_client] = lambda: fake_kms
    cp.app.dependency_overrides[web.get_providers] = lambda: providers

    client = TestClient(cp.app, base_url="https://testserver")
    try:
        r_login = client.get("/login", follow_redirects=False)
        state = parse_qs(urlparse(r_login.headers["location"]).query)["state"][0]
        client.get(f"/auth/callback?code=ok&state={state}", follow_redirects=False)

        page = client.get("/tenants/arsenyspb/ollama-monitor")
        assert page.status_code == 200
        assert 'name="csrf_token"' in page.text or "name='csrf_token'" in page.text

        payload = web.unsign(secret, client.cookies[web.SESSION_COOKIE])
        assert payload is not None
        csrf_tok = payload["csrf"]

        post_routes = [
            ("/tenants", {"repo": "alice/new-repo"}),
            ("/tenants/arsenyspb/ollama-monitor/llm-keys", {"provider": "gemini", "api_key": "valid-gemini-key-1234"}),
            ("/tenants/arsenyspb/ollama-monitor/arbitration-mode", {"mode": "recommend"}),
            (
                "/tenants/arsenyspb/ollama-monitor/llm-roles/developer",
                {"model": "gemini|gemini-3.8-flash", "reasoning_effort": "high"},
            ),
            ("/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete", {}),
            ("/logout", {}),
        ]

        for route, form_data in post_routes:
            # Missing csrf_token -> 403
            r_no_csrf = client.post(route, data=form_data, follow_redirects=False)
            assert r_no_csrf.status_code == 403, f"Expected 403 without csrf_token on {route}"

            # Invalid csrf_token -> 403
            r_bad_csrf = client.post(route, data={**form_data, "csrf_token": "forged"}, follow_redirects=False)
            assert r_bad_csrf.status_code == 403, f"Expected 403 with forged csrf_token on {route}"

            # Valid csrf_token -> 303
            r_ok = client.post(route, data={**form_data, "csrf_token": csrf_tok}, follow_redirects=False)
            assert r_ok.status_code == 303, f"Expected 303 with valid csrf_token on {route}, got {r_ok.status_code}"
    finally:
        cp.app.dependency_overrides.clear()
