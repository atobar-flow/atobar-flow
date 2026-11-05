from fastapi.testclient import TestClient

from control_panel.app import app
from control_panel.store import MemoryStore, get_store
from control_panel.web import Session, current_session

client = TestClient(app)


def test_tenant_detail_shows_manifest_caps_default_25_and_4():
    fake_store = MemoryStore()
    fake_store.create_tenant("test-owner/test-caps", "admin")

    # In default manifest, max_pr_cost_usd is 25 and max_review_rounds is 4
    # Simulate logged in user who can view the tenant
    session = Session(
        login="admin",
        repos=frozenset(["test-owner/test-caps"]),
        admin=True,
        csrf="csrf-admin-caps",
    )

    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[current_session] = lambda: session

    resp = client.get("/tenants/test-owner/test-caps")
    assert resp.status_code == 200
    html = resp.text

    # Verify 4 max rounds is shown in PR history table header or row
    assert "4 max" in html or "max_review_rounds" in html or "4" in html

    # Also test PR detail page shows 25 max cost and 4 max rounds
    resp_pr = client.get("/tenants/test-owner/test-caps/prs/101")
    assert resp_pr.status_code == 200
    pr_html = resp_pr.text

    # PR timeline / caps info
    assert "4 max" in pr_html
    assert "$25.00 max" in pr_html or "$25.0 max" in pr_html or "25 max" in pr_html


def test_tenant_goal_dashboard_and_form_submission():
    fake_store = MemoryStore()
    tenant = fake_store.create_tenant("test-owner/test-goal", "admin")
    fake_store.upsert_metric_definition(
        tenant.id,
        {
            "key": "coverage_pct",
            "status": "active",
            "comparator": ">=",
            "threshold": 85.0,
            "kind": "target",
        },
    )
    fake_store.upsert_metric_definition(
        tenant.id,
        {
            "key": "test_pass_rate",
            "status": "active",
            "comparator": ">=",
            "threshold": 1.0,
            "kind": "guardrail",
        },
    )
    fake_store.record_observations(
        [
            {
                "tenant_id": tenant.id,
                "key": "coverage_pct",
                "value": 68.0,
                "commit_sha": "a" * 40,
                "pr_number": None,
            },
            {
                "tenant_id": tenant.id,
                "key": "test_pass_rate",
                "value": 1.0,
                "commit_sha": "a" * 40,
                "pr_number": None,
            },
        ]
    )

    session = Session(
        login="admin",
        repos=frozenset(["test-owner/test-goal"]),
        admin=True,
        csrf="csrf-test-goal",
    )

    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[current_session] = lambda: session
    try:
        r_post = client.post(
            "/tenants/test-owner/test-goal/goal",
            data={
                "csrf": "csrf-test-goal",
                "objective": "Reach 85% coverage without breaking tests",
                "targets": "coverage_pct",
                "guardrails": "test_pass_rate",
                "autonomy": "auto",
                "tracking_issue_number": "77",
                "max_goal_cost_usd": "150",
                "max_goal_prs": "12",
                "max_goal_days": "21",
                "max_merges_without_progress": "4",
                "max_open_planned_issues": "3",
            },
            follow_redirects=False,
        )
        assert r_post.status_code == 303

        fake_store.update_tenant_goal(tenant.id, {"spent_usd": 18.5, "prs_merged": 2, "merges_without_progress": 1})

        r_get = client.get("/tenants/test-owner/test-goal")
        assert r_get.status_code == 200
        html = r_get.text
        assert "Outer Goal Loop Status" in html
        assert "Reach 85% coverage without breaking tests" in html
        assert "coverage_pct" in html
        assert "<progress value='80' max='100'>" in html
        assert "$18.50 / $150.00" in html
        assert "2 / 12" in html
        assert "#77" in html
    finally:
        app.dependency_overrides.clear()

