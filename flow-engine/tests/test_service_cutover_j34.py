"""WP-2 J34 Service Cutover E2E tests for flow_engine.api (#301)."""

import hashlib
import io
import json
import logging
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from atobar_contracts.envelopes import Plan, PlannedCriterion, Verdict, render
from atobar_contracts.manifest import canonical_sha
from flow_engine import api as fe_api
from flow_engine.store import MemoryStore


class FakeEC2Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
        return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"UserData": ""}}]}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        idx = len(self.calls)
        return {"Instances": [{"InstanceId": f"i-spot-{idx:04d}"}]}


class FakeGitHubVerifierAndClient:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, int], dict[str, Any]] = {}
        self.artifacts: dict[tuple[str, int], bytes] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.assignees: dict[tuple[str, int], set[str]] = {}
        self.review_requests: list[tuple[str, int, list[str]]] = []
        self.seeded_repo_labels: list[str] = []
        self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.issue_bodies: dict[tuple[str, int], str] = {}
        self.opened_prs: list[dict[str, Any]] = []

    def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        return self.runs.get((repo, run_id), {})

    def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
        return self.artifacts.get((repo, run_id), b"")

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        lbls = [{"name": n} for n in sorted(self.labels.get((repo, pr_number), set()))]
        return {"number": pr_number, "head": {"sha": "a" * 40}, "labels": lbls}

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        return ["src/main.py"]

    def get_commit_sha(self, repo: str, ref: str) -> str:
        return "a" * 40

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.comments.get((repo, pr_number), []))

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        return list(self.check_runs.get((repo, ref), []))

    def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        entry = {
            "id": len(self.comments.get((repo, issue_number), [])) + 1,
            "user": {"login": "atobar-flow-agent[bot]"},
            "body": body,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.comments.setdefault((repo, issue_number), []).append(entry)
        return entry

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        for entries in self.comments.values():
            for c in entries:
                if c.get("id") == comment_id:
                    c["body"] = body
                    return c
        return {"id": comment_id, "body": body}

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self.get_pr(repo, issue_number)

    def seed_repository_labels(self, repo: str, labels: Any = None) -> None:
        self.seeded_repo_labels.append(repo)

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).add(label)

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).discard(label)

    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        self.assignees.setdefault((repo, issue_number), set()).update(assignees)

    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        for a in assignees:
            self.assignees.setdefault((repo, issue_number), set()).discard(a)

    def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        self.review_requests.append((repo, pr_number, list(reviewers)))

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        return self.issue_bodies.get(
            (repo, issue_number),
            "## Scope\n- `src/main.py`\n\n## Acceptance criteria\n- [ ] AC-1: Passes\n",
        )

    def open_or_update_metrics_pr(
        self,
        repo: str,
        workflow_yaml: str,
        default_branch: str = "main",
    ) -> dict[str, Any]:
        pr_entry = {
            "repo": repo,
            "workflow_yaml": workflow_yaml,
            "default_branch": default_branch,
            "number": len(self.opened_prs) + 100,
            "html_url": f"https://github.com/{repo}/pull/{len(self.opened_prs) + 100}",
        }
        self.opened_prs.append(pr_entry)
        return {"number": pr_entry["number"], "html_url": pr_entry["html_url"]}


def _make_metrics_zip(commit_sha: str, values: dict[str, float], run_attempt: int = 1) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "flow-metrics.json",
            json.dumps(
                {
                    "commit_sha": commit_sha,
                    "run_attempt": run_attempt,
                    "values": values,
                }
            ),
        )
    return buf.getvalue()


