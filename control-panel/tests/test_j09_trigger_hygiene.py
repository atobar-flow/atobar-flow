from fastapi.testclient import TestClient

from control_panel.app import app, get_dispatcher
from control_panel.github import Dispatcher
from control_panel.store import get_store

client = TestClient(app)

def test_j09_ac3_events_endpoint_blocks_state_labels():
    from control_panel.store import MemoryStore
    fake_store = MemoryStore()
    fake_store.create_tenant("owner/repo", "arsenyspb")
    fake_store.update_tenant_status("t-0", "active")
    fake_store.add_token("t-0", "fake_hash", "arsenyspb", "ci")

    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[get_dispatcher] = lambda: Dispatcher()

    # mock token logic since we bypass auth properly via digest or just setup proper token
    def fake_bearer(authorization):
        return "fake_token"
    # Wait, the tenant auth checks token hash against active tokens. Let's just override current_tenant.
    from control_panel.app import current_tenant
    app.dependency_overrides[current_tenant] = lambda: fake_store.tenants["t-0"]

    class FakeDispatcher:
        def dispatch(self, kind, payload):
            self.dispatched = True
        def comment_on_open_issue_by_label(self, *args, **kwargs):
            pass
        def remove_issue_label(self, *args, **kwargs):
            pass
        def create_issue_comment(self, *args, **kwargs):
            pass

    fake_disp = FakeDispatcher()
    fake_disp.dispatched = False
    app.dependency_overrides[get_dispatcher] = lambda: fake_disp

    # Resume label allows unblocking and dispatching
    res = client.post("/v1/events", json={
        "kind": "issues.labeled",
        "issue_number": 1,
        "label": "flow:resume",
        "sender": "owner"
    })
    assert res.status_code == 202
    assert res.json()["dispatched"] is True
    assert fake_disp.dispatched

    fake_disp.dispatched = False

    # In-progress label
    res = client.post("/v1/events", json={
        "kind": "issues.labeled",
        "issue_number": 1,
        "label": "flow:in-progress",
        "sender": "owner"
    })
    assert res.status_code == 202
    assert res.json()["dispatched"] is False
    assert not fake_disp.dispatched

    # Feature label while the resumed run is live: one issue, one session
    res = client.post("/v1/events", json={
        "kind": "issues.labeled",
        "issue_number": 1,
        "label": "flow:feature",
        "sender": "owner"
    })
    assert res.status_code == 202
    assert res.json() == {"dispatched": False, "reason": "a run is already active on this issue"}
    assert not fake_disp.dispatched

    # Feature label on another issue starts its own session
    res = client.post("/v1/events", json={
        "kind": "issues.labeled",
        "issue_number": 2,
        "label": "flow:feature",
        "sender": "owner"
    })
    assert res.status_code == 202
    assert res.json()["dispatched"] is True
    assert fake_disp.dispatched
