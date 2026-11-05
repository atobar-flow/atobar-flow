import hashlib
from unittest.mock import MagicMock

from control_panel.store import MemoryStore
from flow_engine.conductor.io import process_flow_event
from atobar_contracts.envelopes import Ack, render


def test_process_flow_event_records_envelope_digest_and_processed_event_payload():
    store = MemoryStore()
    tenant = store.create_tenant("owner/repo", "admin")

    ack = Ack(
        tenant="owner/repo",
        pr=10,
        head_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="run-ack-10",
        addressed_findings=[],
        dispute=[],
    )
    rendered_ack = render(ack)
    expected_digest = hashlib.sha256(rendered_ack.encode("utf-8")).hexdigest()

    # Create run credential so envelope is trusted
    store.create_run_credential(
        run_id="run-ack-10",
        tenant_id=tenant.id,
        role="developer",
        cred_hash="dummy",
        expires_at="2099-01-01T00:00:00Z",
    )

    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {
        "state": "open",
        "labels": [],
        "head": {"sha": "0123456789abcdef0123456789abcdef01234567"},
    }
    fake_gh.get_comments.return_value = [
        {"id": 101, "user": {"login": "atobar-flow-agent[bot]"}, "body": rendered_ack}
    ]
    fake_gh.get_check_runs.return_value = []

    payload = {
        "tenant": "owner/repo",
        "event_id": "evt-123",
        "event": {
            "kind": "issue_comment.created",
            "repo": "owner/repo",
            "pr_number": 10,
            "comment_id": 101,
            "comment_body": rendered_ack,
            "sender": "atobar-flow-agent[bot]",
        },
    }

    bot_logins = {"developer": "atobar-flow-agent[bot]"}

    res = process_flow_event(
        payload=payload,
        gh=fake_gh,
        store=store,
        bot_logins=bot_logins,
    )
    assert res["status"] == "success"

    # Verify loop_rounds row has envelope_digest
    rounds = store.loop_rounds_for_pr(tenant.id, 10)
    assert len(rounds) == 1
    assert rounds[0]["envelope_digest"] == expected_digest

    # Verify conductor_events recorded the event payload, kind, and sender (plus comment dedup key)
    assert hasattr(store, "conductor_events")
    assert len(store.conductor_events) == 2
    recorded = store.conductor_events[0]
    assert recorded["event_id"] == "evt-123"
    assert store.conductor_events[1]["event_id"] == "comment:10:101"
    assert recorded["kind"] == "issue_comment.created"
    assert recorded["sender"] == "atobar-flow-agent[bot]"
    assert recorded["payload"] == payload["event"]


