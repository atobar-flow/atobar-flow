"""Tests for Conductor Outer Goal Loop goal_step() and POST /internal/v1/metrics integration (WP-4 / AC-4)."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from atobar_contracts.goal import GoalBudget, GoalStallPolicy, TenantGoal
from atobar_contracts.issue_states import STATUS_MARKER
from atobar_contracts.manifest import Metric, TenantManifest
from flow_engine.api import app, get_ec2_client, get_github_client
from flow_engine.conductor.goal import (
    GoalMetAction,
    GoalPlanAction,
    GoalState,
    GoalStopAction,
    GoalWaitAction,
    goal_step,
)
from flow_engine.store import MemoryStore, get_store

NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _manifest_with_goal(tracking_issue: int = 55) -> TenantManifest:
    return TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        metrics=[
            Metric(key="coverage_pct", run="cov", comparator=">=", threshold=85.0),
            Metric(key="latency_ms", run="lat", comparator="<=", threshold=100.0),
            Metric(key="test_pass_rate", run="pytest", comparator=">=", threshold=1.0),
        ],
        goal=TenantGoal(
            objective="Reach 85% coverage and <=100ms latency while keeping tests green.",
            targets=["coverage_pct", "latency_ms"],
            guardrails=["test_pass_rate"],
            autonomy="approve",
            tracking_issue_number=tracking_issue,
            budget=GoalBudget(max_goal_cost_usd=100.0, max_goal_prs=10, max_goal_days=14),
            stall=GoalStallPolicy(max_merges_without_progress=3, max_open_planned_issues=2),
        ),
    )


@pytest.mark.parametrize(
    "state_kwargs,obs,open_issues,expected_action",
    [
        # 1. All targets meet comparator + threshold -> GoalMetAction
        (
            {},
            {"coverage_pct": 86.0, "latency_ms": 95.0, "test_pass_rate": 1.0},
            0,
            GoalMetAction(),
        ),
        # 2. Stop on budget_cost
        (
            {"spent_usd": 100.0},
            {"coverage_pct": 70.0, "latency_ms": 95.0},
            0,
            GoalStopAction(reason="budget_cost"),
        ),
        # 3. Stop on budget_prs
        (
            {"prs_merged": 10},
            {"coverage_pct": 70.0, "latency_ms": 95.0},
            0,
            GoalStopAction(reason="budget_prs"),
        ),
        # 4. Stop on budget_days
        (
            {"started_at": NOW - timedelta(days=14, seconds=1)},
            {"coverage_pct": 70.0, "latency_ms": 95.0},
            0,
            GoalStopAction(reason="budget_days"),
        ),
        # 5. Stop on stalled (merges_without_progress >= max_merges_without_progress)
        (
            {"merges_without_progress": 3},
            {"coverage_pct": 70.0, "latency_ms": 95.0},
            0,
            GoalStopAction(reason="stalled"),
        ),
        # 6. Wait when at WIP limit (open_planned_issues >= max_open_planned_issues)
        (
            {},
            {"coverage_pct": 70.0, "latency_ms": 110.0},
            2,
            GoalWaitAction(),
        ),
        # 7. Plan(n) when below WIP limit (n = max_open_planned_issues - open_planned_issues)
        (
            {},
            {"coverage_pct": 70.0, "latency_ms": 110.0},
            0,
            GoalPlanAction(count=2),
        ),
        (
            {},
            {"coverage_pct": 70.0, "latency_ms": 110.0},
            1,
            GoalPlanAction(count=1),
        ),
    ],
)
def test_ac4_goal_step_table(
    state_kwargs: dict[str, Any],
    obs: dict[str, float],
    open_issues: int,
    expected_action: Any,
) -> None:
    manifest = _manifest_with_goal()
    defaults: dict[str, Any] = {
        "goal_sha": manifest.goal.sha256() if manifest.goal else "",
        "started_at": NOW - timedelta(days=2),
    }
    defaults.update(state_kwargs)
    base_state = GoalState(**defaults)
    action = goal_step(
        manifest=manifest,
        goal_state=base_state,
        latest_default_obs=obs,
        open_planned_issues=open_issues,
        now=NOW,
    )
    assert action == expected_action


def _setup_metrics_test(store: MemoryStore, gh_mock: MagicMock) -> str:
    manifest = _manifest_with_goal(tracking_issue=55)
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    for m in manifest.metrics:
        store.upsert_metric_definition(
            tenant.id,
            {
                "key": m.key,
                "status": "active",
                "comparator": m.comparator,
                "threshold": m.threshold,
                "baseline_mode": m.baseline_mode,
                "min_samples": 1,
            },
        )
    store.update_tenant_manifest(tenant.id, manifest.model_dump(mode="json"), manifest.sha256())
    assert manifest.goal is not None
    store.set_tenant_goal(tenant.id, manifest.goal.model_dump(mode="json"))

    gh_mock.list_open_issues.return_value = []
    return tenant.id


def test_ac4_metrics_endpoint_wires_goal_step_plan_stop_and_met(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    gh_mock = MagicMock()
    ec2_mock = MagicMock()
    tenant_id = _setup_metrics_test(store, gh_mock)

    observed_values: dict[str, float] = {"coverage_pct": 72.0, "latency_ms": 95.0, "test_pass_rate": 1.0}

    def _fake_verify(**kwargs: Any) -> tuple[dict[str, float], str, int]:
        return dict(observed_values), "a" * 64, 1

    monkeypatch.setattr("flow_engine.api.verify_and_extract_metrics", _fake_verify)
    monkeypatch.setattr(
        "flow_engine.api.launch_spot_instance",
        lambda payload, ec2_client=None: {"instance_id": "i-goal-planner", "role": payload.get("role")},
    )

    monkeypatch.setenv("FE_INTERNAL_TOKEN", "fe-test-secret")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_github_client] = lambda: gh_mock
    app.dependency_overrides[get_ec2_client] = lambda: ec2_mock
    client = TestClient(app)
    headers = {"Authorization": "Bearer fe-test-secret"}

    try:
        # 1. Default-branch observation below target -> GoalPlanAction(count=2) dispatches planner in goal mode
        r1 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 101,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r1.status_code == 201
        data1 = r1.json()
        assert data1["goal_action"] == {"kind": "plan", "count": 2}
        planner_creds = [c for c in store.run_creds.values() if c.get("role") == "planner"]
        assert len(planner_creds) == 1
        assert planner_creds[0].get("goal_mode") is True

        # 2. Trip stall cap (merges_without_progress = 3) -> GoalStopAction("stalled"), flow:escalated + status card on #55
        store.update_tenant_goal(tenant_id, {"merges_without_progress": 3, "spent_usd": 42.5})
        r2 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 102,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r2.status_code == 201
        assert r2.json()["goal_action"] == {"kind": "stop", "reason": "stalled"}
        g_stopped = store.get_tenant_goal(tenant_id)
        assert g_stopped is not None
        assert g_stopped["status"] == "stopped"
        assert g_stopped["stop_reason"] == "stalled"
        gh_mock.add_label.assert_any_call("arsenyspb/ollama-monitor", 55, "flow:escalated")
        comment_args = gh_mock.create_comment.call_args[0]
        assert comment_args[0] == "arsenyspb/ollama-monitor"
        assert comment_args[1] == 55
        assert STATUS_MARKER in comment_args[2]
        assert "flow:escalated" in comment_args[2]
        assert "stalled" in comment_args[2]

        # 3. Re-open goal and report target-meeting metrics -> GoalMetAction, status='met' and flow:shipped card on #55
        store.update_tenant_goal(
            tenant_id,
            {"status": "open", "stop_reason": None, "merges_without_progress": 0, "planning_run_id": None},
        )
        observed_values.update({"coverage_pct": 88.0, "latency_ms": 90.0})
        gh_mock.reset_mock()
        r3 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 103,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r3.status_code == 201
        assert r3.json()["goal_action"] == {"kind": "met"}
        g_met = store.get_tenant_goal(tenant_id)
        assert g_met is not None
        assert g_met["status"] == "met"
        gh_mock.add_label.assert_any_call("arsenyspb/ollama-monitor", 55, "flow:shipped")
        met_comment = gh_mock.create_comment.call_args[0][2]
        assert STATUS_MARKER in met_comment
        assert "flow:shipped" in met_comment
    finally:
        app.dependency_overrides.clear()


def test_arbitration_merge_updates_goal_counters_and_proposals_are_idempotent() -> None:
    from atobar_contracts.conductor_io import process_flow_event
    from atobar_contracts.constants import get_bot_logins
    from atobar_contracts.envelopes import (
        Arbitration,
        Plan,
        PlannedCriterion,
        ProposedIssueDraft,
        execute_goal_plan_proposals,
        render,
    )

    store = MemoryStore()
    gh_mock = MagicMock()
    tenant_id = _setup_metrics_test(store, gh_mock)
    gh_mock.get_pr.return_value = {"number": 12, "head": {"sha": COMMIT_SHA}, "labels": [{"name": "flow:in-review"}]}
    gh_mock.get_comments.return_value = []
    gh_mock.get_check_runs.return_value = []

    bots = get_bot_logins()
    store.create_run_credential("run-arb-12", tenant_id, "arbitrator", "h", "2099-01-01T00:00:00Z", head_sha=COMMIT_SHA)
    store.create_run_credential("run-arb-13", tenant_id, "arbitrator", "h", "2099-01-01T00:00:00Z", head_sha=COMMIT_SHA)
    arb_no_prog = Arbitration(
        tenant="arsenyspb/ollama-monitor",
        pr=12,
        head_sha=COMMIT_SHA,
        run_id="run-arb-12",
        decision="pass",
        mode="auto_merge",
        action_taken="merged",
        progress=False,
    )
    arb_with_prog = Arbitration(
        tenant="arsenyspb/ollama-monitor",
        pr=13,
        head_sha=COMMIT_SHA,
        run_id="run-arb-13",
        decision="pass",
        mode="auto_merge",
        action_taken="merged",
        progress=True,
    )
    gh_mock.get_comments.side_effect = lambda _repo, pr_num: (
        [{"id": 1001, "user": {"login": bots["arbitrator"]}, "body": render(arb_no_prog)}]
        if pr_num == 12
        else [{"id": 1002, "user": {"login": bots["arbitrator"]}, "body": render(arb_with_prog)}]
    )
    process_flow_event(
        payload={
            "tenant": "arsenyspb/ollama-monitor",
            "event_id": "evt-arb-1",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 12,
                "comment_id": 1001,
                "comment_body": render(arb_no_prog),
                "sender": bots["arbitrator"],
            },
        },
        gh=gh_mock,
        store=store,
        bot_logins=bots,
    )
    g1 = store.get_tenant_goal(tenant_id)
    assert g1 is not None
    assert g1["prs_merged"] == 1
    assert g1["merges_without_progress"] == 1

    process_flow_event(
        payload={
            "tenant": "arsenyspb/ollama-monitor",
            "event_id": "evt-arb-2",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 13,
                "comment_id": 1002,
                "comment_body": render(arb_with_prog),
                "sender": bots["arbitrator"],
            },
        },
        gh=gh_mock,
        store=store,
        bot_logins=bots,
    )
    g2 = store.get_tenant_goal(tenant_id)
    assert g2 is not None
    assert g2["prs_merged"] == 2
    assert g2["merges_without_progress"] == 0

    # Verify idempotent issue creation when an open issue with the same title already exists
    plan = Plan(
        tenant="arsenyspb/ollama-monitor",
        pr=55,
        head_sha="0" * 40,
        complexity="multi_issue_epic",
        goal="Improve coverage",
        proposed_issues=[
            ProposedIssueDraft(
                title="Cover parser edge cases",
                goal="Add parser tests",
                scope_files=["tests/test_parser.py"],
                acceptance_criteria=[PlannedCriterion(id="AC-1", description="Pass")],
                target_metric_key="coverage_pct",
                rationale="Raises coverage_pct",
            )
        ],
    )
    gh_mock.list_open_issues.return_value = [{"number": 88, "title": "Cover parser edge cases", "state": "open"}]
    gh_mock.create_issue.reset_mock()
    nums = execute_goal_plan_proposals(plan, repo="arsenyspb/ollama-monitor", manifest=_manifest_with_goal(), gh=gh_mock)
    assert nums == [88]
    gh_mock.create_issue.assert_not_called()


def test_e2e_goal_validation_retire_block_userdata_inflight_and_turn_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    from atobar_contracts.envelopes import Plan, PlannedCriterion, ProposedIssueDraft, render

    class _EC2Capture:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
            return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"UserData": ""}}]}

        def run_instances(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"Instances": [{"InstanceId": f"i-goal-{len(self.calls)}"}]}

    store = MemoryStore()
    gh_mock = MagicMock()
    ec2 = _EC2Capture()
    tenant_id = _setup_metrics_test(store, gh_mock)

    # Unrelated open issues (no flow:goal label or goal_sha marker) must NOT count toward open_planned_issues (P1 #4)
    created_issues: list[dict[str, Any]] = [
        {"number": 1, "title": "Unrelated user bug report", "body": "Something broke", "labels": [{"name": "bug"}]},
        {"number": 2, "title": "Unrelated docs question", "body": "How to install?", "labels": [{"name": "question"}]},
        {"number": 3, "title": "Another unrelated issue", "body": "Feature idea", "labels": []},
    ]

    def _create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        num = 200 + len(created_issues) + 1
        obj = {
            "number": num,
            "repo": repo,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": lbl} for lbl in (labels or [])],
        }
        created_issues.append(obj)
        return obj

    gh_mock.create_issue.side_effect = _create_issue
    gh_mock.list_open_issues.side_effect = lambda repo: list(created_issues)

    observed_values: dict[str, float] = {"coverage_pct": 72.0, "latency_ms": 95.0, "test_pass_rate": 1.0}
    monkeypatch.setattr(
        "flow_engine.api.verify_and_extract_metrics",
        lambda **kwargs: (dict(observed_values), "a" * 64, 1),
    )
    monkeypatch.setenv("FE_INTERNAL_TOKEN", "fe-test-secret")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_github_client] = lambda: gh_mock
    app.dependency_overrides[get_ec2_client] = lambda: ec2
    client = TestClient(app)
    headers = {"Authorization": "Bearer fe-test-secret"}

    try:
        # P0 #1: PUT /internal/v1/tenants/{id}/goal with unknown target metric -> 422
        r_bad_goal = client.put(
            f"/internal/v1/tenants/{tenant_id}/goal",
            headers=headers,
            json={
                "objective": "Target a typo metric",
                "targets": ["typo_nonexistent_metric"],
                "tracking_issue_number": 55,
            },
        )
        assert r_bad_goal.status_code == 422

        # P0 #1: Retiring a metric referenced by the active goal -> 409
        r_retire_blocked = client.post(
            f"/internal/v1/tenants/{tenant_id}/metrics/coverage_pct/retire",
            headers=headers,
            json={"actor": "arsenyspb"},
        )
        assert r_retire_blocked.status_code == 409

        # P1 #4 + P0 #2 + P0 #3: POST /internal/v1/metrics ignores the 3 unrelated open issues and dispatches planner
        r_m1 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 501,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r_m1.status_code == 201
        assert r_m1.json()["goal_action"] == {"kind": "plan", "count": 2}
        assert len(ec2.calls) == 1

        # Verify UserData exports FLOW_GOAL_MODE, FLOW_PLAN_COUNT, FLOW_GOAL_OBJECTIVE_B64 (P0 #2)
        user_data_text = base64.b64decode(ec2.calls[0]["UserData"]).decode("utf-8")
        assert 'export FLOW_GOAL_MODE="1"' in user_data_text
        assert 'export FLOW_PLAN_COUNT="2"' in user_data_text
        assert "export FLOW_GOAL_OBJECTIVE_B64=" in user_data_text

        # Verify get_run_credential returns a copy with goal_mode=True persisted (P0 #3)
        planner_run_id = next(iter(store.run_creds.keys()))
        cred_copy = store.get_run_credential(planner_run_id)
        assert cred_copy is not None
        assert cred_copy["goal_mode"] is True
        assert cred_copy["plan_count"] == 2
        cred_copy["goal_mode"] = False
        assert store.get_run_credential(planner_run_id)["goal_mode"] is True

        # P1 #5: Burst second POST /internal/v1/metrics while planner is in-flight -> GoalWaitAction (no 2nd VM!)
        r_m2 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 502,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r_m2.status_code == 201
        assert r_m2.json()["goal_action"] == {"kind": "wait"}
        assert len(ec2.calls) == 1

        # Turn-complete creates goal issues with flow:goal, records planner cost, clears planning_run_id, and dedupes retries (P1 #6, P2 #10)
        plan = Plan(
            tenant="arsenyspb/ollama-monitor",
            pr=55,
            head_sha="0" * 40,
            run_id=planner_run_id,
            complexity="multi_issue_epic",
            goal="Propose 2 coverage issues",
            proposed_issues=[
                ProposedIssueDraft(
                    title="Add tests for module A",
                    goal="Cover module A",
                    scope_files=["tests/test_a.py"],
                    acceptance_criteria=[PlannedCriterion(id="AC-1", description="Pass")],
                    target_metric_key="coverage_pct",
                    rationale="Raises coverage_pct",
                ),
                ProposedIssueDraft(
                    title="Add tests for module B",
                    goal="Cover module B",
                    scope_files=["tests/test_b.py"],
                    acceptance_criteria=[PlannedCriterion(id="AC-1", description="Pass")],
                    target_metric_key="coverage_pct",
                    rationale="Raises coverage_pct",
                ),
            ],
        )
        r_tc1 = client.post(
            f"/internal/v1/runs/{planner_run_id}/turn-complete",
            headers=headers,
            json={
                "comment_body": render(plan),
                "issue_number": 55,
                "head_sha": "0" * 40,
                "usage": {"total": {"input_tokens": 500, "output_tokens": 200, "cost_usd": 1.25}},
            },
        )
        assert r_tc1.status_code == 200
        assert r_tc1.json()["reason"] == "goal_planned"
        assert len(r_tc1.json()["created_issues"]) == 2
        g_after = store.get_tenant_goal(tenant_id)
        assert g_after is not None
        assert g_after["spent_usd"] == pytest.approx(1.25)
        assert g_after["planning_run_id"] is None

        # Retry turn-complete is a deduped no-op (P1 #6)
        r_tc2 = client.post(
            f"/internal/v1/runs/{planner_run_id}/turn-complete",
            headers=headers,
            json={
                "comment_body": render(plan),
                "issue_number": 55,
                "head_sha": "0" * 40,
                "usage": {"total": {"input_tokens": 500, "output_tokens": 200, "cost_usd": 1.25}},
            },
        )
        assert r_tc2.status_code == 200
        assert r_tc2.json().get("deduped") is True
        assert store.get_tenant_goal(tenant_id)["spent_usd"] == pytest.approx(1.25)

        # Now 2 goal-labeled open issues exist -> next metrics POST returns GoalWaitAction (at WIP limit 2)
        r_m3 = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant": "arsenyspb/ollama-monitor",
                "commit_sha": COMMIT_SHA,
                "run_id": 503,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": observed_values,
            },
        )
        assert r_m3.status_code == 201
        assert r_m3.json()["goal_action"] == {"kind": "wait"}

        # J44 WP-5 (AS-08): developer/evaluator turns and halted runs on goal-spawned issues roll up into spent_usd
        from atobar_contracts.envelopes import Ack

        spawned_issue = int(r_tc1.json()["created_issues"][0])
        dev_run_id = "run-goal-dev-1"
        store.create_run_credential(dev_run_id, tenant_id, "developer", "h", "2099-01-01T00:00:00Z")
        store.upsert_session(
            {
                "instance_id": "i-goal-dev-1",
                "run_id": dev_run_id,
                "tenant_id": tenant_id,
                "role": "developer",
                "target_repo": "arsenyspb/ollama-monitor",
                "issue_id": spawned_issue,
                "status": "Running",
                "usage": {"total": {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 3.50}},
            }
        )
        ack_env = render(
            Ack(
                tenant="arsenyspb/ollama-monitor",
                pr=spawned_issue,
                head_sha=COMMIT_SHA,
                run_id=dev_run_id,
                addressed_findings=[],
                dispute=[],
            )
        )
        r_dev_tc = client.post(
            f"/internal/v1/runs/{dev_run_id}/turn-complete",
            headers=headers,
            json={
                "comment_body": ack_env,
                "comment_id": 8801,
                "issue_number": spawned_issue,
                "head_sha": COMMIT_SHA,
                "usage": {"total": {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 3.50}},
            },
        )
        assert r_dev_tc.status_code == 200
        assert store.get_tenant_goal(tenant_id)["spent_usd"] == pytest.approx(4.75)

        # Halted run heartbeat on a goal-spawned issue also rolls up into spent_usd, including on retry after transient failure
        halted_run_id = "run-goal-eval-halted"
        store.create_run_credential(halted_run_id, tenant_id, "evaluator", "h", "2099-01-01T00:00:00Z")
        orig_record_goal_spend = store.record_goal_spend
        fail_once = {"remaining": 1}

        def _flaky_goal_spend(tid: str, delta: float) -> None:
            if fail_once["remaining"] > 0:
                fail_once["remaining"] -= 1
                raise RuntimeError("transient spend rpc failure")
            orig_record_goal_spend(tid, delta)

        monkeypatch.setattr(store, "record_goal_spend", _flaky_goal_spend)
        hb_payload = {
            "status": "Halted: timeout",
            "instance_id": "i-goal-eval-halted",
            "issue_id": spawned_issue,
            "halt_reason": "timeout",
            "usage": {"total": {"input_tokens": 400, "output_tokens": 100, "cost_usd": 0.75}},
        }
        r_hb1 = client.post(f"/internal/v1/runs/{halted_run_id}/heartbeat", headers=headers, json=hb_payload)
        assert r_hb1.status_code == 200
        assert store.get_tenant_goal(tenant_id)["spent_usd"] == pytest.approx(4.75)

        r_hb2 = client.post(f"/internal/v1/runs/{halted_run_id}/heartbeat", headers=headers, json=hb_payload)
        assert r_hb2.status_code == 200
        assert store.get_tenant_goal(tenant_id)["spent_usd"] == pytest.approx(5.50)

        # Third retry is idempotent (does not double-count $0.75)
        r_hb3 = client.post(f"/internal/v1/runs/{halted_run_id}/heartbeat", headers=headers, json=hb_payload)
        assert r_hb3.status_code == 200
        assert store.get_tenant_goal(tenant_id)["spent_usd"] == pytest.approx(5.50)
    finally:
        app.dependency_overrides.clear()


