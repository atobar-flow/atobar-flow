import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from control_panel.app import app, current_tenant, get_dispatcher, get_store
from control_panel.store import MemoryStore, Tenant
from atobar_contracts.conductor_io import compute_event_id, process_flow_event
from atobar_contracts.constants import get_bot_logins
from atobar_contracts.envelopes import Ack, Plan, PlannedCriterion, Verdict, render

SHA1 = "1111111111111111111111111111111111111111"
SENTINEL_SHA = "0" * 40


class FakeFullDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, Any]]] = []
        self.issue_bodies: dict[tuple[str, int], str] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def dispatch(self, event_type: str, client_payload: dict[str, Any]) -> None:
        self.dispatched.append((event_type, client_payload))

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        return self.issue_bodies.get((repo, issue_number), "")

    def get_collaborator_permission(self, repo: str, username: str) -> str | None:
        return "admin"

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).discard(label)

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).add(label)

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self.comments.setdefault((repo, issue_number), []).append(
            {"user": {"login": "atobar-flow-agent[bot]"}, "body": body}
        )

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        return True

    def comment_on_manifest_pr(self, repo: str, comment: str) -> bool:
        return True


def _setup_cp(manifest: dict[str, Any] | None = None) -> tuple[TestClient, MemoryStore, FakeFullDispatcher, Tenant]:
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    if manifest is not None:
        store.tenants[tenant.id] = dataclasses.replace(store.tenants[tenant.id], manifest=manifest)
    disp = FakeFullDispatcher()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_dispatcher] = lambda: disp
    app.dependency_overrides[current_tenant] = lambda: store.tenants[tenant.id]
    return TestClient(app), store, disp, store.tenants[tenant.id]


def test_wp4_ac1_ingest_event_uses_sufficiency_gate_for_initial_role():
    client, store, disp, tenant = _setup_cp()
    try:
        # 1. Well-specified issue -> fast-track straight to developer
        disp.issue_bodies[(tenant.repo, 10)] = (
            "## Scope\n- `src/app.py`\n\n## Acceptance criteria\n- [ ] AC-1: Works properly\n"
        )
        r1 = client.post(
            "/v1/events",
            json={"kind": "issues.labeled", "issue_number": 10, "label": "flow:feature"},
        )
        assert r1.status_code == 202 and r1.json()["dispatched"] is True
        wf1, payload1 = disp.dispatched[-1]
        assert wf1 == "flow_run"
        assert payload1["role"] == "developer"
        assert store.run_creds[payload1["run_id"]]["role"] == "developer"

        # 2. Underspecified issue -> routes to planner first
        disp.issue_bodies[(tenant.repo, 11)] = "Fix the dashboard overflow on small screens."
        r2 = client.post(
            "/v1/events",
            json={"kind": "issues.labeled", "issue_number": 11, "label": "flow:fix"},
        )
        assert r2.status_code == 202 and r2.json()["dispatched"] is True
        wf2, payload2 = disp.dispatched[-1]
        assert wf2 == "flow_run"
        assert payload2["role"] == "planner"
        assert store.run_creds[payload2["run_id"]]["role"] == "planner"

        # 3. Explicit flow:plan label -> routes to planner even if body has scope & ACs
        disp.issue_bodies[(tenant.repo, 12)] = (
            "## Scope\n- `src/app.py`\n\n## Acceptance criteria\n- [ ] AC-1: Works\n"
        )
        r3 = client.post(
            "/v1/events",
            json={"kind": "issues.labeled", "issue_number": 12, "label": "flow:plan"},
        )
        assert r3.status_code == 202 and r3.json()["dispatched"] is True
        wf3, payload3 = disp.dispatched[-1]
        assert wf3 == "flow_run"
        assert payload3["role"] == "planner"
    finally:
        app.dependency_overrides.clear()