def test_record_loop_round_snapshots_sessions_usage():
    store = MemoryStore()
    tenant = store.create_tenant("owner/repo", "admin")

    # Seed sessions row with usage
    store.sessions = {
        "inst-ack-20": {
            "instance_id": "inst-ack-20",
            "run_id": "run-ack-20",
            "status": "Running",
            "usage": {
                "total": {
                    "input_tokens": 1500,
                    "output_tokens": 300,
                    "cost_usd": 0.045,
                }
            },
        }
    }

    ack = Ack(
        tenant="owner/repo",
        pr=20,
        head_sha="1234567890abcdef1234567890abcdef12345678",
        run_id="run-ack-20",
        addressed_findings=[],
        dispute=[],
    )
    rendered_ack = render(ack)

    store.create_run_credential(
        run_id="run-ack-20",
        tenant_id=tenant.id,
        role="developer",
        cred_hash="dummy",
        expires_at="2099-01-01T00:00:00Z",
    )

    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {
        "state": "open",
        "labels": [],
        "head": {"sha": "1234567890abcdef1234567890abcdef12345678"},
    }
    fake_gh.get_comments.return_value = [
        {"id": 201, "user": {"login": "atobar-flow-agent[bot]"}, "body": rendered_ack}
    ]
    fake_gh.get_check_runs.return_value = []

    payload = {
        "tenant": "owner/repo",
        "event_id": "evt-200",
        "event": {
            "kind": "issue_comment.created",
            "repo": "owner/repo",
            "pr_number": 20,
            "comment_id": 201,
            "comment_body": rendered_ack,
            "sender": "atobar-flow-agent[bot]",
        },
    }

    res = process_flow_event(
        payload=payload,
        gh=fake_gh,
        store=store,
        bot_logins={"developer": "atobar-flow-agent[bot]"},
    )
    assert res["status"] == "success"

    rounds = store.loop_rounds_for_pr(tenant.id, 20)
    assert len(rounds) == 1
    # tokens and cost_usd snapshotted from sessions.usage
    assert rounds[0]["tokens"] == 1800
    assert rounds[0]["cost_usd"] == 0.045
    assert rounds[0]["usage_detail"]["total"] == {
        "input_tokens": 1500,
        "output_tokens": 300,
        "cost_usd": 0.045,
    }
    assert rounds[0]["usage_detail"]["envelope"]["run_id"] == "run-ack-20"
    assert rounds[0]["envelope_digest"] is not None

    # Check that conductor.dispatch and conductor.round events were recorded
    round_events = [e for e in getattr(store, "run_events", []) if e.get("kind") == "conductor.round"]
    assert len(round_events) == 1
    assert round_events[0]["payload"]["pr"] == 20


def test_arbitration_envelope_does_not_insert_loop_round_row():
    from atobar_contracts.envelopes import Arbitration

    store = MemoryStore()
    tenant = store.create_tenant("owner/repo", "admin")
    store.create_run_credential(
        run_id="run-arb-30",
        tenant_id=tenant.id,
        role="arbitrator",
        cred_hash="dummy",
        expires_at="2099-01-01T00:00:00Z",
    )

    arb = Arbitration(
        tenant="owner/repo",
        pr=30,
        head_sha="1234567890abcdef1234567890abcdef12345678",
        run_id="run-arb-30",
        decision="pass",
        mode="recommend",
        action_taken="posted_recommendation",
        observation_ids=[],
        summary="pass: All metrics passed",
    )
    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {
        "state": "open",
        "labels": [],
        "head": {"sha": "1234567890abcdef1234567890abcdef12345678"},
    }
    fake_gh.get_comments.return_value = [
        {"id": 301, "user": {"login": "atobar-flow-agent[bot]"}, "body": render(arb)}
    ]
    fake_gh.get_check_runs.return_value = []

    res = process_flow_event(
        payload={
            "tenant": "owner/repo",
            "event_id": "evt-arb-30",
            "event": {
                "kind": "issue_comment.created",
                "repo": "owner/repo",
                "pr_number": 30,
                "comment_id": 301,
                "comment_body": render(arb),
                "sender": "atobar-flow-agent[bot]",
            },
        },
        gh=fake_gh,
        store=store,
        bot_logins={"arbitrator": "atobar-flow-agent[bot]"},
    )
    assert res["status"] == "success"
    assert store.loop_rounds_for_pr(tenant.id, 30) == []


