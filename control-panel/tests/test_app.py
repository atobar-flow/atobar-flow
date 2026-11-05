from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from control_panel import app as cp
from control_panel.store import Tenant
from control_panel.tokens import hash_token

ADMIN = "admin-secret"


class FakeStore:
    def __init__(self) -> None:
        self.tenants = {
            "t-a": Tenant("t-a", "arsenyspb/ollama-monitor", "active", "shadow", {"k": 1}),
            "t-b": Tenant("t-b", "arsenyspb/other", "active", "recommend", {}),
        }
        self.tokens: dict[str, dict[str, Any]] = {}
        self.metric_defs = {"t-a": {"unit_test_pass_rate"}, "t-b": {"other_metric"}}
        self.observations: list[dict[str, Any]] = []
        self.wikis = {"t-a": {"repo_name": "ollama-monitor", "purpose": "A"}, "t-b": {"purpose": "B"}}
        self.run_credentials: dict[str, dict[str, Any]] = {}
        self.loop_rounds: list[dict[str, Any]] = []
        self.sessions: dict[str, dict[str, Any]] = {}

    def tenant_for_token_hash(self, token_hash: str) -> Optional[Tenant]:
        row = self.tokens.get(token_hash)
        return self.tenants[row["tenant_id"]] if row and not row["revoked"] else None

    def tenant_by_repo(self, repo: str) -> Optional[Tenant]:
        return next((t for t in self.tenants.values() if t.repo == repo), None)

    def tenant_by_id(self, tenant_id: str) -> Optional[Tenant]:
        return self.tenants.get(tenant_id)

    def wiki_for_tenant(self, tenant_id: str) -> Optional[dict[str, Any]]:
        return self.wikis.get(tenant_id)

    def upsert_wiki(self, tenant_id: str, repo_name: str, wiki_row: dict[str, Any]) -> None:
        self.wikis[tenant_id] = {"tenant_id": tenant_id, "repo_name": repo_name, **wiki_row}

    def update_wiki_state(self, tenant_id: str, repo_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        cur = dict(self.wikis.get(tenant_id) or {"tenant_id": tenant_id, "repo_name": repo_name})
        cur.update(patch)
        self.wikis[tenant_id] = cur
        return dict(cur)

    def record_wiki_default_branch_merge(self, tenant_id: str, repo_name: str, commit_sha: str) -> dict[str, Any]:
        cur = dict(self.wikis.get(tenant_id) or {"tenant_id": tenant_id, "repo_name": repo_name})
        prev = str(cur.get("last_observed_sha") or cur.get("commit_sha") or "")
        if commit_sha and commit_sha != prev:
            cur["merges_since_refresh"] = int(cur.get("merges_since_refresh") or 0) + 1
            cur["last_observed_sha"] = commit_sha
        self.wikis[tenant_id] = cur
        return dict(cur)

    def claim_wiki_refresh(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        now: Optional[Any] = None,
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        cur = dict(self.wikis.get(tenant_id) or {"tenant_id": tenant_id, "repo_name": repo_name})
        active = str(cur.get("refresh_run_id") or "")
        if not active:
            live = self.active_session_for_role(tenant_id, "onboarder")
            if live and str(live.get("run_id") or "") != run_id:
                active = str(live.get("run_id") or "")
        if active and active != run_id:
            cur["refresh_queued"] = True
            cur["refresh_status"] = "queued"
            self.wikis[tenant_id] = cur
            return {"claimed": False, "queued": True, "active_run_id": active}
        cur["refresh_run_id"] = run_id
        cur["refresh_status"] = "running"
        cur["refresh_queued"] = False
        cur["last_error"] = None
        self.wikis[tenant_id] = cur
        return {"claimed": True, "queued": False, "active_run_id": run_id}

    def complete_wiki_refresh_run(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        halted_error: Optional[str] = None,
    ) -> dict[str, Any]:
        cur = dict(self.wikis.get(tenant_id) or {})
        if not cur or str(cur.get("refresh_run_id") or "") != run_id:
            return {"finalized": False, "drain_queued": False}
        was_queued = bool(cur.get("refresh_queued", False))
        cur["refresh_run_id"] = None
        cur["refresh_queued"] = False
        cur["refresh_status"] = "queued" if was_queued else ("failed" if halted_error else "idle")
        cur["last_error"] = halted_error
        self.wikis[tenant_id] = cur
        return {"finalized": True, "drain_queued": was_queued}

    def active_session_for_role(
        self,
        tenant_id: str,
        role: str,
        *,
        issue_id: Optional[int] = None,
        metric_key: Optional[str] = None,
        now: Optional[Any] = None,
    ) -> Optional[dict[str, Any]]:
        for s in self.sessions.values():
            if s.get("tenant_id") == tenant_id and s.get("role") == role:
                if issue_id is not None and int(s.get("issue_id") or 0) != int(issue_id):
                    continue
                if str(s.get("status") or "").lower() in ("running", "pending", "active"):
                    return dict(s)
        return None

    def upsert_session(self, row: dict[str, Any]) -> None:
        inst_id = str(row.get("instance_id") or "")
        if inst_id:
            self.sessions[inst_id] = dict(row)

    def record_security_audit(
        self,
        event_type: str,
        *,
        tenant_id: Optional[str] = None,
        actor: str = "system",
        run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        pass

    def create_run_credential(self, run_id: str, tenant_id: str, role: str, cred_hash: str, expires_at: str) -> None:
        self.run_credentials[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "role": role,
            "cred_hash": cred_hash,
            "expires_at": expires_at,
            "used": False,
        }

    def get_run_credential(self, run_id: str) -> Optional[dict[str, Any]]:
        return self.run_credentials.get(run_id)

    def record_loop_round(self, row: dict[str, Any]) -> None:
        self.loop_rounds.append(row)

    def loop_rounds_for_pr(self, tenant_id: str, pr_number: int) -> list[dict[str, Any]]:
        return [r for r in self.loop_rounds if r.get("tenant_id") == tenant_id and r.get("pr_number") == pr_number]

    def update_session(self, instance_id: str, updates: dict[str, Any]) -> None:
        sess = self.sessions.setdefault(instance_id, {"instance_id": instance_id})
        sess.update(updates)

    def add_token(self, tenant_id: str, token_hash: str, owner: str, label: str) -> None:
        self.tokens[token_hash] = {"tenant_id": tenant_id, "revoked": False}

    def revoke_tokens(self, tenant_id: str) -> int:
        n = 0
        for row in self.tokens.values():
            if row["tenant_id"] == tenant_id and not row["revoked"]:
                row["revoked"] = True
                n += 1
        return n

    def metric_keys(self, tenant_id: str) -> set[str]:
        return self.metric_defs.get(tenant_id, set())

    def observed_metric_keys(self, tenant_id: str) -> set[str]:
        return {r["key"] for r in self.observations if r["tenant_id"] == tenant_id and r.get("pr_number") is None}

    def update_tenant_status(self, tenant_id: str, status: str) -> None:
        if tenant_id in self.tenants:
            t = self.tenants[tenant_id]
            self.tenants[tenant_id] = Tenant(t.id, t.repo, status, t.arbitration_mode, t.manifest)

    def record_observations(self, rows: list[dict[str, Any]]) -> None:
        self.observations.extend(rows)


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.permissions: dict[tuple[str, str], str] = {}
        self.removed_labels: list[tuple[str, int, str]] = []
        self.issue_comments: list[tuple[str, int, str]] = []
        self.manifest_pr_comments: list[tuple[str, str]] = []

    def dispatch(self, event_type: str, client_payload: dict[str, Any]) -> None:
        self.calls.append((event_type, client_payload))

    def get_collaborator_permission(self, repo: str, username: str) -> Optional[str]:
        return self.permissions.get((repo, username))

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.removed_labels.append((repo, issue_number, label))

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self.issue_comments.append((repo, issue_number, body))

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        self.issue_comments.append((repo, 0, comment))
        return True

    def comment_on_manifest_pr(self, repo: str, comment: str) -> bool:
        self.manifest_pr_comments.append((repo, comment))
        return True


@pytest.fixture
def env():
    store, disp = FakeStore(), FakeDispatcher()
    cp.app.dependency_overrides[cp.get_store] = lambda: store
    cp.app.dependency_overrides[cp.get_dispatcher] = lambda: disp
    cp.app.dependency_overrides[cp.get_admin_digest] = lambda: hash_token(ADMIN)
    yield TestClient(cp.app), store, disp
    cp.app.dependency_overrides.clear()


def _issue(client: TestClient, repo: str) -> str:
    r = client.post(
        f"/v1/admin/tenants/{repo}/tokens",
        json={"owner_github_login": "arsenyspb"},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert r.status_code == 201
    return r.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_healthz(env):
    client, _, _ = env
    assert client.get("/healthz").json() == {"status": "ok"}


def test_token_only_stored_as_hash(env):
    client, store, _ = env
    token = _issue(client, "arsenyspb/ollama-monitor")
    assert token.startswith("atb_")
    assert token not in store.tokens and hash_token(token) in store.tokens


def test_admin_required_to_issue(env):
    client, _, _ = env
    r = client.post("/v1/admin/tenants/arsenyspb/ollama-monitor/tokens", json={"owner_github_login": "x"})
    assert r.status_code == 401
    r = client.post(
        "/v1/admin/tenants/arsenyspb/ollama-monitor/tokens",
        json={"owner_github_login": "x"},
        headers=_auth("wrong"),
    )
    assert r.status_code == 401


def test_unknown_tenant_404(env):
    client, _, _ = env
    r = client.post(
        "/v1/admin/tenants/arsenyspb/nope/tokens", json={"owner_github_login": "x"}, headers=_auth(ADMIN)
    )
    assert r.status_code == 404


def test_tenant_sees_only_own_data(env):
    client, _, _ = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    assert client.get("/v1/tenant", headers=_auth(token_a)).json()["repo"] == "arsenyspb/ollama-monitor"
    assert client.get("/v1/tenant/wiki", headers=_auth(token_a)).json()["purpose"] == "A"


def test_admin_token_is_not_a_tenant_token(env):
    client, _, _ = env
    assert client.get("/v1/tenant", headers=_auth(ADMIN)).status_code == 401


def test_event_target_repo_comes_from_token_not_body(env):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    r = client.post(
        "/v1/events",
        json={"kind": "issues.labeled", "issue_number": 7, "label": "flow:fix", "target_repo": "arsenyspb/other"},
        headers=_auth(token_a),
    )
    assert r.status_code == 202 and r.json()["dispatched"] is True
    assert disp.calls[0][0] == "flow_run"
    call_payload = disp.calls[0][1]
    assert call_payload["target_repo"] == "arsenyspb/ollama-monitor"
    assert call_payload["issue_id"] == 7
    assert call_payload["label"] == "flow:fix"
    assert "run_id" in call_payload
    assert "run_credential" in call_payload
    assert call_payload["agent_runtime"] == "nanobot"


def test_non_flow_label_not_dispatched(env):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    r = client.post(
        "/v1/events", json={"kind": "issues.labeled", "issue_number": 7, "label": "bug"}, headers=_auth(token_a)
    )
    assert r.json()["dispatched"] is False and disp.calls == []


def test_revoked_token_rejected(env):
    client, _, _ = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    r = client.delete("/v1/admin/tenants/arsenyspb/ollama-monitor/tokens", headers=_auth(ADMIN))
    assert r.json()["revoked"] == 1
    assert client.get("/v1/tenant", headers=_auth(token_a)).status_code == 401


def test_paused_tenant_cannot_dispatch(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "paused", "shadow", {})
    r = client.post(
        "/v1/events", json={"kind": "issues.labeled", "issue_number": 1, "label": "flow:fix"}, headers=_auth(token_a)
    )
    assert r.status_code == 409 and disp.calls == []


SHA = "a" * 40


def test_metrics_recorded_for_own_tenant(env):
    client, store, _ = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"commit_sha": SHA, "workflow_path": "w.yml", "values": {"unit_test_pass_rate": 1.0}}
    r = client.post("/v1/metrics", json=body, headers=_auth(token_a))
    assert r.status_code == 201
    [row] = store.observations
    assert row["tenant_id"] == "t-a" and row["value"] == 1.0 and len(row["artifact_sha256"]) == 64


def test_metrics_reject_undeclared_or_other_tenant_keys(env):
    client, store, _ = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"commit_sha": SHA, "workflow_path": "x", "values": {"other_metric": 1.0}}
    assert client.post("/v1/metrics", json=body, headers=_auth(token_a)).status_code == 422
    assert store.observations == []


def test_metrics_reject_bad_sha(env):
    client, _, _ = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"commit_sha": "HEAD", "workflow_path": "x", "values": {"unit_test_pass_rate": 1.0}}
    assert client.post("/v1/metrics", json=body, headers=_auth(token_a)).status_code == 422


def test_ac1_partial_metrics_keep_tenant_onboarding(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "onboarding", "shadow", {})
    store.metric_defs["t-a"] = {"unit_test_pass_rate", "dashboard_overflow_px"}
    body = {"commit_sha": SHA, "workflow_path": "w.yml", "values": {"unit_test_pass_rate": 1.0}}
    r = client.post("/v1/metrics", json=body, headers=_auth(token_a))
    assert r.status_code == 201
    assert r.json().get("activated") is not True
    assert store.tenants["t-a"].status == "onboarding"


def test_ac2_pr_observations_never_activate(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "onboarding", "shadow", {})
    store.metric_defs["t-a"] = {"unit_test_pass_rate"}
    body = {"commit_sha": SHA, "pr_number": 42, "workflow_path": "w.yml", "values": {"unit_test_pass_rate": 1.0}}
    r = client.post("/v1/metrics", json=body, headers=_auth(token_a))
    assert r.status_code == 201
    assert r.json().get("activated") is not True
    assert store.tenants["t-a"].status == "onboarding"


def test_ac3_paused_tenants_never_reactivated(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "paused", "shadow", {})
    body = {"commit_sha": SHA, "workflow_path": "w.yml", "values": {"unit_test_pass_rate": 1.0}}
    r = client.post("/v1/metrics", json=body, headers=_auth(token_a))
    assert r.status_code == 409
    assert store.tenants["t-a"].status == "paused"


def test_all_default_branch_metrics_activate_tenant_and_post_comment(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "onboarding", "shadow", {})
    store.metric_defs["t-a"] = {"unit_test_pass_rate", "dashboard_overflow_px"}

    # First observation (partial)
    body1 = {"commit_sha": SHA, "workflow_path": "w.yml", "values": {"unit_test_pass_rate": 1.0}}
    r1 = client.post("/v1/metrics", json=body1, headers=_auth(token_a))
    assert r1.status_code == 201
    assert store.tenants["t-a"].status == "onboarding"

    # Second observation completes all metric definitions
    body2 = {"commit_sha": SHA, "workflow_path": "w.yml", "values": {"dashboard_overflow_px": 0.0}}
    r2 = client.post("/v1/metrics", json=body2, headers=_auth(token_a))
    assert r2.status_code == 201
    assert r2.json().get("activated") is True
    assert store.tenants["t-a"].status == "active"
    assert any("active" in c[2].lower() and "flow:feature" in c[2] for c in disp.issue_comments)


@pytest.mark.parametrize("label", ["flow:metrics", "flow:approved"])
def test_control_labels_never_start_agents(env, label):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"kind": "issues.labeled", "issue_number": 3, "label": label}
    r = client.post("/v1/events", json=body, headers=_auth(token_a))
    assert r.status_code == 202 and r.json()["dispatched"] is False and disp.calls == []


