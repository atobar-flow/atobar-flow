import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from control_panel.app import app, current_tenant, get_dispatcher, get_store
from control_panel.client_templates import render_trigger
from control_panel.store import Tenant


class FakeStore:
    def __init__(self):
        self.tenants = {}
        self.tenants_by_repo = {}
        self.observed_keys = set()

    def tenant_by_repo(self, repo):
        return self.tenants_by_repo.get(repo)

    def observed_metric_keys(self, tenant_id):
        return set(self.observed_keys)

    def update_tenant_status(self, tenant_id, status):
        if tenant_id in self.tenants:
            self.tenants[tenant_id].status = status


class FakeDispatcher:
    def __init__(self):
        self.dispatched = []

    def dispatch(self, workflow, payload):
        self.dispatched.append((workflow, payload))

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def make_client(status: str = "active") -> tuple[TestClient, FakeStore, FakeDispatcher]:
    store = FakeStore()
    dispatcher = FakeDispatcher()
    t = Tenant(
        id="t-1",
        repo="arsenyspb/ollama-monitor",
        status=status,
        arbitration_mode="shadow",
        manifest={},
    )
    store.tenants[t.id] = t
    store.tenants_by_repo[t.repo] = t

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    app.dependency_overrides[current_tenant] = lambda: t
    return TestClient(app), store, dispatcher


# --- AC-1: Unit tests per kind ---

def test_ac1_event_pull_request_kinds():
    client, _, disp = make_client(status="active")

    for action in ["opened", "synchronize", "closed"]:
        disp.dispatched.clear()
        payload = {
            "kind": f"pull_request.{action}",
            "pr_number": 42,
            "head_sha": VALID_SHA,
            "base_sha": BASE_SHA,
            "sender": "developer",
            "merged": False,
        }
        res = client.post("/v1/events", json=payload)
        assert res.status_code == 202
        assert res.json() == {"dispatched": True}
        assert len(disp.dispatched) == 1
        name, data = disp.dispatched[0]
        assert name == "flow_event"
        assert data["target_repo"] == "arsenyspb/ollama-monitor"
        assert data["event"]["kind"] == f"pull_request.{action}"
        assert data["event"]["pr_number"] == 42
        assert data["event"]["head_sha"] == VALID_SHA


def test_ac1_event_issue_comment_kind():
    client, _, disp = make_client(status="active")

    payload = {
        "kind": "issue_comment.created",
        "issue_number": 42,
        "comment_body": "```flow-ack\n{}\n```",
        "comment_id": 999,
        "sender": "atobar-flow-agent[bot]",
    }
    res = client.post("/v1/events", json=payload)
    assert res.status_code == 202
    assert res.json() == {"dispatched": True}
    assert len(disp.dispatched) == 1
    name, data = disp.dispatched[0]
    assert name == "flow_event"
    assert data["event"]["kind"] == "issue_comment.created"
    assert data["event"]["comment_id"] == 999


def test_ac1_event_check_run_kind():
    client, _, disp = make_client(status="active")

    payload = {
        "kind": "check_run.completed",
        "check_name": "ci/test",
        "conclusion": "success",
        "head_sha": VALID_SHA,
        "run_id": 12345,
    }
    res = client.post("/v1/events", json=payload)
    assert res.status_code == 202
    assert res.json() == {"dispatched": True}
    assert len(disp.dispatched) == 1
    name, data = disp.dispatched[0]
    assert name == "flow_event"
    assert data["event"]["kind"] == "check_run.completed"
    assert data["event"]["conclusion"] == "success"


# --- AC-2: Onboarding tenants never dispatch flow_event ---

@pytest.mark.parametrize("payload", [
    {
        "kind": "pull_request.opened",
        "pr_number": 1,
        "head_sha": VALID_SHA,
        "base_sha": BASE_SHA,
    },
    {
        "kind": "issue_comment.created",
        "issue_number": 1,
        "comment_body": "```flow-ack\n{}\n```",
    },
    {
        "kind": "check_run.completed",
        "check_name": "ci/test",
        "conclusion": "success",
        "head_sha": VALID_SHA,
    },
])
def test_ac2_onboarding_tenants_never_dispatch_flow_event(payload):
    client, _, disp = make_client(status="onboarding")

    res = client.post("/v1/events", json=payload)
    assert res.status_code == 202
    data = res.json()
    assert data["dispatched"] is False
    assert "active" in data["reason"].lower()
    assert len(disp.dispatched) == 0


# --- AC-3: Generated workflow passes actionlint ---

def test_ac3_generated_workflow_passes_actionlint():
    if not shutil.which("actionlint"):
        pytest.skip("actionlint not found in PATH")

    wf_text = render_trigger(
        slug="arsenyspb__ollama-monitor",
        cp_url="https://cp.example.invalid",
    )
    with tempfile.NamedTemporaryFile(suffix=".yml", mode="w") as f:
        f.write(wf_text)
        f.flush()
        res = subprocess.run(["actionlint", f.name], capture_output=True, text=True)
        assert res.returncode == 0, f"actionlint failed:\n{res.stdout}\n{res.stderr}"