def test_halted_heartbeat_records_idempotent_loop_round_with_partial_usage():
    from fastapi.testclient import TestClient

    from flow_engine import api as fe_api

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.create_run_credential(
        run_id="run-halt-43",
        tenant_id=tenant.id,
        role="developer",
        cred_hash="dummy",
        expires_at="2099-01-01T00:00:00Z",
    )
    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.require_internal_token] = lambda: None
    try:
        client = TestClient(fe_api.app)
        hb_payload = {
            "status": "Halted",
            "instance_id": "i-halt43",
            "target_repo": "arsenyspb/ollama-monitor",
            "issue_id": 43,
            "agent_runtime": "nanobot",
            "iterations": 1,
            "halt_reason": "provider_error",
            "halt_detail": "provider_error: model=gemini-nonexistent status=404",
            "usage": {
                "agent_runtime": "nanobot",
                "total": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "cost_usd": 0.0012},
                "calls": [
                    {
                        "iteration": 1,
                        "provider": "gemini",
                        "model": "gemini-nonexistent",
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "cost_usd": 0.0012,
                        "stop_reason": "provider_error",
                    }
                ],
            },
        }
        # Send twice (e.g. nanobot_runtime + _shutdown); must only insert 1 loop_rounds row
        r1 = client.post("/internal/v1/runs/run-halt-43/heartbeat", json=hb_payload)
        r2 = client.post("/internal/v1/runs/run-halt-43/heartbeat", json=hb_payload)
        assert r1.status_code == 200
        assert r2.status_code == 200

        rounds = store.loop_rounds_for_pr(tenant.id, 43)
        assert len(rounds) == 1
        assert rounds[0]["role"] == "developer"
        assert rounds[0]["tokens"] == 150
        assert rounds[0]["envelope_digest"] == "halted:provider_error"
        assert rounds[0]["usage_detail"]["agent_runtime"] == "nanobot"
        assert rounds[0]["usage_detail"]["halt_reason"] == "provider_error"
        assert len(rounds[0]["usage_detail"]["calls"]) == 1
    finally:
        fe_api.app.dependency_overrides.clear()


def test_service_github_client_mint_repo_token_intersects_installation_permissions(monkeypatch):
    import httpx

    from flow_engine import api as fe_api

    gh = fe_api.FlowEngineGitHubAppClient(app_id="1", installation_id="149092759", private_key="k")
    monkeypatch.setattr(gh, "_jwt", lambda: "fake-jwt")
    captured_json: dict[str, str] = {}

    def fake_get(url, headers=None, timeout=15):
        req = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "id": 149092759,
                "account": {"login": "arsenyspb"},
                "permissions": {
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "checks": "write",
                    "metadata": "read",
                },
            },
            request=req,
        )

    def fake_post(url, headers=None, json=None, timeout=15):
        req = httpx.Request("POST", url)
        captured_json.update((json or {}).get("permissions") or {})
        return httpx.Response(
            201,
            json={"token": "ghs_minted_ok", "expires_at": "2099-01-01T00:00:00Z"},
            request=req,
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    minted = gh.mint_repo_token("arsenyspb/ollama-monitor")
    assert minted["token"] == "ghs_minted_ok"
    assert "actions" not in captured_json
    assert "workflows" not in captured_json
    assert captured_json == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "checks": "write",
        "metadata": "read",
    }


def test_relabeling_issue_without_delivery_id_dispatches_fresh_run_after_terminal_session():
    from fastapi.testclient import TestClient

    from flow_engine import api as fe_api

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")

    fake_ec2 = MagicMock()
    fake_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-relabel-1"}]}
    fake_gh = MagicMock()
    fake_gh.get_issue_body.return_value = "## Scope\n- `src/app.py`\n\n## Acceptance criteria\n- [ ] AC-1: Works"

    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.require_internal_token] = lambda: None
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: fake_ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: fake_gh
    try:
        client = TestClient(fe_api.app)
        payload = {
            "tenant": "arsenyspb/ollama-monitor",
            "event": {"kind": "issues.labeled", "issue_number": 43, "label": "flow:feature", "sender": "arsenyspb"},
        }
        r1 = client.post("/internal/v1/events", json=payload)
        assert r1.status_code == 200
        assert r1.json()["dispatched"] is True
        run1 = r1.json()["run_id"]

        # Mark first session terminal (e.g. Complete or Halted)
        store.update_session(f"pending-{run1}", {"status": "Complete"})

        # Re-applying flow:feature without delivery_id must dispatch a second run
        r2 = client.post("/internal/v1/events", json=payload)
        assert r2.status_code == 200
        assert r2.json()["dispatched"] is True
        assert r2.json()["run_id"] != run1
        assert fake_ec2.run_instances.call_count == 2
    finally:
        fe_api.app.dependency_overrides.clear()