@pytest.fixture()
def fe_fixture(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FE_INTERNAL_TOKEN", "fe-secret-token-301")
    store = MemoryStore()
    ec2 = FakeEC2Client()
    gh = FakeGitHubVerifierAndClient()

    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: gh
    client = TestClient(fe_api.app)
    try:
        yield client, store, ec2, gh, {"Authorization": "Bearer fe-secret-token-301"}
    finally:
        fe_api.app.dependency_overrides.clear()


def test_request_id_middleware_and_healthz(fe_fixture, caplog: pytest.LogCaptureFixture):
    client, _, _, _, _ = fe_fixture
    caplog.set_level(logging.INFO)

    # 1. Auto-generated UUIDv4 X-Request-ID when header is omitted
    r1 = client.get("/internal/healthz")
    assert r1.status_code == 200
    assert r1.json() == {"status": "ok"}
    gen_id = r1.headers.get("X-Request-ID")
    assert gen_id is not None
    uuid.UUID(gen_id, version=4)

    # 2. Echoes incoming X-Request-ID and emits structured JSON log
    caplog.clear()
    r2 = client.get("/internal/healthz", headers={"X-Request-ID": "req-trace-999"})
    assert r2.status_code == 200
    assert r2.headers.get("X-Request-ID") == "req-trace-999"

    structured_logs = []
    for rec in caplog.records:
        try:
            doc = json.loads(rec.getMessage())
            if isinstance(doc, dict) and doc.get("request_id") == "req-trace-999":
                structured_logs.append(doc)
        except Exception:
            continue
    assert len(structured_logs) == 1
    entry = structured_logs[0]
    assert set(entry.keys()) >= {"request_id", "tenant", "run_id", "route", "status", "ms"}
    assert entry["route"] == "/internal/healthz"
    assert entry["status"] == 200


def test_internal_token_auth_enforced_on_all_internal_v1_routes(fe_fixture, monkeypatch: pytest.MonkeyPatch):
    client, _, _, _, _ = fe_fixture

    routes = [
        ("POST", "/internal/v1/events", {"tenant": "arsenyspb/ollama-monitor", "event": {}}),
        (
            "POST",
            "/internal/v1/metrics",
            {
                "tenant_id": "t-0",
                "commit_sha": "a" * 40,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": {},
            },
        ),
        ("POST", "/internal/v1/runs/r-1/heartbeat", {"status": "Running", "instance_id": "i-1", "issue_id": 1}),
        ("POST", "/internal/v1/runs/r-1/events", {"events": []}),
        ("POST", "/internal/v1/runs/r-1/turn-complete", {"comment_body": "done", "issue_number": 1}),
        (
            "POST",
            "/internal/v1/runs/r-1/arbitration-decisions",
            {"pr_number": 1, "head_sha": "a" * 40, "mode": "shadow", "decision": "pass"},
        ),
        ("GET", "/internal/v1/runs/r-1/arbitration-data", None),
        ("GET", "/internal/v1/runs/r-1/wiki", None),
        ("POST", "/internal/v1/runs/r-1/wiki", {"purpose": "test"}),
        (
            "POST",
            "/internal/v1/runs/r-1/metric-compilation",
            {
                "key": "m1",
                "compiled": {"run": "pytest -q", "comparator": ">=", "threshold": 1.0},
                "value": 1.0,
                "sha": "a" * 40,
            },
        ),
        ("POST", "/internal/v1/tenants", {"repo": "arsenyspb/ollama-monitor"}),
        ("PUT", "/internal/v1/tenants/t-0/arbitration-mode", {"mode": "recommend"}),
        ("PUT", "/internal/v1/tenants/t-0/guardrails", {"spot_max_instance_minutes": 90}),
        ("PUT", "/internal/v1/tenants/t-0/metrics/m1", {"intent": "test pass rate"}),
        ("POST", "/internal/v1/tenants/t-0/metrics/m1/compile", {}),
        ("POST", "/internal/v1/tenants/t-0/metrics/m1/approve", {"digest": "abc"}),
        ("POST", "/internal/v1/tenants/t-0/metrics/m1/retire", {}),
    ]

    for method, path, payload in routes:
        # Missing token -> 401
        resp_missing = (
            client.request(method, path, json=payload) if payload is not None else client.request(method, path)
        )
        assert resp_missing.status_code in (401, 403), f"{method} {path} allowed missing token"

        # Mismatched token -> 403
        bad_headers = {"Authorization": "Bearer wrong-token"}
        resp_bad = (
            client.request(method, path, headers=bad_headers, json=payload)
            if payload is not None
            else client.request(method, path, headers=bad_headers)
        )
        assert resp_bad.status_code in (401, 403), f"{method} {path} allowed wrong token"

    # Unset FE_INTERNAL_TOKEN fails closed even if empty bearer is passed
    monkeypatch.delenv("FE_INTERNAL_TOKEN", raising=False)
    r_unset = client.post(
        "/internal/v1/tenants",
        headers={"Authorization": "Bearer "},
        json={"repo": "arsenyspb/ollama-monitor"},
    )
    assert r_unset.status_code in (401, 403, 503)


def test_events_deduplication_and_spot_dispatch_with_tenant_guardrails(fe_fixture):
    client, store, ec2, gh, headers = fe_fixture

    # Create active tenant and set spot_max_instance_minutes = 95
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    r_guard = client.put(
        f"/internal/v1/tenants/{tenant.id}/guardrails",
        headers=headers,
        json={
            "spot_max_instance_minutes": 95,
            "max_pr_wall_clock_minutes": 180,
            "max_pr_cost_usd": 15.0,
            "max_review_rounds": 3,
            "max_repeated_finding": 2,
        },
    )
    assert r_guard.status_code == 200

    # 1. Labeled issue event dispatches Spot with all 6 atobar:* tags and atobar:max_minutes == "95"
    evt_payload = {
        "tenant": tenant.repo,
        "delivery_id": "deliv-issue-42",
        "event": {
            "kind": "issues.labeled",
            "issue_number": 42,
            "label": "flow:feature",
            "issue_body": "## Scope\n- `src/app.py`\n\n## Acceptance criteria\n- [ ] AC-1: Works\n",
        },
    }
    r1 = client.post("/internal/v1/events", headers=headers, json=evt_payload)
    assert r1.status_code in (200, 202)
    assert r1.json()["dispatched"] is True
    assert len(ec2.calls) == 1
    tags = {t["Key"]: t["Value"] for t in ec2.calls[0]["TagSpecifications"][0]["Tags"]}
    assert tags["atobar:managed"] == "true"
    assert tags["atobar:tenant"] == "arsenyspb/ollama-monitor"
    assert tags["atobar:issue"] == "42"
    assert tags["atobar:pr"] == "42"
    assert tags["atobar:run_id"] != ""
    assert tags["atobar:max_minutes"] == "95"

    # 2. Replaying the same delivery_id is deduplicated in conductor_events and does not call EC2 again
    r_dup = client.post("/internal/v1/events", headers=headers, json=evt_payload)
    assert r_dup.status_code in (200, 202)
    assert r_dup.json()["status"] == "duplicate"
    assert len(ec2.calls) == 1

    # 3. flow:onboard event mints onboarder credential and dispatches Spot
    r_onb = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": tenant.repo,
            "delivery_id": "deliv-onboard-9",
            "event": {"kind": "issues.labeled", "issue_number": 9, "label": "flow:onboard"},
        },
    )
    assert r_onb.status_code in (200, 202)
    assert r_onb.json()["dispatched"] is True
    assert len(ec2.calls) == 2
    onb_tags = {t["Key"]: t["Value"] for t in ec2.calls[1]["TagSpecifications"][0]["Tags"]}
    assert onb_tags["Role"] == "onboarder"
    assert onb_tags["atobar:max_minutes"] == "95"

    # 4. flow_event (Evaluator changes_requested verdict) advances Conductor and dispatches Developer Spot
    eval_run_id = "run-eval-55"
    store.create_run_credential(eval_run_id, tenant.id, "evaluator", "h", "2099-01-01T00:00:00Z")
    verdict_body = render(
        Verdict(
            tenant=tenant.repo,
            pr=55,
            round=1,
            head_sha="a" * 40,
            run_id=eval_run_id,
            verdict="changes_requested",
            findings=[],
        )
    )
    gh.labels[(tenant.repo, 55)] = {"flow:in-review"}
    gh.comments[(tenant.repo, 55)] = [{"id": 501, "user": {"login": "atobar-flow-agent[bot]"}, "body": verdict_body}]
    r_flow_evt = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": tenant.repo,
            "delivery_id": "deliv-verdict-55",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 55,
                "comment_id": 501,
                "comment_body": verdict_body,
                "sender": "atobar-flow-agent[bot]",
            },
        },
    )
    assert r_flow_evt.status_code in (200, 202)
    assert len(ec2.calls) == 3
    dev_tags = {t["Key"]: t["Value"] for t in ec2.calls[2]["TagSpecifications"][0]["Tags"]}
    assert dev_tags["Role"] == "developer"
    assert dev_tags["atobar:max_minutes"] == "95"


def test_metrics_github_provenance_tolerant_ingest_and_auto_activation(fe_fixture):
    client, store, _, gh, headers = fe_fixture

    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    # Seed two active metrics: unit_test_pass_rate (min_samples=1) and latency_ms (min_samples=2)
    store.upsert_metric_definition(
        tenant.id,
        {
            "key": "unit_test_pass_rate",
            "status": "active",
            "source": {"kind": "command", "run": "pytest -q"},
            "comparator": ">=",
            "threshold": 1.0,
            "min_samples": 1,
        },
    )
    store.upsert_metric_definition(
        tenant.id,
        {
            "key": "latency_ms",
            "status": "active",
            "source": {"kind": "command", "run": "./bench.sh"},
            "comparator": "<=",
            "threshold": 100.0,
            "min_samples": 2,
        },
    )
    manifest, expected_manifest_sha = store.rebuild_tenant_manifest(tenant.id)
    assert tenant.status == "onboarding"

    sha1 = "1" * 40
    sha2 = "2" * 40

    # 1. Unverified / failed run is rejected with 422
    gh.runs[(tenant.repo, 900)] = {
        "id": 900,
        "status": "completed",
        "conclusion": "failure",
        "head_sha": sha1,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    r_fail = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha1,
            "run_id": 900,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0},
        },
    )
    assert r_fail.status_code == 422
    assert store.observations == []

    # 2. First verified default-branch run with undeclared key 'retired_metric' -> recorded & ignored partitioned,
    #    not yet activated because latency_ms needs min_samples=2
    zip1 = _make_metrics_zip(
        sha1,
        {"unit_test_pass_rate": 1.0, "latency_ms": 42.5, "retired_metric": 999.0},
        run_attempt=1,
    )
    gh.runs[(tenant.repo, 901)] = {
        "id": 901,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha1,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 901)] = zip1

    r_obs1 = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha1,
            "pr_number": None,
            "run_id": 901,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0, "latency_ms": 42.5, "retired_metric": 999.0},
        },
    )
    assert r_obs1.status_code in (200, 201)
    body1 = r_obs1.json()
    assert body1["recorded"] == ["latency_ms", "unit_test_pass_rate"]
    assert body1["ignored"] == ["retired_metric"]
    assert store.tenant_by_id(tenant.id).status == "onboarding"
    assert len(store.observations) == 2
    assert all(o["manifest_sha"] == expected_manifest_sha for o in store.observations)
    assert all(o["artifact_sha256"] == hashlib.sha256(zip1).hexdigest() for o in store.observations)

    # 3. Second verified default-branch run satisfies min_samples=2 for latency_ms -> auto-activates tenant!
    zip2 = _make_metrics_zip(
        sha2,
        {"unit_test_pass_rate": 1.0, "latency_ms": 40.0},
        run_attempt=2,
    )
    gh.runs[(tenant.repo, 902)] = {
        "id": 902,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha2,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 2,
    }
    gh.artifacts[(tenant.repo, 902)] = zip2

    r_obs2 = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha2,
            "pr_number": None,
            "run_id": 902,
            "run_attempt": 2,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0, "latency_ms": 40.0},
        },
    )
    assert r_obs2.status_code in (200, 201)
    assert r_obs2.json().get("activated") is True
    assert store.tenant_by_id(tenant.id).status == "active"


