"""WP-3 J34 Control Panel Gateway Cutover & Self-Service UI tests (#301, #258, #299)."""

import hashlib
import io
import json
import logging
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from atobar_contracts.envelopes import Plan, PlannedCriterion, render
from atobar_contracts.manifest import canonical_sha
from control_panel import app as cp_app
from control_panel import web
from control_panel.flow_engine_client import FlowEngineClient, get_flow_engine_client
from control_panel.tokens import hash_token
from flow_engine import api as fe_api
from flow_engine.store import MemoryStore


class _FakeEC2:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
        return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"UserData": ""}}]}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Instances": [{"InstanceId": f"i-spot-{len(self.calls):04d}"}]}

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        return {"Reservations": []}

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        return {"TerminatingInstances": [{"InstanceId": iid} for iid in InstanceIds]}


class _FakeGH:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, int], dict[str, Any]] = {}
        self.artifacts: dict[tuple[str, int], bytes] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.issue_bodies: dict[tuple[str, int], str] = {}
        self.opened_prs: list[dict[str, Any]] = []
        self.minted_for: list[str] = []

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

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).add(label)

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels.setdefault((repo, issue_number), set()).discard(label)

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        return self.issue_bodies.get(
            (repo, issue_number),
            "## Scope\n- `src/main.py`\n\n## Acceptance criteria\n- [ ] AC-1: Passes\n",
        )

    def get_collaborator_permission(self, repo: str, username: str) -> str:
        return "admin"

    def mint_repo_token(self, repo: str, role: str | None = None) -> dict[str, str]:
        self.minted_for.append(repo)
        return {
            "token": f"ghs_fe_{repo.replace('/', '_')}",
            "expires_at": "2099-01-01T00:00:00Z",
        }

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
            "number": len(self.opened_prs) + 200,
            "html_url": f"https://github.com/{repo}/pull/{len(self.opened_prs) + 200}",
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


class _SyncASGIBridgeTransport(httpx.BaseTransport):
    """Bridges synchronous httpx.Client calls in CP to flow_engine.api.app via TestClient."""

    def __init__(self, asgi_app: Any) -> None:
        self._tc = TestClient(asgi_app, raise_server_exceptions=False)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        resp = self._tc.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            request=request,
        )


@pytest.fixture()
def cutover_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FE_INTERNAL_TOKEN", "fe-cutover-secret-301")
    store = MemoryStore()
    ec2 = _FakeEC2()
    gh = _FakeGH()

    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: gh

    bridge = _SyncASGIBridgeTransport(fe_api.app)
    fe_client = FlowEngineClient(
        base_url="http://flow-engine:8081",
        internal_token="fe-cutover-secret-301",
        transport=bridge,
    )

    admin_tok = "cp-admin-secret-301"
    cp_app.app.dependency_overrides[cp_app.get_store] = lambda: store
    cp_app.app.dependency_overrides[get_flow_engine_client] = lambda: fe_client
    cp_app.app.dependency_overrides[cp_app.get_admin_digest] = lambda: hash_token(admin_tok)

    client = TestClient(cp_app.app, base_url="https://testserver")
    try:
        yield client, store, ec2, gh, bridge, admin_tok
    finally:
        cp_app.app.dependency_overrides.clear()
        fe_api.app.dependency_overrides.clear()


def test_cp_request_id_middleware_and_structured_json_logging(cutover_env, caplog: pytest.LogCaptureFixture):
    client, _, _, _, _, _ = cutover_env
    caplog.set_level(logging.INFO)

    # 1. Auto-generates UUIDv4 X-Request-ID when omitted
    r1 = client.get("/healthz")
    assert r1.status_code == 200
    gen_id = r1.headers.get("X-Request-ID")
    assert gen_id is not None
    uuid.UUID(gen_id, version=4)

    # 2. Echoes incoming X-Request-ID and emits structured JSON log
    caplog.clear()
    r2 = client.get("/healthz", headers={"X-Request-ID": "cp-req-trace-777"})
    assert r2.status_code == 200
    assert r2.headers.get("X-Request-ID") == "cp-req-trace-777"

    logs = []
    for rec in caplog.records:
        try:
            doc = json.loads(rec.getMessage())
            if isinstance(doc, dict) and doc.get("request_id") == "cp-req-trace-777":
                logs.append(doc)
        except Exception:
            continue
    assert len(logs) >= 1
    entry = logs[0]
    assert set(entry.keys()) >= {"request_id", "tenant", "run_id", "route", "status", "ms"}
    assert entry["route"] == "/healthz"
    assert entry["status"] == 200