def test_mint_engine_read_token_and_launch_spot_user_data_upgrade(monkeypatch):
    import base64
    import subprocess

    import httpx

    from atobar_contracts.github_rest import launch_spot_instance
    from flow_engine import api as fe_api

    gh = fe_api.FlowEngineGitHubAppClient(app_id="1", installation_id="149092759", private_key="k")
    monkeypatch.setattr(gh, "_jwt", lambda: "fake-jwt")
    posted: list[dict] = []

    def fake_get(url, headers=None, timeout=15):
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"id": 149092759, "permissions": {"contents": "read", "metadata": "read"}}, request=req)

    def fake_post(url, headers=None, json=None, timeout=15):
        req = httpx.Request("POST", url)
        posted.append(dict(json or {}))
        return httpx.Response(201, json={"token": "ghs_engine_ro", "expires_at": "2099-01-01T00:00:00Z"}, request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    tok = gh.mint_engine_read_token()
    assert tok == "ghs_engine_ro"
    assert posted[0] == {"repositories": ["atobar-flow"], "permissions": {"contents": "read", "metadata": "read"}}

    legacy_base = (
        "#!/bin/bash\n"
        'echo \'Defaults env_keep += "CONTROL_PANEL_URL FLOW_RUN_ID FLOW_RUN_CREDENTIAL GITHUB_TOKEN"\' > /etc/sudoers.d/99-atobar-env\n'
        'ENGINE_TOKEN="ghs_engine_ro"\n'
        'git -c credential.helper= clone "https://github.com/atobar-flow/atobar-flow.git" "$ENGINE_DIR"\n'
        "unset ENGINE_TOKEN\n"
    )
    fake_ec2 = MagicMock()
    fake_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-test-ud"}]}
    launch_spot_instance(
        {
            "role": "planner",
            "target_repo": "arsenyspb/ollama-monitor",
            "issue_id": 43,
            "pr": 43,
            "run_id": "run_ud_1",
            "run_credential": "cred_ud_1",
            "engine_ref": "a" * 40,
            "base_user_data": legacy_base,
        },
        ec2_client=fake_ec2,
    )
    ud_b64 = fake_ec2.run_instances.call_args.kwargs["UserData"]
    ud_text = base64.b64decode(ud_b64).decode("utf-8")
    assert "/etc/sudoers.d/99-atobar-env" in ud_text
    assert "ENGINE_TOKEN=" in ud_text
    assert "unset ENGINE_TOKEN" in ud_text
    proc = subprocess.run(["bash", "-n"], input=ud_text, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_run_envelope_dedup_prevents_double_loop_round_when_comment_id_differs():
    from atobar_contracts.envelopes import Verdict

    store = MemoryStore()
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.create_run_credential(
        run_id="run-eval-dedup",
        tenant_id=tenant.id,
        role="evaluator",
        cred_hash="dummy",
        expires_at="2099-01-01T00:00:00Z",
    )
    sha = "6d0e269e5987645649874df1db2eb6576b664007"
    verdict = Verdict(
        tenant=tenant.repo,
        pr=43,
        head_sha=sha,
        run_id="run-eval-dedup",
        round=1,
        verdict="approved",
        findings=[],
    )
    body = render(verdict)
    fake_gh = MagicMock()
    fake_gh.get_pr.return_value = {"state": "open", "labels": [], "head": {"sha": sha}}
    fake_gh.get_comments.return_value = [
        {"id": 6285044375, "user": {"login": "atobar-flow-agent[bot]"}, "body": body}
    ]
    fake_gh.get_check_runs.return_value = []

    # 1. turn-complete arrives with comment_id=6285044375
    res1 = process_flow_event(
        payload={
            "tenant": tenant.repo,
            "event_id": "evt-tc-1",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 43,
                "comment_body": body,
                "comment_id": 6285044375,
                "sender": "atobar-flow-agent[bot]",
            },
        },
        gh=fake_gh,
        store=store,
        bot_logins={"evaluator": "atobar-flow-agent[bot]"},
    )
    assert res1["status"] == "success"

    # 2. flow-trigger.yml arrives with comment_id=6285044375 and different event_id
    res2 = process_flow_event(
        payload={
            "tenant": tenant.repo,
            "event_id": "evt-gh-2",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 43,
                "comment_body": body,
                "comment_id": 6285044375,
                "sender": "atobar-flow-agent[bot]",
            },
        },
        gh=fake_gh,
        store=store,
        bot_logins={"evaluator": "atobar-flow-agent[bot]"},
    )
    assert res2["status"] == "skipped"
    assert len(store.loop_rounds_for_pr(tenant.id, 43)) == 1


