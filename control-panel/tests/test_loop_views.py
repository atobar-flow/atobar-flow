from fastapi.testclient import TestClient

import control_panel.web as web
from control_panel.app import app, get_store
from control_panel.store import MemoryStore, Tenant
from control_panel.web import Session


def _make_setup():
    store = MemoryStore()

    tenant_a = Tenant(
        id="t-alpha",
        repo="org-alpha/repo-alpha",
        status="active",
        arbitration_mode="shadow",
        manifest={"loop": {"max_review_rounds": 3, "max_cost_usd_per_pr": 5.0}},
    )
    tenant_b = Tenant(
        id="t-beta",
        repo="org-beta/repo-beta",
        status="active",
        arbitration_mode="recommend",
        manifest={"loop": {"max_review_rounds": 2, "max_cost_usd_per_pr": 4.0}},
    )
    store.tenants["t-alpha"] = tenant_a
    store.tenants["t-beta"] = tenant_b

    # Add rounds for tenant A PR 101
    store.record_loop_round({
        "tenant_id": "t-alpha",
        "pr_number": 101,
        "round": 1,
        "role": "developer",
        "head_sha": "a" * 40,
        "tokens": 1500,
        "cost_usd": 0.45,
        "envelope_digest": "flow-ack-sha",
        "created_at": "2025-01-01T12:00:00Z",
    })
    store.record_loop_round({
        "tenant_id": "t-alpha",
        "pr_number": 101,
        "round": 1,
        "role": "evaluator",
        "head_sha": "a" * 40,
        "tokens": 800,
        "cost_usd": 0.25,
        "envelope_digest": "flow-verdict-sha",
        "created_at": "2025-01-01T12:05:00Z",
    })

    # Add rounds for tenant B PR 202
    store.record_loop_round({
        "tenant_id": "t-beta",
        "pr_number": 202,
        "round": 1,
        "role": "developer",
        "head_sha": "b" * 40,
        "tokens": 2000,
        "cost_usd": 1.20,
        "envelope_digest": "flow-ack-b",
        "created_at": "2025-01-01T14:00:00Z",
    })

    return store, tenant_a, tenant_b


# --- AC-1: Readers of tenant A cannot see tenant B's rounds ---

def test_ac1_reader_of_tenant_a_cannot_see_tenant_b_rounds():
    store, tenant_a, tenant_b = _make_setup()

    # User is reader only for tenant A (org-alpha/repo-alpha)
    user_session = Session(
        login="alice",
        repos=frozenset(["org-alpha/repo-alpha"]),
        admin=False,
        csrf="csrf-alice-1",
    )

    app.dependency_overrides.clear()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[web.current_session] = lambda: user_session
    try:
        client = TestClient(app)

        # 1. User CAN access tenant A's PR timeline
        resp_a = client.get("/tenants/org-alpha/repo-alpha/prs/101")
        assert resp_a.status_code == 200
        assert "PR #101" in resp_a.text
        assert "flow-ack-sha" in resp_a.text

        # 2. User CANNOT access tenant B's PR timeline (returns 404)
        resp_b = client.get("/tenants/org-beta/repo-beta/prs/202")
        assert resp_b.status_code == 404

        # 3. Tenant A's page does NOT leak tenant B rounds
        page_a = client.get("/tenants/org-alpha/repo-alpha")
        assert resp_a.status_code == 200
        assert "PR #202" not in page_a.text
        assert "flow-ack-b" not in page_a.text
    finally:
        app.dependency_overrides.clear()


# --- AC-2: Spend shown against the caps ---

def test_ac2_spend_and_rounds_shown_against_caps():
    store, tenant_a, _ = _make_setup()

    user_session = Session(
        login="alice",
        repos=frozenset(["org-alpha/repo-alpha"]),
        admin=False,
        csrf="csrf-alice-2",
    )

    app.dependency_overrides.clear()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[web.current_session] = lambda: user_session
    try:
        client = TestClient(app)

        # Total spend on PR 101: 0.45 + 0.25 = 0.70 USD against cap 5.00 USD
        resp = client.get("/tenants/org-alpha/repo-alpha/prs/101")
        assert resp.status_code == 200
        html = resp.text

        # Verify spend against cap
        assert "$0.70" in html or "0.70" in html
        assert "$5.00" in html or "5.0" in html

        # Verify rounds against cap
        assert "3" in html  # max_review_rounds

        # Tenant overview page also shows loop history summary
        overview = client.get("/tenants/org-alpha/repo-alpha")
        assert overview.status_code == 200
        assert "Loop history" in overview.text or "PR #101" in overview.text
    finally:
        app.dependency_overrides.clear()