def test_flow_engine_client_retry_and_502_unavailable_mapping(cutover_env, monkeypatch: pytest.MonkeyPatch):
    client, store, _, _, _, admin_tok = cutover_env

    # 1. Single ConnectError is retried once and succeeds on attempt 2
    attempts = 0

    def _flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("Temporary connect error", request=request)
        return httpx.Response(200, json={"status": "dispatched", "dispatched": True}, request=request)

    flaky_fe = FlowEngineClient(
        base_url="http://flow-engine:8081",
        internal_token="fe-cutover-secret-301",
        transport=httpx.MockTransport(_flaky_handler),
    )
    res = flaky_fe.post_events(
        {"tenant": "arsenyspb/ollama-monitor", "event": {"kind": "issues.labeled", "issue_number": 1, "label": "flow:fix"}},
        request_id="req-retry-ok",
    )
    assert attempts == 2
    assert res["dispatched"] is True

    # 2. Persistent ConnectError (2 attempts) or Timeout or 503 maps to 502 flow_engine_unavailable
    tenant = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    store.update_tenant_status(tenant.id, "active")
    tok_resp = client.post(
        "/v1/admin/tenants/arsenyspb/ollama-monitor/tokens",
        headers={"Authorization": f"Bearer {admin_tok}"},
        json={"owner_github_login": "arsenyspb"},
    )
    tenant_tok = tok_resp.json()["token"]

    fail_attempts = 0

    def _down_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_attempts
        fail_attempts += 1
        raise httpx.ConnectError("FE unreachable", request=request)

    down_fe = FlowEngineClient(
        base_url="http://flow-engine:8081",
        internal_token="fe-cutover-secret-301",
        transport=httpx.MockTransport(_down_handler),
    )
    cp_app.app.dependency_overrides[get_flow_engine_client] = lambda: down_fe

    r_502 = client.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {tenant_tok}", "X-Request-ID": "req-down-502"},
        json={"kind": "issues.labeled", "issue_number": 10, "label": "flow:fix"},
    )
    assert r_502.status_code == 502
    assert fail_attempts == 2
    assert r_502.headers.get("X-Request-ID") == "req-down-502"
    body_502 = r_502.json()
    detail_502 = body_502.get("detail") if isinstance(body_502.get("detail"), dict) else body_502
    assert detail_502["error"] == "flow_engine_unavailable"
    assert detail_502["request_id"] == "req-down-502"

    # Web UI stays responsive even when flow-engine is down
    r_home = client.get("/")
    assert r_home.status_code == 200