def test_provenance_accepts_in_progress_workflow_run_and_approved_verdict_converges():
    import io
    import json
    import zipfile

    from atobar_contracts.envelopes import Verdict
    from atobar_contracts.machine import LoopLimits, LoopState, VerdictEvent, step
    from flow_engine.provenance import verify_and_extract_metrics

    sha = "6d0e269e5987645649874df1db2eb6576b664007"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "flow-metrics.json",
            json.dumps({"commit_sha": sha, "pr_number": 51, "run_attempt": 1, "values": {"dependency_count": 9.0}}),
        )
    zip_bytes = buf.getvalue()

    class _GH:
        def get_workflow_run(self, repo: str, run_id: int) -> dict:
            return {
                "id": run_id,
                "status": "in_progress",
                "conclusion": None,
                "head_sha": sha,
                "path": ".github/workflows/flow-metrics.yml",
                "run_attempt": 1,
                "pull_requests": [{"number": 51, "head": {"sha": sha}}],
            }

        def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
            return zip_bytes

    vals, _, attempt = verify_and_extract_metrics(
        repo="arsenyspb/ollama-monitor",
        commit_sha=sha,
        run_id=39380501847,
        run_attempt=1,
        workflow_path=".github/workflows/flow-metrics.yml",
        expected_workflow_path=".github/workflows/flow-metrics.yml",
        gh=_GH(),
        pr_number=51,
    )
    assert vals == {"dependency_count": 9.0}
    assert attempt == 1

    # Approved VerdictEvent converges to arbitrator even if wall_clock reaches cap on the final round
    state = LoopState(tenant="arsenyspb/ollama-monitor", pr=43, head_sha=sha, accumulated_wall_clock_minutes=25.0)
    v_ok = Verdict(tenant="arsenyspb/ollama-monitor", pr=43, head_sha=sha, round=1, verdict="approved", findings=[])
    new_state, actions = step(state, VerdictEvent(verdict=v_ok, wall_clock_minutes=8.0), LoopLimits(max_pr_wall_clock_minutes=30.0))
    assert new_state.status == "converged"
    assert any(getattr(a, "role", None) == "arbitrator" for a in actions)