def test_flow_approved_label_handling_removed_from_events(env):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"kind": "issues.labeled", "issue_number": 10, "label": "flow:approved", "sender": "alice"}
    r = client.post("/v1/events", json=body, headers=_auth(token_a))
    assert r.status_code == 202
    assert r.json()["dispatched"] is False
    assert disp.calls == []
    assert disp.removed_labels == []
    assert disp.manifest_pr_comments == []


def test_flow_onboard_dispatches_flow_onboard_once_and_no_flow_run(env):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"kind": "issues.labeled", "issue_number": 42, "label": "flow:onboard"}
    r = client.post("/v1/events", json=body, headers=_auth(token_a))
    assert r.status_code == 202 and r.json()["dispatched"] is True
    assert disp.calls == [("flow_onboard", {"repo": "arsenyspb/ollama-monitor", "issue_number": 42})]
    assert not any(call[0] == "flow_run" for call in disp.calls)


def test_flow_onboard_issues_edited_dispatches_flow_onboard(env):
    client, _, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    body = {"kind": "issues.edited", "issue_number": 42, "label": "flow:onboard"}
    r = client.post("/v1/events", json=body, headers=_auth(token_a))
    assert r.status_code == 202 and r.json()["dispatched"] is True
    assert disp.calls == [("flow_onboard", {"repo": "arsenyspb/ollama-monitor", "issue_number": 42})]