def test_gateway_forwarding_events_metrics_onboard_and_run_routes(cutover_env):
    client, store, ec2, gh, bridge, admin_tok = cutover_env

    # 1. POST /v1/admin/tenants/{owner}/{repo}/onboard forwards to FE POST /internal/v1/tenants
    r_onboard = client.post(
        "/v1/admin/tenants/arsenyspb/ollama-monitor/onboard?issue_number=5",
        headers={"Authorization": f"Bearer {admin_tok}", "X-Request-ID": "req-onb-1"},
    )
    assert r_onboard.status_code == 201
    onb_data = r_onboard.json()
    assert onb_data["dispatched"] is True
    assert "token" in onb_data
    tenant_tok = onb_data["token"]
    tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
    assert tenant is not None
    assert len(ec2.calls) == 1

    # Seed active metric and activate via verified POST /v1/metrics
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
    store.rebuild_tenant_manifest(tenant.id)
    sha1 = "1" * 40
    gh.runs[(tenant.repo, 501)] = {
        "id": 501,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha1,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 501)] = _make_metrics_zip(
        sha1, {"unit_test_pass_rate": 1.0, "undeclared_extra": 5.0}, run_attempt=1
    )
    r_metrics = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}", "X-Request-ID": "req-metrics-1"},
        json={
            "commit_sha": sha1,
            "run_id": 501,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
            "values": {"unit_test_pass_rate": 1.0, "undeclared_extra": 5.0},
        },
    )
    assert r_metrics.status_code == 201
    m_body = r_metrics.json()
    assert m_body["recorded"] == ["unit_test_pass_rate"]
    assert m_body["ignored"] == ["undeclared_extra"]
    assert m_body["activated"] is True
    assert store.tenant_by_id(tenant.id).status == "active"

    # 2. flow:approved label is ignored on /v1/events (no special approval handling)
    r_approved = client.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={"kind": "issues.labeled", "issue_number": 12, "label": "flow:approved", "sender": "alice"},
    )
    assert r_approved.status_code == 202
    assert r_approved.json()["dispatched"] is False

    # 3. AC-3: Warm handover via FE on POST /v1/runs/{run_id}/turn-complete with X-Request-ID propagation
    plan_run_id = "run-warm-plan-301"
    plan_cred = "cred-warm-plan-301"
    store.create_run_credential(
        plan_run_id,
        tenant.id,
        "planner",
        hashlib.sha256(plan_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    store.upsert_session(
        {
            "instance_id": "i-warm-301",
            "run_id": plan_run_id,
            "tenant_id": tenant.id,
            "role": "planner",
            "target_repo": tenant.repo,
            "issue_id": 77,
            "status": "Running",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Heartbeat, events, wiki, arbitration-data, arbitration-decisions, github-token via FE
    r_hb = client.post(
        f"/v1/runs/{plan_run_id}/heartbeat",
        headers={"Authorization": f"Bearer {plan_cred}", "X-Request-ID": "req-hb-301"},
        json={"status": "Running", "instance_id": "i-warm-301", "issue_id": 77},
    )
    assert r_hb.status_code == 200
    assert any(req.headers.get("X-Request-ID") == "req-hb-301" for req in bridge.requests)

    r_evts = client.post(
        f"/v1/runs/{plan_run_id}/events",
        headers={"Authorization": f"Bearer {plan_cred}"},
        json={"events": [{"seq": 1, "kind": "step", "payload": {"ok": True}}]},
    )
    assert r_evts.status_code == 200
    assert len(store.run_events_for_run(plan_run_id)) == 1

    # J42 WP-2 (UJ-10): planner role is rejected (403) on POST /v1/runs/{run_id}/wiki; only onboarder is allowed
    r_wiki_put_denied = client.post(
        f"/v1/runs/{plan_run_id}/wiki",
        headers={"Authorization": f"Bearer {plan_cred}"},
        json={"purpose": "Gateway wiki test"},
    )
    assert r_wiki_put_denied.status_code == 403

    onb_run_id = "run-onb-301"
    onb_cred = "cred-onb-301"
    store.create_run_credential(
        onb_run_id,
        tenant.id,
        "onboarder",
        hashlib.sha256(onb_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    r_wiki_put = client.post(
        f"/v1/runs/{onb_run_id}/wiki",
        headers={"Authorization": f"Bearer {onb_cred}"},
        json={"purpose": "Gateway wiki test"},
    )
    assert r_wiki_put.status_code == 200
    r_wiki_get = client.get(
        f"/v1/runs/{plan_run_id}/wiki",
        headers={"Authorization": f"Bearer {plan_cred}"},
    )
    assert r_wiki_get.status_code == 200
    assert r_wiki_get.json()["purpose"] == "Gateway wiki test"

    r_ghtok = client.post(
        f"/v1/runs/{plan_run_id}/github-token",
        headers={"Authorization": f"Bearer {plan_cred}"},
    )
    assert r_ghtok.status_code == 200
    assert r_ghtok.json()["token"] == "ghs_fe_arsenyspb_ollama-monitor"

    plan_env = render(
        Plan(
            tenant=tenant.repo,
            pr=77,
            head_sha="0" * 40,
            run_id=plan_run_id,
            complexity="single_pr",
            goal="Gateway handover",
            scope_files=["src/main.py"],
            acceptance_criteria=[PlannedCriterion(id="AC-1", description="Passes")],
            implementation_steps=["Update src/main.py"],
        )
    )
    r_tc = client.post(
        f"/v1/runs/{plan_run_id}/turn-complete",
        headers={"Authorization": f"Bearer {plan_cred}", "X-Request-ID": "req-tc-301"},
        json={
            "comment_id": 9001,
            "comment_body": plan_env,
            "head_sha": "0" * 40,
            "issue_number": 77,
            "instance_id": "i-warm-301",
        },
    )
    assert r_tc.status_code == 200
    tc_body = r_tc.json()
    assert tc_body["action"] == "continue"
    assert tc_body["role"] == "developer"
    assert r_tc.headers.get("X-Request-ID") == "req-tc-301"
    assert any(
        req.url.path == f"/internal/v1/runs/{plan_run_id}/turn-complete"
        and req.headers.get("X-Request-ID") == "req-tc-301"
        for req in bridge.requests
    )


def test_web_ui_guardrails_and_cp_authored_metrics_e2e(cutover_env):
    client, store, ec2, gh, _, _ = cutover_env

    secret = "s" * 40
    cfg = web.OAuthConfig("cid", "csecret", secret, frozenset(), "https://cp.example", "atobar-flow/atobar-flow")

    class _FakeOAuthGH:
        def __init__(self, login: str, manage: set[str]) -> None:
            self._login = login
            self._manage = manage

        def exchange_code(self, cfg: web.OAuthConfig, code: str) -> str:
            return "gho_tok"

        def login(self, token: str) -> str:
            return self._login

        def repo_permissions(self, token: str) -> dict[str, dict[str, bool]]:
            return {
                "arsenyspb/ollama-monitor": {
                    "pull": True,
                    "admin": "arsenyspb/ollama-monitor" in self._manage,
                }
            }

    oauth_gh = _FakeOAuthGH("arsenyspb", {"arsenyspb/ollama-monitor"})
    cp_app.app.dependency_overrides[web.get_oauth_config] = lambda: cfg
    cp_app.app.dependency_overrides[web.get_github_user] = lambda: oauth_gh

    # Sign in as repo admin 'arsenyspb'
    r_login = client.get("/login", follow_redirects=False)
    state_val = parse_qs(urlparse(r_login.headers["location"]).query)["state"][0]
    client.get(f"/auth/callback?code=ok&state={state_val}", follow_redirects=False)
    csrf_tok = str(web.unsign(secret, client.cookies[web.SESSION_COOKIE])["csrf"])

    # 1. Add tenant via Web UI POST /tenants (delegates to FlowEngineClient -> POST /internal/v1/tenants)
    r_add = client.post(
        "/tenants",
        data={"repo": "arsenyspb/ollama-monitor", "csrf_token": csrf_tok},
        follow_redirects=False,
    )
    assert r_add.status_code == 303
    tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
    assert tenant is not None

    # 2. Update "Loop Policy & Budget Caps" (#258) via POST /tenants/{owner}/{repo}/guardrails
    assert (
        client.post(
            "/tenants/arsenyspb/ollama-monitor/guardrails",
            data={"spot_max_instance_minutes": "120"},
            follow_redirects=False,
        ).status_code
        == 403
    )
    r_guard = client.post(
        "/tenants/arsenyspb/ollama-monitor/guardrails",
        data={
            "csrf_token": csrf_tok,
            "spot_max_instance_minutes": "120",
            "max_pr_wall_clock_minutes": "90",
            "max_pr_cost_usd": "12.5",
            "max_review_rounds": "3",
            "max_repeated_finding": "2",
        },
        follow_redirects=False,
    )
    assert r_guard.status_code == 303
    assert store.get_spot_max_minutes(tenant.id) == 120
    updated_loop = store.tenant_by_id(tenant.id).manifest["loop"]
    assert updated_loop["max_pr_wall_clock_minutes"] == 90
    assert updated_loop["max_pr_cost_usd"] == 12.5
    assert updated_loop["max_review_rounds"] == 3
    assert updated_loop["max_repeated_finding"] == 2
    assert len(store.tenants_manifest_history) >= 1

    # 3. Create metric draft via UI POST (#299)
    r_draft = client.post(
        "/tenants/arsenyspb/ollama-monitor/metrics",
        data={
            "csrf_token": csrf_tok,
            "key": "dependency_count",
            "intent": "PR must not add Python dependencies compared with main",
            "kind": "guardrail",
            "rationale": "Prevent dependency bloat",
        },
        follow_redirects=False,
    )
    assert r_draft.status_code == 303
    defn = store.get_metric_definition(tenant.id, "dependency_count")
    assert defn is not None and defn["status"] == "draft"

    # Tenant detail page renders both new cards and anchor id="metric-dependency_count"
    page_html = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "Loop Policy &amp; Budget Caps" in page_html or "Loop Policy & Budget Caps" in page_html
    assert "Repository Metrics" in page_html
    assert 'id="metric-dependency_count"' in page_html
    assert "last compile failed" not in page_html

    # A compiler run that halted after its retries surfaces its reason on the metric row
    store.upsert_session({
        "instance_id": "i-comp-old", "run_id": "r-old__dependency_count", "tenant_id": tenant.id,
        "current_todo": "compile:dependency_count attempt 3", "status": "Halted",
        "halted_reason": "Halted: metric 'dependency_count' not compiled after 3 attempts: command exited 2",
        "heartbeat_at": "2026-10-20T01:00:00+00:00",
    })
    store.upsert_session({
        "instance_id": "i-other", "run_id": "r-x__dependency_countx", "tenant_id": tenant.id,
        "current_todo": "compile:dependency_countx", "status": "Running", "heartbeat_at": "2026-10-20T02:00:00+00:00",
    })
    page_html = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "last compile failed" in page_html and "not compiled after 3 attempts" in page_html

    # 4. Trigger compilation via UI POST /tenants/{owner}/{repo}/metrics/{key}/compile
    r_compile = client.post(
        "/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/compile",
        data={"csrf_token": csrf_tok},
        follow_redirects=False,
    )
    assert r_compile.status_code == 303
    assert len(ec2.calls) == 1
    comp_tags = {t["Key"]: t["Value"] for t in ec2.calls[0]["TagSpecifications"][0]["Tags"]}
    comp_run_id = comp_tags["atobar:run_id"]
    comp_cred_rec = store.get_run_credential(comp_run_id)
    assert comp_cred_rec is not None and comp_cred_rec["role"] == "metric_compiler"

    # Mint known credential for comp_run_id to submit compilation via CP gateway /v1/runs/{run_id}/metric-compilation
    raw_comp_cred = "comp-secret-cred-301"
    store.create_run_credential(
        comp_run_id,
        comp_cred_rec["tenant_id"],
        comp_cred_rec["role"],
        hashlib.sha256(raw_comp_cred.encode()).hexdigest(),
        comp_cred_rec["expires_at"],
        metric_key=comp_cred_rec.get("metric_key"),
    )

    # Unsafe command rejected with 422 via CP gateway
    r_unsafe = client.post(
        f"/v1/runs/{comp_run_id}/metric-compilation",
        headers={"Authorization": f"Bearer {raw_comp_cred}"},
        json={
            "key": "dependency_count",
            "compiled": {"run": "echo $GITHUB_TOKEN && wc -l requirements.txt", "comparator": "<=", "threshold": 9.0},
            "value": 9.0,
            "sha": "b" * 40,
            "stdout_tail": "9",
        },
    )
    assert r_unsafe.status_code == 422

    # Valid compilation via CP gateway
    r_comp_ok = client.post(
        f"/v1/runs/{comp_run_id}/metric-compilation",
        headers={"Authorization": f"Bearer {raw_comp_cred}"},
        json={
            "key": "dependency_count",
            "compiled": {
                "run": "python -c 'print(9)'",
                "comparator": "<=",
                "threshold": 9.0,
                "baseline_mode": "no_regression",
                "min_samples": 1,
            },
            "value": 9.0,
            "sha": "b" * 40,
            "stdout_tail": "9\n",
        },
    )
    assert r_comp_ok.status_code == 200
    digest = r_comp_ok.json()["digest"]

    # Page now shows compiled digest, compiled command, dry-run pass pill, and Approve button
    page_compiled = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert digest in page_compiled
    assert "print(9)" in page_compiled
    assert "pill pass" in page_compiled

    # 5. Approve with mismatched digest returns 409
    r_bad_approve = client.post(
        "/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/approve",
        data={"csrf_token": csrf_tok, "digest": "wrong-digest"},
        follow_redirects=False,
    )
    assert r_bad_approve.status_code == 409

    # 6. Approve with matching digest promotes to active and opens flow-metrics.yml PR
    r_approve = client.post(
        "/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/approve",
        data={"csrf_token": csrf_tok, "digest": digest},
        follow_redirects=False,
    )
    assert r_approve.status_code == 303
    assert store.get_metric_definition(tenant.id, "dependency_count")["status"] == "active"
    assert len(gh.opened_prs) == 1
    assert "dependency_count" in gh.opened_prs[0]["workflow_yaml"]

    # 7. Retire metric via UI POST
    r_retire = client.post(
        "/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/retire",
        data={"csrf_token": csrf_tok},
        follow_redirects=False,
    )
    assert r_retire.status_code == 303
    assert store.get_metric_definition(tenant.id, "dependency_count")["status"] == "retired"
    assert store.tenant_by_id(tenant.id).manifest["metrics"] == []

    # 8. Reader without admin permission gets 403 on all new POST routes
    oauth_gh._manage = set()
    r_login2 = client.get("/login", follow_redirects=False)
    state_val2 = parse_qs(urlparse(r_login2.headers["location"]).query)["state"][0]
    client.get(f"/auth/callback?code=ok&state={state_val2}", follow_redirects=False)
    reader_csrf = str(web.unsign(secret, client.cookies[web.SESSION_COOKIE])["csrf"])

    for route, data in [
        ("/tenants/arsenyspb/ollama-monitor/guardrails", {"spot_max_instance_minutes": "60"}),
        ("/tenants/arsenyspb/ollama-monitor/metrics", {"key": "m2", "intent": "test intent"}),
        ("/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/compile", {}),
        ("/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/approve", {"digest": digest}),
        ("/tenants/arsenyspb/ollama-monitor/metrics/dependency_count/retire", {}),
    ]:
        r_forbid = client.post(route, data={"csrf_token": reader_csrf, **data}, follow_redirects=False)
        assert r_forbid.status_code == 403, f"Expected 403 for reader on {route}"

    # 9. Path segment validation on FlowEngineClient rejects path traversal attempts with 422
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        FlowEngineClient().get_run_wiki("../evil")
    assert exc_info.value.status_code == 422

    # 10. Upstream internal token mismatch or non-JSON 200 maps to 502 flow_engine_unavailable
    bad_tok_fe = FlowEngineClient(
        base_url="http://flow-engine:8081",
        internal_token="wrong-token",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, json={"detail": "Invalid internal authentication token"}, request=req)
        ),
    )
    with pytest.raises(HTTPException) as exc_502:
        bad_tok_fe.post_events({"tenant": "arsenyspb/ollama-monitor", "event": {}}, request_id="req-bad-tok")
    assert exc_502.value.status_code == 502
    assert exc_502.value.detail == {"error": "flow_engine_unavailable", "request_id": "req-bad-tok"}


def test_devin_review_fixes_wp3(cutover_env, tmp_path, monkeypatch: pytest.MonkeyPatch):
    import base64
    import shutil
    import subprocess

    from atobar_contracts.manifest import TenantManifest
    from control_panel import llm_keys
    from control_panel.store import SupabaseStore
    from flow_engine.tenancy.render import render_metrics

    client, store, ec2, gh, bridge, admin_tok = cutover_env

    # 1. BUG_0001: render_metrics separates artifact upload (flow-metrics) and completed-run submission (report-metrics)
    m_spec = TenantManifest.model_validate(
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "metrics": [{"key": "unit_test_pass_rate", "run": "pytest -q", "comparator": ">=", "threshold": 1.0}],
        }
    )
    wf_yaml = render_metrics(m_spec, "https://cp.example.invalid")
    assert "workflow_run:" in wf_yaml
    assert "report-metrics:" in wf_yaml
    assert "github.event.workflow_run.conclusion == 'success'" in wf_yaml
    if shutil.which("actionlint"):
        wf_file = tmp_path / "flow-metrics.yml"
        wf_file.write_text(wf_yaml, encoding="utf-8")
        al_res = subprocess.run(["actionlint", str(wf_file)], capture_output=True, text=True)
        assert al_res.returncode == 0, f"actionlint failed:\n{al_res.stdout}\n{al_res.stderr}"

    # 2. BUG_0002: Retrying onboarding or compilation while an onboarder/compiler session is active reuses run_id
    r_onb1 = client.post(
        "/v1/admin/tenants/arsenyspb/ollama-monitor/onboard?issue_number=11",
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert r_onb1.status_code == 201
    run_id_1 = r_onb1.json()["run_id"]
    tenant_tok = r_onb1.json()["token"]
    assert len(ec2.calls) == 1

    r_onb2 = client.post(
        "/v1/admin/tenants/arsenyspb/ollama-monitor/onboard?issue_number=11",
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert r_onb2.status_code == 201
    assert r_onb2.json()["run_id"] == run_id_1
    assert len(ec2.calls) == 1  # No duplicate Spot instance launched!

    tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
    assert tenant is not None

    # Submitting report-metrics payload without inline values extracts values from completed run artifact (BUG_0001)
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
    store.rebuild_tenant_manifest(tenant.id)
    sha_wf = "7" * 40
    gh.runs[(tenant.repo, 777)] = {
        "id": 777,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha_wf,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 777)] = _make_metrics_zip(sha_wf, {"unit_test_pass_rate": 1.0}, run_attempt=1)
    r_wf_report = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": sha_wf,
            "pr_number": None,
            "run_id": 777,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_wf_report.status_code == 201
    assert r_wf_report.json()["recorded"] == ["unit_test_pass_rate"]
    assert r_wf_report.json()["activated"] is True

    # Follow-up 1: PR run where workflow_run.head_sha is merge commit ('8'*40) while PR head & artifact are '9'*40
    pr_head_sha = "9" * 40
    merge_sha = "8" * 40
    gh.runs[(tenant.repo, 778)] = {
        "id": 778,
        "status": "completed",
        "conclusion": "success",
        "head_sha": merge_sha,
        "pull_requests": [{"number": 42, "head": {"sha": pr_head_sha}}],
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 778)] = _make_metrics_zip(pr_head_sha, {"unit_test_pass_rate": 1.0}, run_attempt=1)
    r_pr_report = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": pr_head_sha,
            "pr_number": 42,
            "run_id": 778,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_pr_report.status_code == 201
    assert r_pr_report.json()["recorded"] == ["unit_test_pass_rate"]

    # Forged pr_number=99 or default-branch pr_number=None against PR #42's run is rejected with 422
    r_forged_pr = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": pr_head_sha,
            "pr_number": 99,
            "run_id": 778,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_forged_pr.status_code == 422

    r_forged_default = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": pr_head_sha,
            "pr_number": None,
            "run_id": 778,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_forged_default.status_code == 422

    # Malformed non-scalar pr_number={} or run_attempt=0 inside artifact returns 422 (not 500)
    bad_pr_buf = io.BytesIO()
    with zipfile.ZipFile(bad_pr_buf, "w") as zf:
        zf.writestr(
            "flow-metrics.json",
            json.dumps({"commit_sha": sha_wf, "pr_number": {}, "values": {"unit_test_pass_rate": 1.0}}),
        )
    gh.runs[(tenant.repo, 779)] = {
        "id": 779,
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha_wf,
        "path": ".github/workflows/flow-metrics.yml",
        "run_attempt": 1,
    }
    gh.artifacts[(tenant.repo, 779)] = bad_pr_buf.getvalue()
    r_bad_art_pr = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": sha_wf,
            "pr_number": None,
            "run_id": 779,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_bad_art_pr.status_code == 422

    gh.artifacts[(tenant.repo, 779)] = _make_metrics_zip(sha_wf, {"unit_test_pass_rate": 1.0}, run_attempt=0)
    r_zero_attempt = client.post(
        "/v1/metrics",
        headers={"Authorization": f"Bearer {tenant_tok}"},
        json={
            "commit_sha": sha_wf,
            "pr_number": None,
            "run_id": 779,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/flow-metrics.yml",
        },
    )
    assert r_zero_attempt.status_code == 422

    # Follow-up 2: Compiler heartbeat with changed current_todo still deduplicates repeat compile calls
    fe_headers = {"Authorization": "Bearer fe-cutover-secret-301"}
    store.upsert_metric_definition(tenant.id, {"key": "cov_metric", "intent": "coverage", "status": "draft"})
    ec2_before = len(ec2.calls)
    r_c1 = fe_api.app.dependency_overrides[fe_api.get_store]()  # verify store
    assert r_c1 is store
    from fastapi.testclient import TestClient as _TC

    fe_tc = _TC(fe_api.app)
    rc1 = fe_tc.post(f"/internal/v1/tenants/{tenant.id}/metrics/cov_metric/compile", headers=fe_headers)
    assert rc1.status_code == 200
    comp_rid = rc1.json()["run_id"]
    assert len(ec2.calls) == ec2_before + 1

    # Compiler sends heartbeat changing current_todo to 'install dependencies' (marks pending-{comp_rid} dispatched)
    fe_tc.post(
        f"/internal/v1/runs/{comp_rid}/heartbeat",
        headers=fe_headers,
        json={
            "status": "Running",
            "instance_id": "i-comp-active-1",
            "issue_id": 0,
            "current_todo": "install dependencies",
        },
    )
    assert store.sessions[f"pending-{comp_rid}"]["status"] == "dispatched"
    rc2 = fe_tc.post(f"/internal/v1/tenants/{tenant.id}/metrics/cov_metric/compile", headers=fe_headers)
    assert rc2.status_code == 200
    assert rc2.json()["status"] == "already_compiling"
    assert rc2.json()["run_id"] == comp_rid
    assert len(ec2.calls) == ec2_before + 1  # No duplicate Spot instance launched!

    # Legacy unsuffixed compiler run_id with dispatched pending session and running session also deduplicates
    store.upsert_metric_definition(tenant.id, {"key": "legacy_metric", "intent": "legacy", "status": "draft"})
    store.upsert_session(
        {
            "instance_id": "pending-run_legacy_abc",
            "run_id": "run_legacy_abc",
            "tenant_id": tenant.id,
            "role": "metric_compiler",
            "status": "dispatched",
            "current_todo": "compile:legacy_metric",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store.upsert_session(
        {
            "instance_id": "i-legacy-comp",
            "run_id": "run_legacy_abc",
            "tenant_id": tenant.id,
            "role": "metric_compiler",
            "status": "Running",
            "current_todo": "install dependencies",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    rc_legacy = fe_tc.post(f"/internal/v1/tenants/{tenant.id}/metrics/legacy_metric/compile", headers=fe_headers)
    assert rc_legacy.status_code == 200
    assert rc_legacy.json()["status"] == "already_compiling"
    assert rc_legacy.json()["run_id"] == "run_legacy_abc"
    assert len(ec2.calls) == ec2_before + 1

    # Legacy unsuffixed compiler run cannot overwrite another metric after heartbeat changes current_todo
    store.create_run_credential("run_legacy_abc", tenant.id, "metric_compiler", "h-leg", "2099-01-01T00:00:00Z")
    r_leg_cross = fe_tc.post(
        "/internal/v1/runs/run_legacy_abc/metric-compilation",
        headers=fe_headers,
        json={
            "key": "cov_metric",
            "compiled": {"run": "echo 1", "comparator": ">=", "threshold": 1.0},
            "value": 1.0,
            "sha": "a" * 40,
        },
    )
    assert r_leg_cross.status_code == 403

    # 3. BUG_0003: POST /v1/admin/gc/sweep forwards to FE and halts stale sessions + marks credentials used
    stale_run_id = "run-stale-gc-1"
    store.create_run_credential(stale_run_id, tenant.id, "developer", "h-stale", "2099-01-01T00:00:00Z")
    store.upsert_session(
        {
            "instance_id": "i-stale-gc-1",
            "run_id": stale_run_id,
            "tenant_id": tenant.id,
            "role": "developer",
            "target_repo": tenant.repo,
            "issue_id": 15,
            "status": "Running",
            "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        }
    )
    r_gc = client.post("/v1/admin/gc/sweep", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r_gc.status_code == 200
    assert "i-stale-gc-1" in r_gc.json()["reaped_sessions"]
    assert store.sessions["i-stale-gc-1"]["status"] == "Halted: stale_heartbeat"
    assert store.get_run_credential(stale_run_id)["used_at"] is not None

    # 4. BUG_0005: POST /v1/runs/{run_id}/llm-key forwards credential-used to FE
    kek = b"k" * 32
    cp_app.app.dependency_overrides[llm_keys.get_kek] = lambda: kek
    store.put_llm_key(
        tenant.id,
        "gemini",
        {"provider": "gemini", "ciphertext": llm_keys.encrypt(kek, tenant.id, "gemini", "gem-key-999")},
    )
    store.set_role_llm_pref(tenant.id, "developer", "gemini", "gemini-3.8-flash", "high")
    key_run_id = "run-llm-audit-1"
    raw_key_cred = "cred-llm-audit-1"
    store.create_run_credential(
        key_run_id,
        tenant.id,
        "developer",
        hashlib.sha256(raw_key_cred.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )
    assert store.get_run_credential(key_run_id).get("used_at") is None
    r_llm = client.post(
        f"/v1/runs/{key_run_id}/llm-key",
        headers={"Authorization": f"Bearer {raw_key_cred}"},
    )
    assert r_llm.status_code == 200
    assert any(req.url.path == f"/internal/v1/runs/{key_run_id}/credential-used" for req in bridge.requests)
    assert store.get_run_credential(key_run_id).get("used_at") is not None

    # 5. BUG_0004 & Follow-up 3: Screenshot upload fails visibly with 502 and preserves any prior screenshot on disk
    screen_dir = tmp_path / "screens-fail"
    monkeypatch.setenv("CP_SCREENSHOT_DIR", str(screen_dir))
    monkeypatch.setenv("CP_ARTIFACT_SIGNING_KEY", "artifact-signing-secret-key-32bytes!!")
    first_png = b"\x89PNG\r\n\x1a\nfirst-valid-image"
    r_scr_ok = client.post(
        f"/v1/runs/{key_run_id}/screenshots",
        headers={"Authorization": f"Bearer {raw_key_cred}"},
        json={
            "filename": "capture.png",
            "data_base64": base64.b64encode(first_png).decode("ascii"),
        },
    )
    assert r_scr_ok.status_code == 201
    first_signed_path = "/" + r_scr_ok.json()["url"].split("/", 3)[-1]

    down_fe = FlowEngineClient(
        base_url="http://flow-engine:8081",
        internal_token="fe-cutover-secret-301",
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="unavailable", request=req)),
    )
    cp_app.app.dependency_overrides[get_flow_engine_client] = lambda: down_fe
    r_scr_fail = client.post(
        f"/v1/runs/{key_run_id}/screenshots",
        headers={"Authorization": f"Bearer {raw_key_cred}"},
        json={
            "filename": "capture.png",
            "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nreplacement-image").decode("ascii"),
        },
    )
    assert r_scr_fail.status_code == 502
    # Original capture.png is preserved and still served by its signed URL!
    r_get_orig = client.get(first_signed_path)
    assert r_get_orig.status_code == 200
    assert r_get_orig.content == first_png
    assert "X-Idempotency-Key" not in down_fe._headers(request_id="req-1")

    # 6. SEC_0001: get_flow_engine_client never uses local fallback transport for SupabaseStore even if FE_URL is unset
    monkeypatch.delenv("FE_URL", raising=False)
    monkeypatch.delenv("FLOW_ENGINE_URL", raising=False)
    supa_store = SupabaseStore("https://example.supabase.co", "fake-service-key")
    prod_fe = get_flow_engine_client(store=supa_store)
    assert prod_fe._transport is None
    assert prod_fe.base_url == "http://flow-engine:8081"

    # 7. SEC_0002: MemoryStore rejects unscoped observations and never leaks observations across tenants
    with pytest.raises(ValueError, match="tenant_id"):
        store.record_observations([{"key": "unit_test_pass_rate", "value": 1.0, "commit_sha": sha_wf}])
    other_t = store.create_tenant("other/repo", "other")
    assert store.observed_metric_keys(other_t.id) == set()
    assert store.recent_observations(other_t.id) == []
    assert store.default_branch_observations(other_t.id) == []