def test_j42_wp2_role_scoped_github_tokens_and_404_owner_rejection(monkeypatch):
    import httpx
    import pytest

    from flow_engine import api as fe_api

    gh = fe_api.FlowEngineGitHubAppClient(app_id="1", installation_id="149092759", private_key="k")
    monkeypatch.setattr(gh, "_jwt", lambda: "fake-jwt")
    minted_perms: list[dict[str, str]] = []
    inst_status = 200
    inst_owner = "arsenyspb"

    def fake_get(url, headers=None, timeout=15):
        req = httpx.Request("GET", url)
        if inst_status == 404:
            return httpx.Response(404, json={"message": "Not Found"}, request=req)
        return httpx.Response(
            200,
            json={
                "id": 149092759,
                "account": {"login": inst_owner},
                "permissions": {
                    "contents": "write",
                    "workflows": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "checks": "write",
                    "metadata": "read",
                },
            },
            request=req,
        )

    def fake_post(url, headers=None, json=None, timeout=15):
        req = httpx.Request("POST", url)
        minted_perms.append(dict((json or {}).get("permissions") or {}))
        return httpx.Response(
            201,
            json={"token": f"ghs_{len(minted_perms)}", "expires_at": "2099-01-01T00:00:00Z"},
            request=req,
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    # Developer gets contents:write, pull_requests:write, issues:write, checks:read, NO workflows
    dev_tok = gh.mint_repo_token("arsenyspb/ollama-monitor", "developer")
    assert dev_tok["token"] == "ghs_1"
    assert minted_perms[-1] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    }

    # Evaluator gets contents:read, checks:write, and does NOT reuse developer's cache entry
    eval_tok = gh.mint_repo_token("arsenyspb/ollama-monitor", "evaluator")
    assert eval_tok["token"] == "ghs_2"
    assert minted_perms[-1] == {
        "contents": "read",
        "pull_requests": "write",
        "issues": "write",
        "checks": "write",
        "metadata": "read",
    }

    # Planner gets contents:read, pull_requests:read, issues:write, checks:read
    gh.mint_repo_token("arsenyspb/ollama-monitor", "planner")
    assert minted_perms[-1] == {
        "contents": "read",
        "pull_requests": "read",
        "issues": "write",
        "checks": "read",
        "metadata": "read",
    }

    # Onboarder gets workflows:write
    gh.mint_repo_token("arsenyspb/ollama-monitor", "onboarder")
    assert minted_perms[-1]["workflows"] == "write"

    # Mismatched installation owner raises PermissionError
    inst_owner = "other-org"
    with pytest.raises(PermissionError, match="does not match repo owner"):
        gh.mint_repo_token("arsenyspb/ollama-monitor", "arbitrator")

    # 404 installation does NOT fall back to factory installation_id
    inst_status = 404
    with pytest.raises(PermissionError, match="GitHub App not installed"):
        gh.mint_repo_token("arsenyspb/uninstalled-repo", "developer")


def test_j42_wp4_metric_artifact_bounds(monkeypatch):
    import io
    import json
    import zipfile

    import pytest

    from flow_engine.provenance import verify_and_extract_metrics

    sha = "a" * 40
    run_meta = {
        "id": 900,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
        "pull_requests": [],
    }

    # 1. Zip with > 5 entries is rejected
    buf_many = io.BytesIO()
    with zipfile.ZipFile(buf_many, "w") as zf:
        zf.writestr("flow-metrics.json", json.dumps({"commit_sha": sha, "values": {"m": 1.0}}))
        for i in range(5):
            zf.writestr(f"extra-{i}.txt", "x")

    class _GHMany:
        def get_workflow_run(self, repo: str, run_id: int) -> dict:
            return run_meta

        def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
            return buf_many.getvalue()

    with pytest.raises(ValueError, match="too many entries"):
        verify_and_extract_metrics(
            repo="arsenyspb/ollama-monitor",
            commit_sha=sha,
            run_id=900,
            run_attempt=1,
            workflow_path=".github/workflows/flow-metrics.yml",
            expected_workflow_path=".github/workflows/flow-metrics.yml",
            gh=_GHMany(),
        )

    # 2. Zip with flow-metrics.json > 256 KiB uncompressed is rejected before reading full entry
    buf_huge = io.BytesIO()
    with zipfile.ZipFile(buf_huge, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("flow-metrics.json", " " * 300_000)

    class _GHHuge:
        def get_workflow_run(self, repo: str, run_id: int) -> dict:
            return run_meta

        def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
            return buf_huge.getvalue()

    with pytest.raises(ValueError, match="exceeds 262144 byte limit"):
        verify_and_extract_metrics(
            repo="arsenyspb/ollama-monitor",
            commit_sha=sha,
            run_id=900,
            run_attempt=1,
            workflow_path=".github/workflows/flow-metrics.yml",
            expected_workflow_path=".github/workflows/flow-metrics.yml",
            gh=_GHHuge(),
        )





