"""WP-1 & PR #316 review fixes: canonical_sha, import_manifest, rerun attempt gate,
in-process conductor/onboard/gc consumers, wiki POST, manifest in arbitration-data,
strict rebuild validation, and verified metric provenance (#301)."""

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from atobar_contracts import store as contracts_store
from atobar_contracts.arbitration import Observation, arbitrate
from atobar_contracts.envelopes import Verdict, render
from atobar_contracts.manifest import TenantManifest, canonical_sha, load_manifest
from control_panel import app as cp_app
from control_panel import store as cp_store
from control_panel.tokens import hash_token
from flow_engine import store as fe_store
from flow_engine.provenance import verify_and_extract_metrics
from flow_engine.store import MemoryStore
from flow_engine.tenancy.import_manifest import import_tenant_manifest


def test_wp1_single_canonical_sha_and_store_manifest_sha_deleted():
    assert not hasattr(cp_store, "manifest_sha")
    assert not hasattr(fe_store, "manifest_sha")
    assert not hasattr(contracts_store, "manifest_sha")

    manifest = load_manifest("arsenyspb/ollama-monitor")
    assert manifest.sha256() == canonical_sha(manifest)
    assert manifest.sha256() == canonical_sha(manifest.model_dump(mode="json"))


def test_wp1_import_tenant_manifest_seeds_active_metrics_and_manifest_history():
    store = MemoryStore()
    res = import_tenant_manifest("arsenyspb/ollama-monitor", store=store)
    assert res["repo"] == "arsenyspb/ollama-monitor"
    assert set(res["metrics"]) == {"e2e_install_time_seconds", "dependency_count"}

    tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
    assert tenant is not None
    assert canonical_sha(tenant.manifest) == res["manifest_sha"]
    defs = store.metric_definitions(tenant.id)
    assert {d["key"] for d in defs if d["status"] == "active"} == {
        "e2e_install_time_seconds",
        "dependency_count",
    }
    assert len(store.tenants_manifest_history) == 1
    assert store.tenants_manifest_history[0]["manifest_sha"] == res["manifest_sha"]


def test_wp1_arbitrate_selects_max_run_attempt_and_newest_observed_at():
    manifest = TenantManifest.model_validate(
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "metrics": [
                {
                    "key": "unit_test_pass_rate",
                    "run": "pytest -q",
                    "comparator": ">=",
                    "threshold": 1.0,
                    "baseline_mode": "absolute",
                }
            ],
        }
    )
    sha = "a" * 40
    # Attempt 2 (passing) followed by Attempt 1 (failing) in DESC list order
    decision = arbitrate(
        manifest=manifest,
        pr_observations=[
            Observation(key="unit_test_pass_rate", value=1.0, commit_sha=sha, pr=42, run_attempt=2),
            Observation(key="unit_test_pass_rate", value=0.0, commit_sha=sha, pr=42, run_attempt=1),
        ],
        base_observations=[],
        changed_paths=["src/app.py"],
    )
    assert decision.conclusion == "pass"
    assert decision.per_metric["unit_test_pass_rate"].observed_value == 1.0

    # When run_attempt is None on both, observed_at breaks ties deterministically even if listed DESC
    decision_ts = arbitrate(
        manifest=manifest,
        pr_observations=[
            Observation(
                key="unit_test_pass_rate",
                value=1.0,
                commit_sha=sha,
                pr=42,
                observed_at="2026-04-19T12:05:00+00:00",
            ),
            Observation(
                key="unit_test_pass_rate",
                value=0.0,
                commit_sha=sha,
                pr=42,
                observed_at="2026-04-19T12:00:00+00:00",
            ),
        ],
        base_observations=[],
        changed_paths=["src/app.py"],
    )
    assert decision_ts.conclusion == "pass"
    assert decision_ts.per_metric["unit_test_pass_rate"].observed_value == 1.0


