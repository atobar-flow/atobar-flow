"""Heartbeat handler must not repeat store round-trips: every read crosses a WAN link."""

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flow_engine import api as fe_api
from flow_engine.store import MemoryStore

_COUNTED = (
    "get_run_credential",
    "get_session_by_run",
    "claim_run_credential_instance",
    "get_session_by_instance",
    "update_session",
    "upsert_session",
    "tenant_by_id",
)


class CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if name in _COUNTED:
            calls = super().__getattribute__("calls")

            def counted(*args: Any, **kwargs: Any) -> Any:
                calls[name] += 1
                return attr(*args, **kwargs)

            return counted
        return attr


@pytest.fixture
def fe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FE_INTERNAL_TOKEN", "tok")
    store = CountingStore()
    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    try:
        yield TestClient(fe_api.app), store, {"Authorization": "Bearer tok"}
    finally:
        fe_api.app.dependency_overrides.clear()


def _seed_run(store: CountingStore, run_id: str, *, with_credential: bool = True) -> Any:
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    if with_credential:
        store.create_run_credential(
            run_id, tenant.id, "developer", hashlib.sha256(b"c").hexdigest(),
            (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        )
    store.upsert_session({
        "instance_id": f"pending-{run_id}", "run_id": run_id, "tenant_id": tenant.id,
        "role": "developer", "target_repo": tenant.repo, "issue_id": 33, "status": "pending",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    })
    store.calls.clear()
    return tenant


def _beat(client: TestClient, headers: dict[str, str], run_id: str, instance_id: str) -> Any:
    return client.post(
        f"/internal/v1/runs/{run_id}/heartbeat", headers=headers,
        json={"status": "Running", "instance_id": instance_id, "issue_id": 33},
    )


def test_first_heartbeat_claims_and_dispatches_with_one_credential_read(fe):
    client, store, headers = fe
    _seed_run(store, "run-a")

    assert _beat(client, headers, "run-a", "i-1").status_code == 200

    assert store.calls["get_run_credential"] == 1
    assert store.calls["get_session_by_run"] == 0
    assert store.calls["claim_run_credential_instance"] == 1
    assert store.calls["update_session"] == 1
    assert store.sessions["pending-run-a"]["status"] == "dispatched"
    assert store.sessions["i-1"]["status"] == "Running"


def test_steady_state_heartbeat_skips_claim_and_pending_writes(fe):
    client, store, headers = fe
    _seed_run(store, "run-b")
    assert _beat(client, headers, "run-b", "i-1").status_code == 200
    store.calls.clear()

    assert _beat(client, headers, "run-b", "i-1").status_code == 200

    assert store.calls["get_run_credential"] == 1
    assert store.calls["claim_run_credential_instance"] == 0
    assert store.calls["update_session"] == 0
    assert store.calls["upsert_session"] == 1
    assert store.sessions["pending-run-b"]["status"] == "dispatched"


def test_heartbeat_from_second_instance_is_rejected_without_claim_write(fe):
    client, store, headers = fe
    _seed_run(store, "run-c")
    assert _beat(client, headers, "run-c", "i-1").status_code == 200
    store.calls.clear()

    r = _beat(client, headers, "run-c", "i-intruder")

    assert r.status_code == 403
    assert store.calls["claim_run_credential_instance"] == 0
    assert "i-intruder" not in store.sessions


def test_heartbeat_falls_back_to_session_row_when_credential_row_is_gone(fe):
    client, store, headers = fe
    _seed_run(store, "run-d", with_credential=False)

    assert _beat(client, headers, "run-d", "i-1").status_code == 200

    assert store.calls["get_session_by_run"] == 1
    assert store.calls["claim_run_credential_instance"] == 0
    assert store.sessions["i-1"]["status"] == "Running"


def test_empty_instance_id_never_bypasses_the_claim(fe):
    client, store, headers = fe
    _seed_run(store, "run-e")

    r = _beat(client, headers, "run-e", "")

    assert r.status_code == 403
    assert not store.get_run_credential("run-e").get("claimed_instance_id")
