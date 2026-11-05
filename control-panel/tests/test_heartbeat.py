import hashlib

import pytest
from fastapi.testclient import TestClient
from test_app import FakeDispatcher, FakeStore

from control_panel import app as cp


@pytest.fixture
def env():
    store, disp = FakeStore(), FakeDispatcher()
    cp.app.dependency_overrides[cp.get_store] = lambda: store
    cp.app.dependency_overrides[cp.get_dispatcher] = lambda: disp
    yield TestClient(cp.app), store, disp
    cp.app.dependency_overrides.clear()


def test_heartbeat_payload_fields(env):
    client, store, _ = env
    run_id = "run-test-hb"
    raw_cred = "cred-secret-123"
    cred_hash = hashlib.sha256(raw_cred.encode("utf-8")).hexdigest()

    # Create run credential in store
    store.create_run_credential(run_id, "t-a", "developer", cred_hash, "2099-01-01T00:00:00Z")

    payload = {
        "status": "Running",
        "current_todo": "Write tests",
        "last_tool": "read_file",
        "cost_estimate": 0.42,
        "instance_id": "i-test123",
        "target_repo": "arsenyspb/ollama-monitor",
        "issue_id": 42,
        "agent_runtime": "nanobot",
        "iterations": 3,
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cached_input_tokens": 50,
        },
        "halt_reason": "provider_quota",
        "halt_detail": "quota exhausted",
    }

    res = client.post(
        f"/v1/runs/{run_id}/heartbeat",
        json=payload,
        headers={"Authorization": f"Bearer {raw_cred}"},
    )
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_record_loop_round_usage_detail(env):
    _, store, _ = env
    row = {
        "tenant_id": "t-a",
        "pr_number": 42,
        "round": 1,
        "head_sha": "a" * 40,
        "role": "developer",
        "tokens": 1000,
        "cost_usd": 0.05,
        "usage_detail": {
            "cached_input_tokens": 200,
            "cache_write_tokens": 50,
            "total_tokens": 1000,
        },
    }
    store.record_loop_round(row)
    rounds = store.loop_rounds_for_pr("t-a", 42)
    assert len(rounds) == 1
    assert rounds[0]["usage_detail"]["cached_input_tokens"] == 200