def test_review_fix_rebuild_tenant_manifest_rejects_incomplete_active_metrics():
    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.metric_def_rows[(tenant.id, "broken_metric")] = {
        "tenant_id": tenant.id,
        "key": "broken_metric",
        "status": "active",
        "source": {"kind": "command", "run": "pytest -q"},
        "comparator": None,
        "threshold": None,
    }
    with pytest.raises(ValueError, match="incomplete"):
        store.rebuild_tenant_manifest(tenant.id)


def test_review_fix_provenance_rejects_unverified_metrics():
    sha = "b" * 40
    # 1. Missing run_id is rejected (never falls back to caller values)
    with pytest.raises(ValueError, match="Missing run_id"):
        verify_and_extract_metrics(
            repo="arsenyspb/ollama-monitor",
            commit_sha=sha,
            run_id=None,
            run_attempt=1,
            workflow_path=".github/workflows/flow-metrics.yml",
            expected_workflow_path=".github/workflows/flow-metrics.yml",
            gh=None,
        )

    # 2. Missing gh verifier is rejected
    with pytest.raises(ValueError, match="verifier unavailable"):
        verify_and_extract_metrics(
            repo="arsenyspb/ollama-monitor",
            commit_sha=sha,
            run_id=101,
            run_attempt=1,
            workflow_path=".github/workflows/flow-metrics.yml",
            expected_workflow_path=".github/workflows/flow-metrics.yml",
            gh=None,
        )

    # 3. Valid completed/success run with matching zip artifact succeeds
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "flow-metrics.json",
            json.dumps({"commit_sha": sha, "run_attempt": 2, "values": {"unit_test_pass_rate": 1.0}}),
        )
    zip_bytes = buf.getvalue()

    class _FakeGH:
        def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
            return {
                "id": run_id,
                "status": "completed",
                "conclusion": "success",
                "head_sha": sha,
                "path": ".github/workflows/flow-metrics.yml",
                "run_attempt": 2,
            }

        def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
            return zip_bytes

    vals, digest, attempt = verify_and_extract_metrics(
        repo="arsenyspb/ollama-monitor",
        commit_sha=sha,
        run_id=101,
        run_attempt=1,
        workflow_path=".github/workflows/flow-metrics.yml",
        expected_workflow_path=".github/workflows/flow-metrics.yml",
        gh=_FakeGH(),
    )
    assert vals == {"unit_test_pass_rate": 1.0}
    assert attempt == 2
    assert digest == hashlib.sha256(zip_bytes).hexdigest()


