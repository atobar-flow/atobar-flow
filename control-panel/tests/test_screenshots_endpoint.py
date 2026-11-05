import base64
import hashlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.store import MemoryStore, get_store

client = TestClient(app)


def test_upload_and_serve_run_screenshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(tmp_path / "screens"))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-screens", "admin")

    run_cred = "cred-screen-secret"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-screen-1",
        tenant_id=tenant.id,
        role="developer",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    app.dependency_overrides[get_store] = lambda: store

    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
    b64_data = base64.b64encode(png_bytes).decode("ascii")

    # 1. Unauthenticated upload rejected
    r_unauth = client.post(
        "/v1/runs/run-screen-1/screenshots",
        json={"filename": "layout-compact.png", "data_base64": b64_data},
    )
    assert r_unauth.status_code == 401

    # 2. Authenticated upload succeeds and returns signed capability url + markdown
    r_up = client.post(
        "/v1/runs/run-screen-1/screenshots",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={
            "filename": "layout-compact.png",
            "data_base64": b64_data,
            "caption": "Compact layout view",
        },
    )
    assert r_up.status_code in (200, 201)
    body = r_up.json()
    assert body["filename"] == "layout-compact.png"
    assert "/v1/runs/run-screen-1/screenshots/layout-compact.png?exp=" in body["url"]
    assert "&sig=" in body["url"]
    assert "![Compact layout view](" in body["markdown"]

    # 3. Unauthenticated GET without signature is rejected (401); signed URL serves PNG
    assert client.get("/v1/runs/run-screen-1/screenshots/layout-compact.png").status_code == 401
    signed_path = "/" + body["url"].split("/", 3)[-1]
    r_get = client.get(signed_path)
    assert r_get.status_code == 200
    assert r_get.headers["content-type"] == "image/png"
    assert r_get.headers["cache-control"] == "private, no-store, max-age=0"
    assert r_get.content == png_bytes


