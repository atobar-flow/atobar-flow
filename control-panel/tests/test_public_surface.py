"""Public surface & route authentication allowlist guard (CSPM-08 / #308 WP-5)."""

import json
from typing import Iterator

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from control_panel import app as cp
from control_panel.tokens import hash_token

PUBLIC_ALLOWLIST: set[tuple[str, str]] = {
    ("GET", "/healthz"),
    ("GET", "/onboard.sh"),
    ("GET", "/onboard/flow-trigger.yml"),
    ("GET", "/"),
    ("GET", "/onboarding"),
    ("GET", "/login"),
    ("GET", "/auth/callback"),
}

AUTH_DEPENDENCIES: set[str] = {
    "current_tenant",
    "require_admin",
    "require_run_credential",
    "require_user",
    "current_session",
}


def _iter_api_routes(routes: list) -> Iterator[APIRoute]:
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _iter_api_routes(r.original_router.routes)


def _collect_dep_names(dependant) -> set[str]:
    names: set[str] = set()
    for d in getattr(dependant, "dependencies", []):
        call = getattr(d, "call", None)
        if call is not None and hasattr(call, "__name__"):
            names.add(call.__name__)
        names |= _collect_dep_names(d)
    return names


def test_every_non_allowlisted_route_declares_auth_dependency():
    unprotected: list[tuple[str, str, set[str]]] = []
    for route in _iter_api_routes(cp.app.routes):
        deps = _collect_dep_names(route.dependant)
        for method in route.methods or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, route.path)
            if key in PUBLIC_ALLOWLIST:
                continue
            if not (deps & AUTH_DEPENDENCIES):
                unprotected.append((method, route.path, deps))
    assert unprotected == [], f"Routes missing auth dependency: {unprotected}"


def test_openapi_and_docs_disabled_by_default():
    client = TestClient(cp.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        resp = client.get(path)
        assert resp.status_code == 404, f"Expected 404 on {path}, got {resp.status_code}"


def test_healthz_returns_exact_ok_and_admin_status_requires_admin(tmp_path, monkeypatch):
    deploy_file = tmp_path / "deploy.json"
    deploy_file.write_text(
        json.dumps({"status": "ok", "commit": "deadbeef" * 5, "deployed_at": "2026-10-18T00:00:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEPLOY_JSON_PATH", str(deploy_file))

    admin_token = "super-secret-admin-token"
    cp.app.dependency_overrides[cp.get_admin_digest] = lambda: hash_token(admin_token)
    client = TestClient(cp.app)
    try:
        # /healthz never leaks commit SHA or deploy timestamp
        r_health = client.get("/healthz")
        assert r_health.status_code == 200
        assert r_health.json() == {"status": "ok"}

        # /v1/admin/status requires require_admin
        assert client.get("/v1/admin/status").status_code == 401
        assert client.get("/v1/admin/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

        r_admin = client.get("/v1/admin/status", headers={"Authorization": f"Bearer {admin_token}"})
        assert r_admin.status_code == 200
        assert r_admin.json()["commit"] == "deadbeef" * 5
    finally:
        cp.app.dependency_overrides.clear()