def test_run_scoped_routes_heartbeat_events_turn_complete_wiki_and_arbitration(fe_fixture):
    client, store, ec2, gh, headers = fe_fixture

    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    store.upsert_metric_definition(
        tenant.id,
        {
            "key": "unit_test_pass_rate",
            "status": "active",
            "source": {"kind": "command", "run": "pytest -q"},
            "comparator": ">=",
            "threshold": 1.0,
            "min_samples": 1,
        },
    )
    manifest, manifest_sha = store.rebuild_tenant_manifest(tenant.id)
    # Require external check "ci/test" so Evaluator approval terminates warm VM instead of immediately arbitrating
    manifest["required_checks"] = ["ci/test"]
    manifest_sha = canonical_sha(manifest)
    store.update_tenant_manifest(tenant.id, manifest, manifest_sha)

    run_id = "run-planner-77"
    store.create_run_credential(
        run_id,
        tenant.id,
        "planner",
        hashlib.sha256(b"cred-77").hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    store.upsert_session(
        {
            "instance_id": f"pending-{run_id}",
            "run_id": run_id,
            "tenant_id": tenant.id,
            "role": "planner",
            "target_repo": tenant.repo,
            "issue_id": 77,
            "status": "pending",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    # 1. Heartbeat marks pending session dispatched and upserts active session
    r_hb = client.post(
        f"/internal/v1/runs/{run_id}/heartbeat",
        headers=headers,
        json={"status": "Running", "instance_id": "i-warm-77", "issue_id": 77},
    )
    assert r_hb.status_code == 200
    assert store.sessions[f"pending-{run_id}"]["status"] == "dispatched"
    assert store.sessions["i-warm-77"]["status"] == "Running"

    # 2. Run events: > 500 returns 413; <= 500 deduplicates on (run_id, seq)
    r_too_many = client.post(
        f"/internal/v1/runs/{run_id}/events",
        headers=headers,
        json={"events": [{"seq": i, "kind": "log", "payload": {}} for i in range(501)]},
    )
    assert r_too_many.status_code == 413

    r_evts = client.post(
        f"/internal/v1/runs/{run_id}/events",
        headers=headers,
        json={
            "events": [
                {"seq": 1, "kind": "step", "payload": {"msg": "one"}},
                {"seq": 2, "kind": "step", "payload": {"msg": "two"}},
            ]
        },
    )
    assert r_evts.status_code == 200
    # Replay seq=1 + new seq=3
    r_evts_dup = client.post(
        f"/internal/v1/runs/{run_id}/events",
        headers=headers,
        json={
            "events": [
                {"seq": 1, "kind": "step", "payload": {"msg": "one-dup"}},
                {"seq": 3, "kind": "step", "payload": {"msg": "three"}},
            ]
        },
    )
    assert r_evts_dup.status_code == 200
    recorded_seqs = [e["seq"] for e in store.run_events_for_run(run_id)]
    assert recorded_seqs == [1, 2, 3]

    # 3. Turn-complete warm handover (Planner -> Developer)
    plan_env = render(
        Plan(
            tenant=tenant.repo,
            pr=77,
            head_sha="0" * 40,
            run_id=run_id,
            complexity="single_pr",
            goal="Implement feature",
            scope_files=["src/main.py"],
            acceptance_criteria=[PlannedCriterion(id="AC-1", description="Works")],
            implementation_steps=["Edit src/main.py"],
        )
    )
    r_tc_warm = client.post(
        f"/internal/v1/runs/{run_id}/turn-complete",
        headers=headers,
        json={
            "comment_id": 1001,
            "comment_body": plan_env,
            "head_sha": "0" * 40,
            "issue_number": 77,
            "instance_id": "i-warm-77",
        },
    )
    assert r_tc_warm.status_code == 200
    tc_data = r_tc_warm.json()
    assert tc_data["action"] == "continue"
    assert tc_data["role"] == "developer"
    assert len(ec2.calls) == 0

    # Turn-complete terminate when Evaluator approves but required check 'ci/test' is still pending
    eval_run_id = "run-eval-77"
    store.create_run_credential(eval_run_id, tenant.id, "evaluator", "h-eval", "2099-01-01T00:00:00Z")
    verdict_env = render(
        Verdict(
            tenant=tenant.repo,
            pr=77,
            round=1,
            head_sha="a" * 40,
            run_id=eval_run_id,
            verdict="approved",
            findings=[],
        )
    )
    r_tc_term = client.post(
        f"/internal/v1/runs/{eval_run_id}/turn-complete",
        headers=headers,
        json={
            "comment_id": 1002,
            "comment_body": verdict_env,
            "head_sha": "a" * 40,
            "issue_number": 77,
            "instance_id": "i-warm-77",
        },
    )
    assert r_tc_term.status_code == 200
    assert r_tc_term.json()["action"] in ("terminate", "shutdown")
    assert r_tc_term.json()["action"] == "terminate"

    # 4. Arbitration decisions persists manifest_sha
    r_dec = client.post(
        f"/internal/v1/runs/{eval_run_id}/arbitration-decisions",
        headers=headers,
        json={
            "pr_number": 77,
            "head_sha": "a" * 40,
            "mode": "shadow",
            "decision": "pass",
            "reasons": "All metrics passed",
        },
    )
    assert r_dec.status_code == 200
    assert len(store.arbitration_decisions) == 1
    assert store.arbitration_decisions[0]["manifest_sha"] == manifest_sha

    # 5. Arbitration data returns tenant_id, arbitration_mode, manifest, manifest_sha, and recent_observations
    store.record_observations(
        [
            {
                "tenant_id": tenant.id,
                "key": "unit_test_pass_rate",
                "value": 0.5,
                "commit_sha": "a" * 40,
                "pr_number": 77,
                "run_id": 500,
                "run_attempt": 1,
                "observed_at": "2026-04-19T10:00:00+00:00",
            },
            {
                "tenant_id": tenant.id,
                "key": "unit_test_pass_rate",
                "value": 1.0,
                "commit_sha": "a" * 40,
                "pr_number": 77,
                "run_id": 500,
                "run_attempt": 2,
                "observed_at": "2026-04-19T10:05:00+00:00",
            },
        ]
    )
    r_arb_data = client.get(f"/internal/v1/runs/{eval_run_id}/arbitration-data", headers=headers)
    assert r_arb_data.status_code == 200
    arb_json = r_arb_data.json()
    assert arb_json["tenant_id"] == tenant.id
    assert arb_json["arbitration_mode"] == tenant.arbitration_mode
    assert arb_json["manifest_sha"] == manifest_sha
    assert arb_json["manifest"]["required_checks"] == ["ci/test"]
    assert [o["run_attempt"] for o in arb_json["recent_observations"]] == [1, 2]
    assert arb_json["recent_observations"][1]["observed_at"] == "2026-04-19T10:05:00+00:00"

    # 6. Wiki GET (404 before creation) & POST (onboarder only) -> GET roundtrip
    assert client.get(f"/internal/v1/runs/{eval_run_id}/wiki", headers=headers).status_code == 404
    assert (
        client.post(
            f"/internal/v1/runs/{eval_run_id}/wiki",
            headers=headers,
            json={"purpose": "Denied for evaluator"},
        ).status_code
        == 403
    )
    onb_run_id = "run-onb-77"
    store.create_run_credential(onb_run_id, tenant.id, "onboarder", "h-onb", "2099-01-01T00:00:00Z")
    r_wiki_post = client.post(
        f"/internal/v1/runs/{onb_run_id}/wiki",
        headers=headers,
        json={"purpose": "Service cutover test", "technologies": ["Python", "FastAPI"]},
    )
    assert r_wiki_post.status_code == 200
    r_wiki_get = client.get(f"/internal/v1/runs/{eval_run_id}/wiki", headers=headers)
    assert r_wiki_get.status_code == 200
    assert r_wiki_get.json()["purpose"] == "Service cutover test"


def test_cp_authored_metrics_lifecycle_and_guardrails(fe_fixture):
    client, store, ec2, gh, headers = fe_fixture

    # 1. Create tenant via POST /internal/v1/tenants
    r_create = client.post(
        "/internal/v1/tenants",
        headers=headers,
        json={"repo": "arsenyspb/ollama-monitor", "created_by": "arsenyspb"},
    )
    assert r_create.status_code in (200, 201)
    tenant_id = r_create.json()["tenant_id"]

    # 2. Update arbitration mode
    r_mode = client.put(
        f"/internal/v1/tenants/{tenant_id}/arbitration-mode",
        headers=headers,
        json={"mode": "recommend", "actor": "arsenyspb"},
    )
    assert r_mode.status_code == 200
    assert store.tenant_by_id(tenant_id).arbitration_mode == "recommend"

    # 3. Draft metric via PUT /internal/v1/tenants/{tenant_id}/metrics/{key}
    r_draft = client.put(
        f"/internal/v1/tenants/{tenant_id}/metrics/test_pass_rate",
        headers=headers,
        json={
            "intent": "Pytest unit test pass rate must be 100%",
            "kind": "guardrail",
            "rationale": "Prevent broken builds",
            "actor": "arsenyspb",
        },
    )
    assert r_draft.status_code == 200
    assert store.get_metric_definition(tenant_id, "test_pass_rate")["status"] == "draft"

    # 4. Trigger compilation via POST /internal/v1/tenants/{tenant_id}/metrics/{key}/compile
    r_comp_trigger = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/test_pass_rate/compile",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    assert r_comp_trigger.status_code in (200, 202)
    comp_run_id = r_comp_trigger.json()["run_id"]
    assert store.get_run_credential(comp_run_id)["role"] == "metric_compiler"
    assert len(ec2.calls) == 1

    # 5. Unsafe compiled command referencing secret env vars or mutating protected paths is rejected with 422
    r_unsafe_secret = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "test_pass_rate",
            "compiled": {
                "run": "echo $SUPABASE_SERVICE_KEY && pytest -q",
                "comparator": ">=",
                "threshold": 1.0,
            },
            "value": 1.0,
            "sha": "b" * 40,
            "stdout_tail": "1.0",
        },
    )
    assert r_unsafe_secret.status_code == 422

    r_unsafe_path = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "test_pass_rate",
            "compiled": {
                "run": "rm -rf .github/workflows/flow-metrics.yml && echo 1.0",
                "comparator": ">=",
                "threshold": 1.0,
            },
            "value": 1.0,
            "sha": "b" * 40,
            "stdout_tail": "1.0",
        },
    )
    assert r_unsafe_path.status_code == 422
    assert store.get_metric_definition(tenant_id, "test_pass_rate")["status"] == "draft"

    # 6. Safe compiled command stores status='compiled', compiled, digest, dry_run
    r_safe = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "test_pass_rate",
            "compiled": {
                "run": "pytest -q | tail -n1",
                "comparator": ">=",
                "threshold": 1.0,
                "baseline_mode": "absolute",
                "min_samples": 1,
            },
            "value": 1.0,
            "sha": "b" * 40,
            "stdout_tail": "1.0\n",
        },
    )
    assert r_safe.status_code == 200
    digest = r_safe.json()["digest"]
    assert digest
    defn = store.get_metric_definition(tenant_id, "test_pass_rate")
    assert defn["status"] == "compiled"
    assert defn["digest"] == digest
    assert defn["dry_run"]["value"] == 1.0

    # 7. Approve with mismatched digest returns 409 Conflict
    r_bad_approve = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/test_pass_rate/approve",
        headers=headers,
        json={"digest": "wrong-digest-000", "actor": "arsenyspb"},
    )
    assert r_bad_approve.status_code == 409
    assert store.get_metric_definition(tenant_id, "test_pass_rate")["status"] == "compiled"

    # 8. Approve with matching digest promotes to active, rebuilds manifest + manifest_sha, and opens PR
    r_approve = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/test_pass_rate/approve",
        headers=headers,
        json={"digest": digest, "actor": "arsenyspb"},
    )
    assert r_approve.status_code == 200
    approved_body = r_approve.json()
    assert store.get_metric_definition(tenant_id, "test_pass_rate")["status"] == "active"
    updated_tenant = store.tenant_by_id(tenant_id)
    assert [m["key"] for m in updated_tenant.manifest["metrics"]] == ["test_pass_rate"]
    assert approved_body["manifest_sha"] == canonical_sha(updated_tenant.manifest)
    assert len(gh.opened_prs) == 1
    assert "test_pass_rate" in gh.opened_prs[0]["workflow_yaml"]
    assert any(h["manifest_sha"] == approved_body["manifest_sha"] for h in store.tenants_manifest_history)

    # 9. Retire metric marks status='retired' and rebuilds manifest
    r_retire = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/test_pass_rate/retire",
        headers=headers,
    )
    assert r_retire.status_code == 200
    assert store.get_metric_definition(tenant_id, "test_pass_rate")["status"] == "retired"
    assert store.tenant_by_id(tenant_id).manifest["metrics"] == []