def test_wp4_ac2_and_ac3_turn_complete_handover_and_webhook_dedup():
    client, store, disp, tenant = _setup_cp()
    try:
        issue_num = 33
        plan_run_id = "run_plan_initial"
        plan_cred = "cred_plan_initial"
        store.create_run_credential(
            plan_run_id,
            tenant.id,
            "planner",
            hashlib.sha256(plan_cred.encode()).hexdigest(),
            (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        )
        store.upsert_session(
            {
                "instance_id": "i-warmspot01",
                "run_id": plan_run_id,
                "tenant_id": tenant.id,
                "role": "planner",
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "status": "Running",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Turn 1: Planner completes single_pr flow-plan and calls turn-complete
        plan = Plan(
            tenant=tenant.repo,
            pr=issue_num,
            head_sha=SENTINEL_SHA,
            run_id=plan_run_id,
            complexity="single_pr",
            goal="Fix overflow",
            scope_files=["src/ui.py"],
            acceptance_criteria=[PlannedCriterion(id="AC-1", description="Fits 80 cols")],
            implementation_steps=["Update src/ui.py"],
        )
        plan_comment_body = render(plan)
        r_plan = client.post(
            f"/v1/runs/{plan_run_id}/turn-complete",
            headers={"Authorization": f"Bearer {plan_cred}"},
            json={
                "comment_id": 701,
                "comment_body": plan_comment_body,
                "head_sha": SENTINEL_SHA,
                "issue_number": issue_num,
                "instance_id": "i-warmspot01",
            },
        )
        assert r_plan.status_code == 200
        handover1 = r_plan.json()
        assert handover1["action"] == "continue"
        assert handover1["role"] == "developer"
        dev_run_id = handover1["run_id"]
        dev_cred = handover1["run_credential"]
        assert dev_run_id != plan_run_id
        assert dev_cred
        # Zero cold Spot dispatches!
        assert disp.dispatched == []
        # Session updated in-place on the same Spot instance
        sess = store.sessions["i-warmspot01"]
        assert sess["run_id"] == dev_run_id
        assert sess["role"] == "developer"

        # AC-3: Delayed GitHub webhook for comment_id=701 arrives via flow-trigger.yml
        bots = get_bot_logins()
        webhook_event = {
            "kind": "issue_comment.created",
            "issue_number": issue_num,
            "comment_body": plan_comment_body,
            "comment_id": 701,
            "sender": bots["planner"],
        }
        expected_event_id = compute_event_id(webhook_event)
        assert store.has_processed_event(tenant.id, expected_event_id) is True

        r_dup = client.post("/v1/events", json=webhook_event)
        assert r_dup.status_code == 202
        assert r_dup.json()["dispatched"] is False
        assert disp.dispatched == []

        # Turn 2: Developer completes flow-ack at SHA1 and calls turn-complete.
        # J43 WP-2 (INF-02): Verifying roles (evaluator/arbitrator) never inherit a warm VM from developer.
        ack = Ack(
            tenant=tenant.repo,
            pr=issue_num,
            head_sha=SHA1,
            run_id=dev_run_id,
        )
        ack_comment_body = render(ack)
        r_dev = client.post(
            f"/v1/runs/{dev_run_id}/turn-complete",
            headers={"Authorization": f"Bearer {dev_cred}"},
            json={
                "comment_id": 702,
                "comment_body": ack_comment_body,
                "head_sha": SHA1,
                "issue_number": issue_num,
                "instance_id": "i-warmspot01",
            },
        )
        assert r_dev.status_code == 200
        handover2 = r_dev.json()
        assert handover2["action"] == "shutdown"
        assert len(disp.dispatched) == 1
        wf_eval, payload_eval = disp.dispatched[0]
        assert wf_eval == "flow_run"
        assert payload_eval["role"] == "evaluator"
        assert payload_eval["head_sha"] == SHA1
    finally:
        app.dependency_overrides.clear()


def test_wp4_ac4_turn_complete_returns_shutdown_when_no_immediate_role():
    # Tenant requires an external CI check "ci/build" before convergence
    client, store, disp, tenant = _setup_cp(manifest={"required_checks": ["ci/build"]})
    try:
        issue_num = 44
        eval_run_id = "run_eval_1"
        eval_cred = "cred_eval_1"
        store.create_run_credential(
            eval_run_id,
            tenant.id,
            "evaluator",
            hashlib.sha256(eval_cred.encode()).hexdigest(),
            (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        )
        store.upsert_session(
            {
                "instance_id": "i-warmspot02",
                "run_id": eval_run_id,
                "tenant_id": tenant.id,
                "role": "evaluator",
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "status": "Running",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        verdict = Verdict(
            tenant=tenant.repo,
            pr=issue_num,
            head_sha=SHA1,
            run_id=eval_run_id,
            round=1,
            verdict="approved",
            findings=[],
        )
        r = client.post(
            f"/v1/runs/{eval_run_id}/turn-complete",
            headers={"Authorization": f"Bearer {eval_cred}"},
            json={
                "comment_id": 801,
                "comment_body": render(verdict),
                "head_sha": SHA1,
                "issue_number": issue_num,
                "instance_id": "i-warmspot02",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "shutdown"
        assert disp.dispatched == []
    finally:
        app.dependency_overrides.clear()


def test_wp4_ac5_serverless_arbitrator_on_check_run_completed_when_no_live_vm():
    manifest = {
        "repo": "arsenyspb/ollama-monitor",
        "owner_github_login": "arsenyspb",
        "required_checks": ["ci/build"],
        "arbitration": {"mode": "recommend"},
        "metrics": [],
    }
    client, store, disp, tenant = _setup_cp(manifest=manifest)
    try:
        pr_num = 55
        dev_run_id = "run_dev_55"
        eval_run_id = "run_eval_55"
        store.create_run_credential(dev_run_id, tenant.id, "developer", "h1", "2099-01-01T00:00:00Z", head_sha=SHA1)
        store.create_run_credential(eval_run_id, tenant.id, "evaluator", "h2", "2099-01-01T00:00:00Z", head_sha=SHA1)
        store.claim_run_credential_envelope(dev_run_id, 1)
        store.claim_run_credential_envelope(eval_run_id, 2)

        ack = Ack(tenant=tenant.repo, pr=pr_num, head_sha=SHA1, run_id=dev_run_id)
        verdict = Verdict(
            tenant=tenant.repo,
            pr=pr_num,
            head_sha=SHA1,
            run_id=eval_run_id,
            round=1,
            verdict="approved",
            findings=[],
        )

        class _GH:
            def __init__(self) -> None:
                self.dispatched_runs: list[dict[str, Any]] = []
                self.comments_posted: list[str] = []
                self.checks_created: list[dict[str, Any]] = []

            def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
                return {"number": pr_number, "head": {"sha": SHA1}, "labels": []}

            def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
                return [
                    {"id": 1, "user": {"login": "atobar-flow-agent[bot]"}, "body": render(ack)},
                    {"id": 2, "user": {"login": "atobar-flow-agent[bot]"}, "body": render(verdict)},
                ]

            def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
                return [{"name": "ci/build", "conclusion": "success"}]

            def create_comment(self, repo: str, issue_number: int, body: str) -> None:
                self.comments_posted.append(body)

            def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
                self.comments_posted.append(body)

            def create_check_run(
                self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]
            ) -> None:
                self.checks_created.append({"name": name, "head_sha": head_sha, "conclusion": conclusion})

            def add_label(self, repo: str, issue_number: int, label: str) -> None:
                pass

            def remove_label(self, repo: str, issue_number: int, label: str) -> None:
                pass

            def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None:
                self.dispatched_runs.append(payload)

        gh = _GH()
        res = process_flow_event(
            payload={
                "tenant": tenant.repo,
                "event": {
                    "kind": "check_run.completed",
                    "pr_number": pr_num,
                    "check_name": "ci/build",
                    "conclusion": "success",
                    "head_sha": SHA1,
                },
            },
            gh=gh,
            store=store,
            bot_logins=get_bot_logins(),
        )
        assert res["status"] == "success"
        assert res["new_status"] == "converged"
        # Arbitrator ran in-process without spawning a Spot VM!
        assert gh.dispatched_runs == []
        assert any("```flow-arbitration" in c for c in gh.comments_posted)
        assert any(c["name"] == "flow/arbiter" and c["conclusion"] == "success" for c in gh.checks_created)
    finally:
        app.dependency_overrides.clear()