def test_onboarding_tenant_records_but_does_not_dispatch(env):
    client, store, disp = env
    token_a = _issue(client, "arsenyspb/ollama-monitor")
    store.tenants["t-a"] = Tenant("t-a", "arsenyspb/ollama-monitor", "onboarding", "shadow", {})
    r = client.post(
        "/v1/events", json={"kind": "issues.labeled", "issue_number": 1, "label": "flow:fix"}, headers=_auth(token_a)
    )
    assert r.status_code == 202 and r.json()["dispatched"] is False and disp.calls == []


def test_onboard_trigger_template(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("CP_PUBLIC_URL", "https://cp.example/")
    r = client.get("/onboard/flow-trigger.yml", params={"repo": "alice/new"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "tenants/alice__new.json" in r.text and "https://cp.example/v1/events" in r.text
    assert "secrets.ATOBAR_TENANT_TOKEN" in r.text
    assert client.get("/onboard/flow-trigger.yml", params={"repo": "x/y; rm -rf /"}).status_code == 422
    for bad in ["alice/new\n", "alice/new\nname: evil", "../repo", "owner/..", "./x", "x/."]:
        assert client.get("/onboard/flow-trigger.yml", params={"repo": bad}).status_code == 422, bad
    assert client.get("/onboard/flow-trigger.yml", params={"repo": "alice/.github"}).status_code == 200