def test_review_fix_arbitration_data_includes_manifest_and_run_attempt_and_wiki_roundtrip():
    store = cp_store.MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    manifest = {
        "repo": "arsenyspb/ollama-monitor",
        "owner_github_login": "arsenyspb",
        "metrics_runtime": {"setup": ["pip install -r requirements-dev.txt"]},
        "loop": {"agent_runtime": "nanobot", "max_pr_wall_clock_minutes": 45},
        "metrics": [],
    }
    store.tenants[tenant.id] = cp_store.Tenant(
        tenant.id, tenant.repo, "active", "recommend", manifest
    )
    sha = "c" * 40
    store.record_observations(
        [
            {
                "tenant_id": tenant.id,
                "key": "unit_test_pass_rate",
                "value": 0.0,
                "commit_sha": sha,
                "pr_number": 12,
                "run_id": 1001,
                "run_attempt": 1,
                "observed_at": "2026-04-19T10:00:00+00:00",
            },
            {
                "tenant_id": tenant.id,
                "key": "unit_test_pass_rate",
                "value": 1.0,
                "commit_sha": sha,
                "pr_number": 12,
                "run_id": 1001,
                "run_attempt": 2,
                "observed_at": "2026-04-19T10:05:00+00:00",
            },
        ]
    )

    raw_cred = "run-secret-316"
    store.create_run_credential(
        "run-316",
        tenant.id,
        "arbitrator",
        hashlib.sha256(raw_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )

    cp_app.app.dependency_overrides[cp_app.get_store] = lambda: store
    try:
        client = TestClient(cp_app.app)
        headers = {"Authorization": f"Bearer {raw_cred}"}

        # 1. GET /v1/runs/{run_id}/arbitration-data includes manifest, manifest_sha, and run_attempt
        r_arb = client.get("/v1/runs/run-316/arbitration-data", headers=headers)
        assert r_arb.status_code == 200
        data = r_arb.json()
        assert data["manifest"]["metrics_runtime"]["setup"] == ["pip install -r requirements-dev.txt"]
        assert data["manifest"]["loop"]["max_pr_wall_clock_minutes"] == 45
        assert data["manifest_sha"] == canonical_sha(manifest)
        obs = data["recent_observations"]
        assert len(obs) == 2
        assert [o["run_attempt"] for o in obs] == [1, 2]

        # 2. POST /v1/runs/{run_id}/wiki requires onboarder role; GET /v1/runs/{run_id}/wiki retrieves it
        assert (
            client.post("/v1/runs/run-316/wiki", headers=headers, json={"purpose": "Denied"}).status_code
            == 403
        )
        onb_cred = "cred-onb-316"
        store.create_run_credential(
            "run-onb-316",
            tenant.id,
            "onboarder",
            hash_token(onb_cred),
            "2099-01-01T00:00:00Z",
        )
        r_post_wiki = client.post(
            "/v1/runs/run-onb-316/wiki",
            headers={"Authorization": f"Bearer {onb_cred}"},
            json={
                "purpose": "Monitor Ollama models",
                "key_technologies": ["Python", "FastAPI"],
                "top_level_structure": "src/",
                "key_concepts": ["Polling"],
            },
        )
        assert r_post_wiki.status_code == 200
        r_get_wiki = client.get("/v1/runs/run-316/wiki", headers=headers)
        assert r_get_wiki.status_code == 200
        assert r_get_wiki.json()["purpose"] == "Monitor Ollama models"
    finally:
        cp_app.app.dependency_overrides.clear()


def test_review_fix_in_process_conductor_and_onboard_and_gc_consumers():
    store = cp_store.MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.tenants[tenant.id] = cp_store.Tenant(
        tenant.id,
        tenant.repo,
        "active",
        "shadow",
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "arbitration": {"mode": "shadow"},
            "metrics": [],
        },
    )
    tok = "atb_test_token_316"
    store.add_token(tenant.id, hash_token(tok), "arsenyspb", "ci")

    class _ConsumerDispatcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.onboarded: list[tuple[str, int, str]] = []
            self.labels: dict[tuple[str, int], set[str]] = {
                ("arsenyspb/ollama-monitor", 55): {"flow:in-review"}
            }
            self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
            self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {
                ("arsenyspb/ollama-monitor", "d" * 40): [{"name": "ci", "status": "completed", "conclusion": "success"}]
            }
            self.issue_comments: list[tuple[str, int, str]] = []

        def dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
            self.calls.append((event_type, payload))

        def onboard_tenant(self, repo: str, issue_number: int = 0, *, run_id: str = "", run_credential: str = "") -> None:
            self.onboarded.append((repo, issue_number, run_id))

        def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
            self.labels.setdefault((repo, issue_number), set()).add(label)

        def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
            self.labels.setdefault((repo, issue_number), set()).discard(label)

        def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
            self.issue_comments.append((repo, issue_number, body))

    disp = _ConsumerDispatcher()
    cp_app.app.dependency_overrides[cp_app.get_store] = lambda: store
    cp_app.app.dependency_overrides[cp_app.get_dispatcher] = lambda: disp
    cp_app.app.dependency_overrides[cp_app.get_admin_digest] = lambda: hash_token("admin-tok")
    try:
        client = TestClient(cp_app.app)
        headers = {"Authorization": f"Bearer {tok}"}

        # 1. flow:onboard mints onboarder run credential and invokes onboard_tenant consumer
        r_onb = client.post(
            "/v1/events",
            headers=headers,
            json={"kind": "issues.labeled", "issue_number": 9, "label": "flow:onboard"},
        )
        assert r_onb.status_code == 202 and r_onb.json()["dispatched"] is True
        assert len(disp.onboarded) == 1
        assert disp.onboarded[0][0] == "arsenyspb/ollama-monitor"
        assert disp.onboarded[0][1] == 9
        onb_run_id = disp.onboarded[0][2]
        assert store.get_run_credential(onb_run_id)["role"] == "onboarder"

        # 2. Evaluator changes_requested verdict comment on active PR advances Conductor in-process and spawns Developer
        store.create_run_credential("run-eval-55", tenant.id, "evaluator", "h", "2099-01-01T00:00:00Z", head_sha="d" * 40)
        verdict_body = render(
            Verdict(
                tenant="arsenyspb/ollama-monitor",
                pr=55,
                round=1,
                head_sha="d" * 40,
                run_id="run-eval-55",
                verdict="changes_requested",
                findings=[],
            )
        )
        disp.comments[("arsenyspb/ollama-monitor", 55)] = [
            {"id": 701, "user": {"login": "atobar-flow-agent[bot]"}, "body": verdict_body}
        ]
        r_evt = client.post(
            "/v1/events",
            headers=headers,
            json={
                "kind": "issue_comment.created",
                "issue_number": 55,
                "comment_id": 701,
                "comment_body": verdict_body,
                "sender": "atobar-flow-agent[bot]",
            },
        )
        assert r_evt.status_code == 202 and r_evt.json()["dispatched"] is True
        flow_runs = [c[1] for c in disp.calls if c[0] == "flow_run"]
        assert any(fr.get("role") == "developer" and fr.get("pr") == 55 for fr in flow_runs)

        # 3. Admin GC sweep reaps stale sessions
        store.upsert_session(
            {
                "instance_id": "i-stale-316",
                "run_id": onb_run_id,
                "tenant_id": tenant.id,
                "target_repo": tenant.repo,
                "issue_id": 55,
                "status": "Running",
                "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            }
        )
        r_gc = client.post("/v1/admin/gc/sweep", headers={"Authorization": "Bearer admin-tok"})
        assert r_gc.status_code == 200
        assert "i-stale-316" in r_gc.json()["reaped_sessions"]
        assert store.sessions["i-stale-316"]["status"] == "Halted"
    finally:
        cp_app.app.dependency_overrides.clear()


def test_review_fix_launch_spot_instance_merges_base_template_user_data_and_tags():
    import base64

    from atobar_contracts.github_rest import launch_spot_instance

    base_script = "#!/bin/bash\nset -e\necho 'running cloud_init.sh'\n"
    base_b64 = base64.b64encode(base_script.encode("utf-8")).decode("ascii")

    class _FakeEC2:
        def __init__(self) -> None:
            self.run_kwargs: dict[str, Any] = {}

        def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
            return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"UserData": base_b64}}]}

        def run_instances(self, **kwargs: Any) -> dict[str, Any]:
            self.run_kwargs = kwargs
            return {"Instances": [{"InstanceId": "i-spot-316"}]}

    ec2 = _FakeEC2()
    res = launch_spot_instance(
        {
            "role": "developer",
            "target_repo": "arsenyspb/ollama-monitor",
            "issue_id": 42,
            "run_id": "run-42",
            "run_credential": "cred-42",
        },
        ec2_client=ec2,
    )
    assert res["instance_id"] == "i-spot-316"
    assert res["tags"]["atobar:managed"] == "true"
    assert res["tags"]["atobar:tenant"] == "arsenyspb/ollama-monitor"
    assert res["tags"]["Repository"] == "arsenyspb/ollama-monitor"
    decoded_ud = base64.b64decode(ec2.run_kwargs["UserData"]).decode("utf-8")
    assert 'export FLOW_ROLE="developer"' in decoded_ud
    assert 'export FLOW_RUN_ID="run-42"' in decoded_ud
    assert "echo 'running cloud_init.sh'" in decoded_ud
    assert ec2.run_kwargs["LaunchTemplate"] == {
        "LaunchTemplateName": "flow-spot-sandbox-template",
        "Version": "$Latest",
    }