def test_serverless_arbitrator_rerun_attempt_and_fail_closed_guards(fe_fixture):
    from atobar_contracts.envelopes import Ack

    client, store, ec2, gh, headers = fe_fixture

    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    store.set_arbitration_mode(tenant.id, "recommend")
    store.upsert_metric_definition(
        tenant.id,
        {
            "key": "unit_test_pass_rate",
            "status": "active",
            "source": {"kind": "command", "run": "pytest -q"},
            "comparator": ">=",
            "threshold": 1.0,
            "min_samples": 1,
        },
    )
    manifest, _ = store.rebuild_tenant_manifest(tenant.id)
    manifest["required_checks"] = ["ci/build"]
    manifest["arbitration"] = {"mode": "recommend"}
    manifest_sha = canonical_sha(manifest)
    store.update_tenant_manifest(tenant.id, manifest, manifest_sha)

    sha = "a" * 40
    # 1. Fail-closed guard: missing run_id in /internal/v1/metrics is rejected with 422
    r_no_run = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha,
            "run_id": None,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0},
        },
    )
    assert r_no_run.status_code == 422

    # 2. Fail-closed guard: tampered values in request body mismatching verified GitHub artifact -> 422
    gh.runs[(tenant.repo, 700)] = {
        "id": 700,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 700)] = _make_metrics_zip(sha, {"unit_test_pass_rate": 0.0}, run_attempt=1)
    r_tamper = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha,
            "pr_number": 88,
            "run_id": 700,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0},
        },
    )
    assert r_tamper.status_code == 422
    assert store.observations == []

    # 3. Record attempt 1 (failing: 0.0) and attempt 2 (passing: 1.0) on PR #88 at the same head_sha
    r_att1 = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha,
            "pr_number": 88,
            "run_id": 700,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 0.0},
        },
    )
    assert r_att1.status_code == 201

    gh.runs[(tenant.repo, 700)]["run_attempt"] = 2
    gh.artifacts[(tenant.repo, 700)] = _make_metrics_zip(sha, {"unit_test_pass_rate": 1.0}, run_attempt=2)
    r_att2 = client.post(
        "/internal/v1/metrics",
        headers=headers,
        json={
            "tenant_id": tenant.id,
            "commit_sha": sha,
            "pr_number": 88,
            "run_id": 700,
            "run_attempt": 2,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0},
        },
    )
    assert r_att2.status_code == 201

    # 4. Trigger check_run.completed via POST /internal/v1/events -> serverless arbitrator evaluates attempt 2 -> pass
    store.create_run_credential("run-dev-88", tenant.id, "developer", "h1", "2099-01-01T00:00:00Z", head_sha=sha)
    store.create_run_credential("run-eval-88", tenant.id, "evaluator", "h2", "2099-01-01T00:00:00Z", head_sha=sha)
    store.claim_run_credential_envelope("run-dev-88", 1)
    store.claim_run_credential_envelope("run-eval-88", 2)
    ack = Ack(tenant=tenant.repo, pr=88, head_sha=sha, run_id="run-dev-88")
    verdict = Verdict(
        tenant=tenant.repo,
        pr=88,
        round=1,
        head_sha=sha,
        run_id="run-eval-88",
        verdict="approved",
        findings=[],
    )
    gh.comments[(tenant.repo, 88)] = [
        {"id": 1, "user": {"login": "atobar-flow-agent[bot]"}, "body": render(ack)},
        {"id": 2, "user": {"login": "atobar-flow-agent[bot]"}, "body": render(verdict)},
    ]
    gh.check_runs[(tenant.repo, sha)] = [{"name": "ci/build", "conclusion": "success"}]

    r_chk = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": tenant.repo,
            "delivery_id": "deliv-chk-88",
            "event": {
                "kind": "check_run.completed",
                "pr_number": 88,
                "check_name": "ci/build",
                "conclusion": "success",
                "head_sha": sha,
            },
        },
    )
    assert r_chk.status_code == 200
    assert r_chk.json()["new_status"] == "converged"
    assert len(ec2.calls) == 0
    assert len(store.arbitration_decisions) == 1
    assert store.arbitration_decisions[0]["decision"] == "pass"
    assert store.arbitration_decisions[0]["manifest_sha"] == manifest_sha