def test_j42_wp5_signing_secrets_fail_closed(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    from control_panel import tokens, web

    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(tmp_path / "screens"))
    monkeypatch.delenv("CP_ARTIFACT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CP_SESSION_SECRET", raising=False)

    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-screens-503", "admin")
    run_cred = "cred-screen-503"
    store.create_run_credential(
        run_id="run-screen-503",
        tenant_id=tenant.id,
        role="developer",
        cred_hash=hashlib.sha256(run_cred.encode("utf-8")).hexdigest(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    app.dependency_overrides[get_store] = lambda: store

    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
    b64_data = base64.b64encode(png_bytes).decode("ascii")

    # 1. Screenshot upload fails closed with 503 when no signing secret is configured
    r_up_503 = client.post(
        "/v1/runs/run-screen-503/screenshots",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={"filename": "layout.png", "data_base64": b64_data},
    )
    assert r_up_503.status_code == 503
    assert "screenshot signing not configured" in r_up_503.json()["detail"]

    # 2. Screenshot verify route fails closed with 503 when no signing secret is configured
    future_exp = int(datetime.now(timezone.utc).timestamp()) + 300
    r_get_503 = client.get(f"/v1/runs/run-screen-503/screenshots/layout.png?exp={future_exp}&sig={'a' * 64}")
    assert r_get_503.status_code == 503
    assert "screenshot signing not configured" in r_get_503.json()["detail"]

    # 3. Direct helper calls raise RuntimeError when secret is missing or short
    with pytest.raises(RuntimeError, match="screenshot signing not configured"):
        tokens._artifact_secret()
    with pytest.raises(RuntimeError, match="CP_SESSION_SECRET must be at least 32 characters"):
        web._secret()
    monkeypatch.setenv("CP_SESSION_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="CP_SESSION_SECRET must be at least 32 characters"):
        web.csrf_token(web.Session(login="alice", repos=frozenset(), admin=False))
    with pytest.raises(HTTPException) as exc_info:
        web.verify_csrf(web.Session(login="alice", repos=frozenset(), admin=False), "tok")
    assert exc_info.value.status_code == 503


def test_screenshot_persistence_volume_and_fail_closed_on_disk_error(tmp_path, monkeypatch):
    """#357: Screenshots must persist on a named Docker volume (not /tmp tmpfs) and fail closed on write errors."""
    import pathlib
    import yaml

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    compose_data = yaml.safe_load((repo_root / "infra" / "control-panel" / "docker-compose.yml").read_text())
    api_svc = compose_data["services"]["api"]
    api_env = api_svc.get("environment") or []
    assert any("CP_SCREENSHOT_DIR=" in e and "/var/lib/atobar/screenshots" in e for e in api_env)
    api_vols = api_svc.get("volumes") or []
    assert "screenshots_data:/var/lib/atobar/screenshots" in api_vols
    assert "screenshots_data" in (compose_data.get("volumes") or {})

    dockerfile_text = (repo_root / "control-panel" / "Dockerfile").read_text()
    assert "/var/lib/atobar/screenshots" in dockerfile_text

    # Point CP_SCREENSHOT_DIR at a regular file so mkdir/write inside it raises OSError -> must fail closed (503)
    broken_screen_dir = tmp_path / "not-a-dir"
    broken_screen_dir.write_text("file-blocking-mkdir")
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(broken_screen_dir))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")

    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-screens-disk-err", "admin")
    run_cred = "cred-screen-disk-err"
    store.create_run_credential(
        run_id="run-screen-disk-err",
        tenant_id=tenant.id,
        role="developer",
        cred_hash=hashlib.sha256(run_cred.encode("utf-8")).hexdigest(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    app.dependency_overrides[get_store] = lambda: store
    try:
        png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
        b64_data = base64.b64encode(png_bytes).decode("ascii")
        r_err = client.post(
            "/v1/runs/run-screen-disk-err/screenshots",
            headers={"Authorization": f"Bearer {run_cred}"},
            json={"filename": "layout.png", "data_base64": b64_data},
        )
        assert r_err.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_j49_upload_screenshot_persists_provenance_metadata_in_run_events(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(tmp_path / "screens"))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-screens-prov", "admin")

    run_cred = "cred-screen-prov"
    store.create_run_credential(
        run_id="run-screen-prov-1",
        tenant_id=tenant.id,
        role="evaluator",
        cred_hash=hashlib.sha256(run_cred.encode("utf-8")).hexdigest(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    app.dependency_overrides[get_store] = lambda: store
    try:
        png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
        b64_data = base64.b64encode(png_bytes).decode("ascii")
        r_up = client.post(
            "/v1/runs/run-screen-prov-1/screenshots",
            headers={"Authorization": f"Bearer {run_cred}"},
            json={
                "filename": "tui-burst.png",
                "data_base64": b64_data,
                "caption": "UI Screenshot",
                "metadata": {
                    "data_mode": "synthetic",
                    "data_source": "/tmp/eval_trace.csv (idle -> step burst -> spike)",
                    "constraints": "t4g.large Spot VM (ARM64 CPU-only, 8GB RAM, no GPU)",
                    "scenario": "0-TPS idle period and step burst",
                    "resolution": "1920x1080",
                    "x11_windows": ['0x600003 "xterm": ("xterm" "XTerm") 1600x900+0+0'],
                    "active_processes": ["2048 3 python src/monitor_tui.py --csv /tmp/eval_trace.csv --api-key sk-secret123"],
                    "has_active_window": True,
                    "captured_at": "2026-11-01T12:00:00Z",
                },
            },
        )
        assert r_up.status_code == 201
        events = store.run_events_for_run("run-screen-prov-1")
        screen_evts = [e for e in events if e.get("kind") == "screenshot"]
        assert len(screen_evts) == 1
        payload = screen_evts[0]["payload"]
        assert payload["data_mode"] == "synthetic"
        assert payload["data_source"] == "/tmp/eval_trace.csv (idle -> step burst -> spike)"
        assert payload["constraints"] == "t4g.large Spot VM (ARM64 CPU-only, 8GB RAM, no GPU)"
        assert payload["scenario"] == "0-TPS idle period and step burst"
        assert payload["resolution"] == "1920x1080"
        assert payload["x11_windows"] == ['0x600003 "xterm": ("xterm" "XTerm") 1600x900+0+0']
        assert payload["active_processes"] == ["2048 3 python src/monitor_tui.py --csv /tmp/eval_trace.csv [REDACTED]"]
        assert payload["has_active_window"] is True
    finally:
        app.dependency_overrides.clear()


