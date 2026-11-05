import hashlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.store import MemoryStore, get_store

client = TestClient(app)


def test_events_endpoint_auth_batch_limit_and_dedup():
    store = MemoryStore()
    tenant = store.create_tenant("test-owner/test-events", "admin")

    run_cred = "cred-events-secret"
    cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    store.create_run_credential(
        run_id="run-evt-1",
        tenant_id=tenant.id,
        role="developer",
        cred_hash=cred_hash,
        expires_at=expires_at,
    )

    app.dependency_overrides[get_store] = lambda: store

    # 1. 401 on missing or bad cred
    resp_no_auth = client.post("/v1/runs/run-evt-1/events", json={"events": []})
    assert resp_no_auth.status_code == 401

    resp_bad_auth = client.post(
        "/v1/runs/run-evt-1/events",
        headers={"Authorization": "Bearer wrong-token"},
        json={"events": []},
    )
    assert resp_bad_auth.status_code == 401

    # 2. >500 events batch -> 413 Payload Too Large
    big_batch = [
        {"seq": i, "kind": "test.event", "payload": {"idx": i}}
        for i in range(501)
    ]
    resp_too_large = client.post(
        "/v1/runs/run-evt-1/events",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={"events": big_batch},
    )
    assert resp_too_large.status_code == 413

    # 3. Valid batch inserts rows
    event1 = {
        "seq": 1000,
        "kind": "lifecycle.start",
        "ts": "2026-10-11T12:00:00Z",
        "payload": {"role": "developer"},
    }
    event2 = {
        "seq": 2000,
        "kind": "model_call",
        "ts": "2026-10-11T12:00:01Z",
        "payload": {"tokens": 150},
    }
    resp_valid = client.post(
        "/v1/runs/run-evt-1/events",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={"events": [event1, event2]},
    )
    assert resp_valid.status_code in (200, 201)

    # Verify rows in store
    events = store.run_events_for_run("run-evt-1")
    assert len(events) == 2
    assert events[0]["seq"] == 1000
    assert events[0]["kind"] == "lifecycle.start"
    assert events[1]["seq"] == 2000

    # 4. Re-POST of same seq dedups (on_conflict do_nothing)
    resp_dedup = client.post(
        "/v1/runs/run-evt-1/events",
        headers={"Authorization": f"Bearer {run_cred}"},
        json={"events": [event1]},
    )
    assert resp_dedup.status_code in (200, 201)
    events_after = store.run_events_for_run("run-evt-1")
    assert len(events_after) == 2  # No duplicate row inserted