def test_devin_review_fixes_all_covered(fe_fixture):
    from flow_engine.store import SupabaseStore

    client, store, ec2, gh, headers = fe_fixture

    # 1. BUG_0001: SupabaseStore.tenant_by_id rejects non-UUID strings before hitting PostgREST
    supa = SupabaseStore("https://example.supabase.co", "fake-service-key")
    assert supa.tenant_by_id("arsenyspb/ollama-monitor") is None

    # 2. SEC_0002: Tenant creation with dispatch_onboard=True rejects missing or unauthorized actor
    r_unauth_onboard = client.post(
        "/internal/v1/tenants",
        headers=headers,
        json={"repo": "arsenyspb/ollama-monitor", "created_by": "intruder", "dispatch_onboard": True},
    )
    assert r_unauth_onboard.status_code == 403

    r_auth_create = client.post(
        "/internal/v1/tenants",
        headers=headers,
        json={"repo": "arsenyspb/ollama-monitor", "created_by": "arsenyspb", "dispatch_onboard": True},
    )
    assert r_auth_create.status_code == 201
    tenant_id = r_auth_create.json()["tenant_id"]

    # 3. SEC_0001: Cross-tenant X-Tenant-ID / X-Tenant-Repo header mismatch is rejected with 403
    r_cross = client.put(
        f"/internal/v1/tenants/{tenant_id}/arbitration-mode",
        headers={**headers, "X-Tenant-Repo": "other/repo"},
        json={"mode": "recommend"},
    )
    assert r_cross.status_code == 403

    # 4. SEC_0005: Unknown run_id on /arbitration-decisions returns 404 even when payload.tenant_id is passed
    r_forged_dec = client.post(
        "/internal/v1/runs/nonexistent-run-999/arbitration-decisions",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "pr_number": 1,
            "head_sha": "a" * 40,
            "mode": "shadow",
            "decision": "pass",
        },
    )
    assert r_forged_dec.status_code == 404

    # 5. BUG_0004, BUG_0005, SEC_0003: Metric compilation validation, key binding, and tolerance_pct preservation
    client.put(
        f"/internal/v1/tenants/{tenant_id}/metrics/p95_latency",
        headers=headers,
        json={"intent": "p95 latency no regression within 5%", "kind": "guardrail", "actor": "arsenyspb"},
    )
    client.put(
        f"/internal/v1/tenants/{tenant_id}/metrics/other_metric",
        headers=headers,
        json={"intent": "other metric", "kind": "guardrail", "actor": "arsenyspb"},
    )
    r_comp = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/p95_latency/compile",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    comp_run_id = r_comp.json()["run_id"]

    # SEC_0003: Submitting compilation for 'other_metric' using run bound to 'p95_latency' -> 403
    r_wrong_key = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "other_metric",
            "compiled": {"run": "echo 1", "comparator": "<=", "threshold": 10.0},
            "value": 1.0,
            "sha": "b" * 40,
        },
    )
    assert r_wrong_key.status_code == 403

    # BUG_0004: Non-numeric threshold returns 422 (not 500)
    r_bad_thr = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "p95_latency",
            "compiled": {"run": "echo 1", "comparator": "<=", "threshold": "not-a-number"},
            "value": 1.0,
            "sha": "b" * 40,
        },
    )
    assert r_bad_thr.status_code == 422

    # BUG_0005: Compile & approve with tolerance_pct=5.0 preserves tolerance_pct in manifest
    r_ok_comp = client.post(
        f"/internal/v1/runs/{comp_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "p95_latency",
            "compiled": {
                "run": "./bench.sh",
                "comparator": "<=",
                "threshold": 100.0,
                "baseline_mode": "no_regression",
                "tolerance_pct": 5.0,
                "min_samples": 2,
            },
            "value": 50.0,
            "sha": "b" * 40,
        },
    )
    assert r_ok_comp.status_code == 200
    digest = r_ok_comp.json()["digest"]

    r_app = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/p95_latency/approve",
        headers=headers,
        json={"digest": digest, "actor": "arsenyspb"},
    )
    assert r_app.status_code == 200
    metrics_in_manifest = store.tenant_by_id(tenant_id).manifest["metrics"]
    assert metrics_in_manifest[0]["tolerance_pct"] == 5.0

    # 6. BUG_0003: Reposting the exact same default-branch run does not duplicate observations or prematurely activate
    sha1 = "1" * 40
    zip1 = _make_metrics_zip(sha1, {"p95_latency": 50.0}, run_attempt=1)
    gh.runs[("arsenyspb/ollama-monitor", 801)] = {
        "id": 801,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha1,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[("arsenyspb/ollama-monitor", 801)] = zip1

    for _ in range(2):
        r_m = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant_id": tenant_id,
                "commit_sha": sha1,
                "pr_number": None,
                "run_id": 801,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": {"p95_latency": 50.0},
            },
        )
        assert r_m.status_code == 201
        assert r_m.json()["activated"] is False
    assert len(store.observations) == 1
    assert store.tenant_by_id(tenant_id).status == "onboarding"

    # 7. BUG_0008: Target PR observations are preserved even when >50 newer observations arrive on another PR
    target_pr_sha = "9" * 40
    store.record_observations(
        [
            {
                "tenant_id": tenant_id,
                "key": "p95_latency",
                "value": 48.0,
                "commit_sha": target_pr_sha,
                "pr_number": 12,
                "run_id": 900,
                "run_attempt": 1,
                "observed_at": "2026-04-19T09:00:00+00:00",
            }
        ]
    )
    for idx in range(55):
        store.record_observations(
            [
                {
                    "tenant_id": tenant_id,
                    "key": "p95_latency",
                    "value": 60.0,
                    "commit_sha": f"{idx:040d}",
                    "pr_number": 99,
                    "run_id": 1000 + idx,
                    "run_attempt": 1,
                    "observed_at": f"2026-04-19T10:{idx:02d}:00+00:00",
                }
            ]
        )
    arb_run_id = "run-arb-pr12"
    store.create_run_credential(arb_run_id, tenant_id, "arbitrator", "h-arb", "2099-01-01T00:00:00Z")
    store.upsert_session(
        {
            "instance_id": "i-arb-12",
            "run_id": arb_run_id,
            "tenant_id": tenant_id,
            "role": "arbitrator",
            "target_repo": "arsenyspb/ollama-monitor",
            "issue_id": 12,
            "head_sha": target_pr_sha,
            "status": "Running",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    r_arb12 = client.get(f"/internal/v1/runs/{arb_run_id}/arbitration-data", headers=headers)
    assert r_arb12.status_code == 200
    assert any(o["commit_sha"] == target_pr_sha and o["pr_number"] == 12 for o in r_arb12.json()["pr_observations"])

    # 8. BUG_0007: Retiring a metric renders the updated workflow and opens/updates the policy PR
    prs_before = len(gh.opened_prs)
    r_ret = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/p95_latency/retire",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    assert r_ret.status_code == 200
    assert r_ret.json()["workflow_yaml"] != ""
    assert len(gh.opened_prs) == prs_before + 1
    assert "p95_latency" not in gh.opened_prs[-1]["workflow_yaml"]

    # 9. Retryable approval after transient GitHub PR failure & X-Run-Credential required for github-token
    r_comp2 = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/other_metric/compile",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    comp2_run_id = r_comp2.json()["run_id"]
    r_sub2 = client.post(
        f"/internal/v1/runs/{comp2_run_id}/metric-compilation",
        headers=headers,
        json={
            "key": "other_metric",
            "compiled": {"run": "echo 1", "comparator": ">=", "threshold": 1.0, "tolerance_pct": 2.5},
            "value": 1.0,
            "sha": "b" * 40,
        },
    )
    assert r_sub2.status_code == 200
    assert "tolerance_pct" not in store.get_metric_definition(tenant_id, "other_metric")
    assert store.get_metric_definition(tenant_id, "other_metric")["compiled"]["tolerance_pct"] == 2.5
    digest2 = r_sub2.json()["digest"]

    orig_open_pr = gh.open_or_update_metrics_pr

    def _fail_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("transient GitHub 503")

    gh.open_or_update_metrics_pr = _fail_once  # type: ignore[method-assign]
    r_app_fail = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/other_metric/approve",
        headers=headers,
        json={"digest": digest2, "actor": "arsenyspb"},
    )
    assert r_app_fail.status_code == 502
    gh.open_or_update_metrics_pr = orig_open_pr  # type: ignore[method-assign]

    # Retrying with the same digest succeeds and opens the workflow PR
    r_app_retry = client.post(
        f"/internal/v1/tenants/{tenant_id}/metrics/other_metric/approve",
        headers=headers,
        json={"digest": digest2, "actor": "arsenyspb"},
    )
    assert r_app_retry.status_code == 200
    assert "other_metric" in gh.opened_prs[-1]["workflow_yaml"]

    # Missing X-Run-Credential on /github-token returns 403
    r_no_run_cred = client.post(f"/internal/v1/runs/{comp2_run_id}/github-token", headers=headers)
    assert r_no_run_cred.status_code == 403


def test_plan_approve_and_repeated_resume_and_merged_pr_shipped(fe_fixture) -> None:
    client, store, ec2, gh, headers = fe_fixture
    repo = "owner/repo"

    # 1. Creating a tenant seeds repository labels automatically and defaults assignee to creator
    r_create = client.post(
        "/internal/v1/tenants",
        headers=headers,
        json={"repo": repo, "created_by": "owner", "dispatch_onboard": False},
    )
    assert r_create.status_code == 201
    assert repo in gh.seeded_repo_labels
    onb_tenant = store.tenant_by_repo(repo)
    assert onb_tenant is not None
    assert onb_tenant.manifest.get("owner_github_login") == "owner"

    # 2. Onboarding tenant removes premature work labels and upserts a single clickable comment
    for lbl in ("flow:feature", "flow:fix"):
        gh.add_label(repo, 77, lbl)
        r_onb_lbl = client.post(
            "/internal/v1/events",
            headers=headers,
            json={"tenant": repo, "event": {"kind": "issues.labeled", "issue_number": 77, "label": lbl}},
        )
        assert r_onb_lbl.status_code == 200
        assert r_onb_lbl.json()["dispatched"] is False
        assert lbl not in gh.labels.get((repo, 77), set())
    comments_77 = gh.get_comments(repo, 77)
    assert len(comments_77) == 1, "Double-clicking work labels during onboarding must update the hold comment in-place"
    assert f"https://cp.example.invalid/tenants/{repo}" in comments_77[0]["body"]

    # Activate tenant and configure custom human collaborator assignee
    store.update_tenant_status(onb_tenant.id, "active")
    r_assign = client.patch(
        f"/internal/v1/tenants/{onb_tenant.id}/assignee",
        headers=headers,
        json={"owner_github_login": "collab-bob", "actor": "arsenyspb"},
    )
    assert r_assign.status_code == 200
    assert r_assign.json()["owner_github_login"] == "collab-bob"

    # 3. flow:approved on an onboarding/metrics issue in flow:needs-approval must NEVER dispatch developer
    gh.add_label(repo, 9, "flow:onboard")
    gh.add_label(repo, 9, "flow:needs-approval")
    r_onb_appr = client.post(
        "/internal/v1/events",
        headers=headers,
        json={"tenant": repo, "event": {"kind": "issues.labeled", "issue_number": 9, "label": "flow:approved"}},
    )
    assert r_onb_appr.status_code == 200
    assert r_onb_appr.json()["dispatched"] is False

    # 4. flow:plan -> flow:needs-approval -> flow:approved -> developer dispatch
    gh.add_label(repo, 88, "flow:plan")
    r_plan = client.post(
        "/internal/v1/events",
        headers=headers,
        json={"tenant": repo, "event": {"kind": "issues.labeled", "issue_number": 88, "label": "flow:plan"}},
    )
    assert r_plan.status_code == 200
    assert r_plan.json()["dispatched"] is True
    assert r_plan.json()["role"] == "planner"

    # Simulate planner finishing with flow:needs-approval set on issue 88
    store.update_session(f"pending-{r_plan.json()['run_id']}", {"status": "Complete"})
    gh.add_label(repo, 88, "flow:needs-approval")
    gh.add_assignees(repo, 88, ["collab-bob"])

    gh.add_label(repo, 88, "flow:approved")
    r_appr = client.post(
        "/internal/v1/events",
        headers=headers,
        json={"tenant": repo, "event": {"kind": "issues.labeled", "issue_number": 88, "label": "flow:approved"}},
    )
    assert r_appr.status_code == 200
    assert r_appr.json()["dispatched"] is True
    assert r_appr.json()["role"] == "developer"
    assert "flow:approved" not in gh.labels.get((repo, 88), set())
    assert "flow:needs-approval" not in gh.labels.get((repo, 88), set())
    assert "flow:plan" not in gh.labels.get((repo, 88), set())
    assert "collab-bob" not in gh.assignees.get((repo, 88), set())

    # 5. Escalate on max_pr_cost_usd -> flow:resume resets epoch spend so next round does NOT re-escalate
    store.update_session(f"pending-{r_appr.json()['run_id']}", {"status": "Halted"})
    client.put(
        f"/internal/v1/tenants/{onb_tenant.id}/guardrails",
        headers=headers,
        json={
            "spot_max_instance_minutes": 60,
            "max_pr_wall_clock_minutes": 240,
            "max_pr_cost_usd": 2.0,
            "max_review_rounds": 4,
            "max_repeated_finding": 2,
            "actor": "arsenyspb",
        },
    )
    # Record a round that exceeded the $2.00 cost cap before resume
    store.record_loop_round(
        {
            "tenant_id": onb_tenant.id,
            "pr_number": 88,
            "round": 1,
            "head_sha": "a" * 40,
            "role": "evaluator",
            "tokens": 5000,
            "cost_usd": 2.15,
            "wall_clock_minutes": 3.0,
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        }
    )
    for round_idx in (1, 2):
        gh.add_label(repo, 88, "flow:escalated")
        gh.add_assignees(repo, 88, ["collab-bob"])
        gh.add_label(repo, 88, "flow:resume")
        r_res = client.post(
            "/internal/v1/events",
            headers=headers,
            json={"tenant": repo, "event": {"kind": "issues.labeled", "issue_number": 88, "label": "flow:resume"}},
        )
        assert r_res.status_code == 200, f"round {round_idx} failed"
        assert r_res.json()["dispatched"] is True
        assert r_res.json()["role"] == "developer"
        assert "flow:resume" not in gh.labels.get((repo, 88), set())
        assert "flow:escalated" not in gh.labels.get((repo, 88), set())
        assert "collab-bob" not in gh.assignees.get((repo, 88), set())
        store.update_session(f"pending-{r_res.json()['run_id']}", {"status": "Halted"})

    # Evaluator turn after flow:resume with $0.50 cost must NOT trip max_pr_cost_usd ($2.00) because epoch reset spend
    eval_run_after_resume = "run-eval-after-resume-88"
    store.create_run_credential(eval_run_after_resume, onb_tenant.id, "evaluator", "h", "2099-01-01T00:00:00Z", head_sha="a" * 40)
    verdict_comment = render(
        Verdict(
            tenant=repo,
            pr=88,
            head_sha="a" * 40,
            run_id=eval_run_after_resume,
            round=1,
            verdict="changes_requested",
            findings=[],
        )
    )
    gh.comments.setdefault((repo, 88), []).append(
        {
            "id": 9001,
            "user": {"login": "atobar-flow-agent[bot]"},
            "body": verdict_comment,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    r_post_resume_eval = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": repo,
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 88,
                "sender": "atobar-flow-agent[bot]",
                "comment_id": 9001,
                "comment_body": verdict_comment,
                "cost_usd": 0.50,
            },
        },
    )
    assert r_post_resume_eval.status_code == 200
    assert r_post_resume_eval.json()["new_status"] == "in_dev", "Epoch reset must clear prior $2.15 spend"

    # 6. Merging an unrelated human PR gets NO flow:shipped label; merging a factory PR labels the delegated issue
    r_human_pr = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": repo,
            "event": {
                "kind": "pull_request.closed",
                "pr_number": 200,
                "merged": True,
                "head_ref": "chore/readme-typo",
                "body": "Small typo fix by human",
            },
        },
    )
    assert r_human_pr.status_code == 200
    assert r_human_pr.json()["status"] == "ignored"
    assert "flow:shipped" not in gh.labels.get((repo, 200), set())

    # Factory PR #105 fixing issue #88 transitions issue #88 to flow:shipped and unassigns collab-bob
    gh.add_label(repo, 88, "flow:in-review")
    gh.add_assignees(repo, 88, ["collab-bob"])
    r_merged = client.post(
        "/internal/v1/events",
        headers=headers,
        json={
            "tenant": repo,
            "event": {
                "kind": "pull_request.closed",
                "pr_number": 105,
                "merged": True,
                "head_ref": "flow-88-fix-overflow",
                "body": "Fixes #88",
            },
        },
    )
    assert r_merged.status_code == 200
    assert r_merged.json()["new_status"] == "shipped"
    assert r_merged.json()["issue_number"] == 88
    assert "flow:shipped" in gh.labels.get((repo, 88), set())
    assert "flow:in-review" not in gh.labels.get((repo, 88), set())
    assert "collab-bob" not in gh.assignees.get((repo, 88), set())


