import time
from typing import Any, Optional
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from control_panel.app import app, get_store
from control_panel.oidc import verify_github_oidc_token
from control_panel.store import MemoryStore, Tenant

# --- RSA Key Setup for Tests ---

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()


class FakeJWKClient:
    def __init__(self, public_key: Any) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        mock_key = MagicMock()
        mock_key.key = self.public_key
        return mock_key


def _issue_token(
    payload_overrides: Optional[dict[str, Any]] = None,
    private_key: Any = _PRIVATE_KEY,
    headers: Optional[dict[str, Any]] = None,
) -> str:
    now = int(time.time())
    payload = {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": "atobar-flow",
        "sub": "repo:owner/test-repo:ref:refs/heads/main",
        "repository": "owner/test-repo",
        "repository_id": "123456",
        "repository_owner": "owner",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return jwt.encode(payload, private_key, algorithm="RS256", headers=headers or {"kid": "key-1"})


# --- AC-1: Forged / Expired / Wrong-Audience Tokens Rejected ---

def test_ac1_valid_oidc_token_succeeds():
    token = _issue_token()
    jwk_client = FakeJWKClient(_PUBLIC_KEY)
    claims = verify_github_oidc_token(token, jwk_client=jwk_client)
    assert claims["repository_id"] == "123456"
    assert claims["repository"] == "owner/test-repo"


def test_ac1_forged_signature_token_rejected():
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged_token = _issue_token(private_key=other_key)
    jwk_client = FakeJWKClient(_PUBLIC_KEY)

    with pytest.raises(Exception, match="Signature verification failed|Invalid OIDC token"):
        verify_github_oidc_token(forged_token, jwk_client=jwk_client)


def test_ac1_expired_token_rejected():
    now = int(time.time())
    expired_token = _issue_token({"iat": now - 7200, "nbf": now - 7200, "exp": now - 3600})
    jwk_client = FakeJWKClient(_PUBLIC_KEY)

    with pytest.raises(Exception, match="Signature has expired|expired|Invalid OIDC token"):
        verify_github_oidc_token(expired_token, jwk_client=jwk_client)


def test_ac1_wrong_audience_rejected():
    wrong_aud_token = _issue_token({"aud": "wrong-audience"})
    jwk_client = FakeJWKClient(_PUBLIC_KEY)

    with pytest.raises(Exception, match="Audience|wrong-audience|Invalid OIDC token"):
        verify_github_oidc_token(wrong_aud_token, jwk_client=jwk_client)


# --- AC-2: Repository rename keeps working via repository_id ---

def test_ac2_repository_rename_resolves_via_repository_id(monkeypatch):
    store = MemoryStore()
    # Tenant was renamed from owner/old-repo to owner/new-repo, but repository_id is identical: 999888
    tenant = Tenant(
        id="t-rename",
        repo="owner/new-repo",
        status="active",
        arbitration_mode="shadow",
        manifest={"repository_id": "999888"},
        repository_id="999888",
    )
    store.tenants["t-rename"] = tenant
    store.metric_defs["t-rename"] = {"coverage"}

    from control_panel.app import get_dispatcher, get_jwk_client

    app.dependency_overrides.clear()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: MagicMock()
    jwk_client = FakeJWKClient(_PUBLIC_KEY)
    app.dependency_overrides[get_jwk_client] = lambda: jwk_client
    try:
        # Token carries old repo name in claims, but matches repository_id
        old_repo_token = _issue_token({
            "repository": "owner/old-repo",
            "repository_id": "999888",
        })

        client = TestClient(app)
        # Make a call to POST /v1/metrics using OIDC Bearer token
        resp = client.post(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {old_repo_token}"},
            json={
                "commit_sha": "0123456789abcdef0123456789abcdef01234567",
                "workflow_path": ".github/workflows/ci.yml",
                "values": {"coverage": 0.95},
            },
        )
        # Should authenticate successfully (not 401)
        assert resp.status_code == 201
        data = resp.json()
        assert data["recorded"] == 1
    finally:
        app.dependency_overrides.clear()