def test_j50_wiki_coalesced_refresh_queue_and_auto_refresh(fe_fixture: Any) -> None:
    """J50 AC-4 & AC-5: Coalesced single-flight wiki refresh queue + default-branch merge auto-refresh."""
    client, store, ec2, gh, headers = fe_fixture
    repo = "arsenyspb/ollama-monitor"
    tenant = store.create_tenant(repo, "arsenyspb")
    store.upsert_metric_definition(
        tenant.id,
        {
            "key": "coverage_pct",
            "status": "active",
            "run": "pytest",
            "comparator": ">=",
            "threshold": 80.0,
            "min_samples": 1,
        },
    )

    # 1. Update policy to refresh every 2 merges
    r_pol = client.post(
        f"/internal/v1/tenants/{tenant.id}/wiki/policy",
        headers=headers,
        json={"refresh_interval_merges": 2, "actor": "arsenyspb"},
    )
    assert r_pol.status_code == 200
    assert r_pol.json()["refresh_interval_merges"] == 2

    # 2. First manual regenerate dispatches a Spot VM with issue_id=0 and role=onboarder
    r_regen1 = client.post(
        f"/internal/v1/tenants/{tenant.id}/wiki/regenerate",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    assert r_regen1.status_code == 200
    data1 = r_regen1.json()
    assert data1["status"] == "dispatched"
    assert data1["dispatched"] is True
    run1 = data1["run_id"]
    assert len(ec2.calls) == 1
    assert store.wiki_for_tenant(tenant.id)["refresh_status"] == "running"

    # 3. Concurrent regenerate requests while run1 is active coalesce into refresh_queued=True (no new Spot VM)
    for _ in range(3):
        r_queued = client.post(
            f"/internal/v1/tenants/{tenant.id}/wiki/regenerate",
            headers=headers,
            json={"actor": "arsenyspb"},
        )
        assert r_queued.status_code == 200
        assert r_queued.json()["status"] == "queued"
        assert r_queued.json()["dispatched"] is False
    assert len(ec2.calls) == 1
    w_queued = store.wiki_for_tenant(tenant.id)
    assert w_queued["refresh_queued"] is True
    assert w_queued["refresh_status"] == "queued"

    # 4. Run 1 publishes wiki and completes via heartbeat -> automatically drains queue by launching Run 2 once!
    r_wiki_post = client.post(
        f"/internal/v1/runs/{run1}/wiki",
        headers=headers,
        json={
            "purpose": "Monitor Ollama",
            "technologies": ["Python"],
            "structure": "src/",
            "concepts": ["TUI"],
            "architecture_mermaid": "flowchart TD\n  A --> B",
            "commit_sha": "1" * 40,
        },
    )
    assert r_wiki_post.status_code == 200

    r_hb_done = client.post(
        f"/internal/v1/runs/{run1}/heartbeat",
        headers=headers,
        json={"status": "Complete", "instance_id": "i-spot-0001", "issue_id": 0, "role": "onboarder"},
    )
    assert r_hb_done.status_code == 200
    assert len(ec2.calls) == 2, "Finishing run1 while refresh_queued=True must launch exactly one follow-up Spot VM"
    w_after_drain = store.wiki_for_tenant(tenant.id)
    assert w_after_drain["refresh_queued"] is False
    assert w_after_drain["refresh_status"] == "running"

    # Retrying Run 1's completion (or turn-complete) while Run 2 is running (even after queueing Run 3) is a no-op!
    active_onb = store.active_session_for_role(tenant.id, "onboarder")
    assert active_onb is not None
    run2 = active_onb["run_id"]
    client.post(
        f"/internal/v1/tenants/{tenant.id}/wiki/regenerate",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    assert store.wiki_for_tenant(tenant.id)["refresh_queued"] is True
    r_retry_run1 = client.post(
        f"/internal/v1/runs/{run1}/heartbeat",
        headers=headers,
        json={"status": "Complete", "instance_id": "i-spot-0001", "issue_id": 0, "role": "onboarder"},
    )
    assert r_retry_run1.status_code == 200
    assert len(ec2.calls) == 2, "Retried completion of old run1 must not launch a duplicate VM or consume run2's queue"
    assert store.wiki_for_tenant(tenant.id)["refresh_queued"] is True
    assert store.wiki_for_tenant(tenant.id)["refresh_run_id"] == run2

    # Complete Run 2 -> drains the queued Run 3 -> then complete Run 3 so status returns to idle
    r_tc2 = client.post(
        f"/internal/v1/runs/{run2}/turn-complete",
        headers=headers,
        json={"comment_body": "", "issue_number": 0, "instance_id": "i-spot-0002"},
    )
    assert r_tc2.status_code == 200
    assert len(ec2.calls) == 3
    run3 = store.wiki_for_tenant(tenant.id)["refresh_run_id"]
    assert run3 and run3 != run2
    r_tc3 = client.post(
        f"/internal/v1/runs/{run3}/turn-complete",
        headers=headers,
        json={"comment_body": "", "issue_number": 0, "instance_id": "i-spot-0003"},
    )
    assert r_tc3.status_code == 200
    assert store.wiki_for_tenant(tenant.id)["refresh_status"] == "idle"

    # 5. Default-branch metric observations increment merges_since_refresh (idempotent per commit_sha)
    store.rebuild_tenant_manifest(tenant.id)

    def _post_main_metrics(sha: str, run_id_num: int) -> dict[str, Any]:
        gh.runs[(repo, run_id_num)] = {
            "id": run_id_num,
            "head_sha": sha,
            "event": "push",
            "path": ".github/workflows/flow-metrics.yml",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
        gh.artifacts[(repo, run_id_num)] = _make_metrics_zip(sha, {"coverage_pct": 85.0}, run_attempt=1)
        resp = client.post(
            "/internal/v1/metrics",
            headers=headers,
            json={
                "tenant_id": tenant.id,
                "commit_sha": sha,
                "run_id": run_id_num,
                "run_attempt": 1,
                "workflow_path": ".github/workflows/flow-metrics.yml",
                "values": {"coverage_pct": 85.0},
            },
        )
        assert resp.status_code == 201
        return resp.json()

    # Merge 1 (sha "2"*40) -> merges_since_refresh == 1 (below interval 2)
    _post_main_metrics("2" * 40, 501)
    assert store.wiki_for_tenant(tenant.id)["merges_since_refresh"] == 1
    assert len(ec2.calls) == 3

    # Duplicate CI run on same sha "2"*40 -> still 1
    _post_main_metrics("2" * 40, 502)
    assert store.wiki_for_tenant(tenant.id)["merges_since_refresh"] == 1

    # Merge 2 (sha "3"*40) -> reaches interval 2 -> auto-dispatches wiki refresh!
    out_m2 = _post_main_metrics("3" * 40, 503)
    assert out_m2.get("wiki_refresh", {}).get("dispatched") is True
    assert len(ec2.calls) == 4

    # 6. Failing auto-refresh resets merges_since_refresh=0 so the next single merge does NOT relaunch Spot
    active_auto = store.active_session_for_role(tenant.id, "onboarder")
    assert active_auto is not None
    run_auto = active_auto["run_id"]
    r_hb_fail = client.post(
        f"/internal/v1/runs/{run_auto}/heartbeat",
        headers=headers,
        json={
            "status": "Halted (crash)",
            "halt_reason": "crash",
            "halt_detail": "LLM timeout",
            "instance_id": "i-spot-0004",
            "issue_id": 0,
            "role": "onboarder",
        },
    )
    assert r_hb_fail.status_code == 200
    w_failed = store.wiki_for_tenant(tenant.id)
    assert w_failed["refresh_status"] == "failed"
    assert w_failed["merges_since_refresh"] == 0, "Failed refresh must reset counter so next merge does not relaunch"

    # Next single merge (1 < interval 2) increments counter to 1 without relaunching Spot
    out_after_fail = _post_main_metrics("4" * 40, 504)
    assert "wiki_refresh" not in out_after_fail
    assert store.wiki_for_tenant(tenant.id)["merges_since_refresh"] == 1
    assert len(ec2.calls) == 4

    # 7. Setting refresh_interval_merges=0 disables auto-refresh
    client.post(
        f"/internal/v1/tenants/{tenant.id}/wiki/policy",
        headers=headers,
        json={"refresh_interval_merges": 0, "actor": "arsenyspb"},
    )
    out_m3 = _post_main_metrics("5" * 40, 505)
    assert "wiki_refresh" not in out_m3


def test_j50_regenerate_during_initial_onboard_launches_followup_on_complete(fe_fixture: Any) -> None:
    """Regenerate requested during initial onboarding (dispatch_onboard or flow:onboard) launches follow-up when initial run finishes."""
    client, store, ec2, gh, headers = fe_fixture
    repo = "arsenyspb/new-service"

    # 1. Initial onboarding via POST /internal/v1/tenants (dispatch_onboard=True) launches onboarder run 1
    r_create = client.post(
        "/internal/v1/tenants",
        headers=headers,
        json={"repo": repo, "created_by": "arsenyspb", "issue_number": 1, "dispatch_onboard": True},
    )
    assert r_create.status_code == 201
    tenant_id = r_create.json()["tenant_id"]
    onb_run_id = r_create.json()["run_id"]
    assert len(ec2.calls) == 1
    assert store.wiki_for_tenant(tenant_id)["refresh_run_id"] == onb_run_id

    # 2. User clicks Force Regenerate while initial onboarding run is still running -> queued!
    r_regen = client.post(
        f"/internal/v1/tenants/{tenant_id}/wiki/regenerate",
        headers=headers,
        json={"actor": "arsenyspb"},
    )
    assert r_regen.status_code == 200
    assert r_regen.json()["status"] == "queued"
    assert r_regen.json()["dispatched"] is False
    assert len(ec2.calls) == 1
    assert store.wiki_for_tenant(tenant_id)["refresh_queued"] is True

    # 3. Initial onboarding run publishes wiki and completes -> follow-up wiki refresh (run 2) launches automatically!
    client.post(
        f"/internal/v1/runs/{onb_run_id}/wiki",
        headers=headers,
        json={
            "purpose": "New service",
            "technologies": ["Python"],
            "structure": "src/",
            "concepts": ["API"],
            "architecture_mermaid": "flowchart TD\n  A --> B",
            "commit_sha": "a" * 40,
        },
    )
    r_done = client.post(
        f"/internal/v1/runs/{onb_run_id}/heartbeat",
        headers=headers,
        json={"status": "Complete", "instance_id": "i-spot-0001", "issue_id": 1, "role": "onboarder"},
    )
    assert r_done.status_code == 200
    assert len(ec2.calls) == 2, "Completing initial onboarding must drain queued refresh by launching follow-up VM"
    w_state = store.wiki_for_tenant(tenant_id)
    assert w_state["refresh_status"] == "running"
    assert w_state["refresh_queued"] is False
    assert w_state["refresh_run_id"] != onb_run_id

