import dataclasses
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_app import FakeStore

from control_panel import app as cp
from control_panel import llm_keys, web
from control_panel.llm_keys import Provider
from control_panel.store import Tenant
from control_panel.tokens import hash_token

SECRET = "s" * 40
SHA = "a" * 40
KEK = bytes(range(32))
GEMINI_KEY = "fake-gemini-test-key-abcd"


class WebStore(FakeStore):
    def set_arbitration_mode(self, tenant_id: str, mode: str) -> None:
        t = dataclasses.replace(self.tenants[tenant_id], arbitration_mode=mode)
        self.tenants[tenant_id] = t

    def __init__(self) -> None:
        super().__init__()
        self.keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.defs = {
            "t-a": [{"key": "unit_test_pass_rate", "comparator": ">=", "threshold": 1.0, "baseline_mode": "absolute"}],
            "t-b": [{"key": "other_metric", "comparator": "<=", "threshold": 0.0, "baseline_mode": "absolute"}],
        }
        self.obs = {
            "t-a": [{"key": "unit_test_pass_rate", "value": 0.5, "commit_sha": SHA, "pr_number": None,
                     "workflow_path": "w", "observed_at": "2020-01-01T00:00:00+00:00"}],
        }

    def list_tenants(self) -> list[Tenant]:
        return sorted(self.tenants.values(), key=lambda t: t.repo)

    def metric_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.defs.get(tenant_id, [])

    def recent_observations(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.obs.get(tenant_id, [])[:limit]

    def create_tenant(self, repo: str, created_by: str) -> Tenant:
        t = Tenant(f"t-{len(self.tenants)}", repo, "onboarding", "shadow", {"created_by": created_by})
        self.tenants[t.id] = t
        return t

    def active_tokens(self, tenant_id: str) -> list[dict[str, Any]]:
        return [{"label": "ci"} for r in self.tokens.values() if r["tenant_id"] == tenant_id and not r["revoked"]]

    def role_llm_prefs(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        return {r: dict(v) for (t, r), v in getattr(self, "role_llm", {}).items() if t == tenant_id}

    def set_role_llm_pref(self, tenant_id: str, role: str, provider: str, model: str, reasoning_effort: str) -> None:
        self.__dict__.setdefault("role_llm", {})[(tenant_id, role)] = {
            "provider": provider, "model": model, "reasoning_effort": reasoning_effort
        }

    def llm_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in r.items() if k != "ciphertext"} | {"provider": p}
            for (tid, p), r in self.keys.items()
            if tid == tenant_id
        ]

    def put_llm_key(self, tenant_id: str, provider: str, row: dict[str, Any]) -> None:
        self.keys[(tenant_id, provider)] = row

    def delete_llm_key(self, tenant_id: str, provider: str) -> None:
        self.keys.pop((tenant_id, provider), None)

    def get_tenant_llm_ciphertext(self, tenant_id: str, provider: str) -> Any:
        row = self.keys.get((tenant_id, provider))
        return row.get("ciphertext") if row else None


class FakeGitHub:
    def __init__(self, login: str, readable: set[str], manage: frozenset[str] = frozenset()) -> None:
        self._login, self.readable, self.manage = login, readable, manage

    def exchange_code(self, cfg: web.OAuthConfig, code: str) -> str:
        return "gho_user_token"

    def login(self, token: str) -> str:
        return self._login

    def repo_permissions(self, token: str) -> dict[str, dict[str, bool]]:
        return {r: {"pull": True, "admin": r in self.manage} for r in self.readable | self.manage}


def _cfg(admins: frozenset[str] = frozenset()) -> web.OAuthConfig:
    return web.OAuthConfig("cid", "csecret", SECRET, admins, "https://cp.example", "atobar-flow/atobar-flow")


@pytest.fixture
def ui():
    store = WebStore()
    state: dict[str, Any] = {
        "cfg": _cfg(),
        "gh": FakeGitHub("alice", {"arsenyspb/ollama-monitor"}),
        "kek": KEK,
        "valid": True,
        "validated": [],
    }

    def validate(key: str) -> tuple[bool, list[str]]:
        state["validated"].append(key)
        return bool(state["valid"]), ["model-1"] if state["valid"] else []

    providers = {"gemini": Provider("gemini", "Google Gemini", "https://example", validate)}
    cp.app.dependency_overrides[cp.get_store] = lambda: store
    cp.app.dependency_overrides[web.get_kek] = lambda: state["kek"]
    cp.app.dependency_overrides[web.get_providers] = lambda: providers
    cp.app.dependency_overrides[web.get_oauth_config] = lambda: state["cfg"]
    cp.app.dependency_overrides[web.get_github_user] = lambda: state["gh"]
    yield TestClient(cp.app, base_url="https://testserver"), store, state
    cp.app.dependency_overrides.clear()


def _sign_in(client: TestClient) -> Any:
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["https://cp.example/auth/callback"]
    return client.get(f"/auth/callback?code=c&state={q['state'][0]}", follow_redirects=False)


def test_anonymous_sees_sign_in(ui):
    client, _, _ = ui
    r = client.get("/")
    assert "Sign in with GitHub" in r.text and "ollama-monitor" not in r.text


def test_login_unconfigured_is_503(ui):
    client, _, state = ui
    state["cfg"] = None
    assert client.get("/login", follow_redirects=False).status_code == 503
    assert "not configured" in client.get("/").text


def test_callback_rejects_bad_state(ui):
    client, _, _ = ui
    client.get("/login", follow_redirects=False)
    assert client.get("/auth/callback?code=c&state=forged", follow_redirects=False).status_code == 400


def test_user_sees_only_readable_tenants(ui):
    client, _, _ = ui
    r = _sign_in(client)
    assert r.status_code == 303
    assert "gho_user_token" not in r.headers["set-cookie"]
    home = client.get("/").text
    assert "arsenyspb/ollama-monitor" in home and "arsenyspb/other" not in home
    assert "0/1 passing" in home
    detail = client.get("/tenants/arsenyspb/ollama-monitor")
    assert detail.status_code == 200
    assert "unit_test_pass_rate" in detail.text and "pill fail" in detail.text
    assert "flow%3Ametrics" in detail.text
    assert detail.headers["x-frame-options"] == "DENY"
    assert client.get("/tenants/arsenyspb/other").status_code == 404


def test_admin_sees_all(ui):
    client, _, state = ui
    state["cfg"] = _cfg(frozenset({"boss"}))
    state["gh"] = FakeGitHub("Boss", set())
    _sign_in(client)
    home = client.get("/").text
    assert "arsenyspb/ollama-monitor" in home and "arsenyspb/other" in home


def test_tampered_session_is_anonymous(ui):
    client, _, _ = ui
    forged = web.sign("x" * 40, {"login": "eve", "repos": ["arsenyspb/other"], "admin": True, "exp": 9999999999})
    client.cookies.set(web.SESSION_COOKIE, forged)
    assert "Sign in with GitHub" in client.get("/").text
    assert client.get("/tenants/arsenyspb/other", follow_redirects=False).status_code == 303


def test_expired_session_rejected():
    assert web.unsign(SECRET, web.sign(SECRET, {"login": "a", "exp": 1})) is None


def test_logout_rejects_cross_site(ui):
    client, _, _ = ui
    _sign_in(client)
    assert client.post("/logout", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/logout", follow_redirects=False).status_code == 403
    r = client.post("/logout", data={"csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and web.SESSION_COOKIE in r.headers["set-cookie"]


@pytest.mark.parametrize(
    "value,cmp,thr,ok",
    [(1, ">=", 1, True), (0.9, ">=", 1, False), (0, "<=", 0, True), (1, "==", 1, True), (1, "!=", 1, False)],
)
def test_passes(value: float, cmp: str, thr: float, ok: bool) -> None:
    assert web.passes(value, cmp, thr) is ok




def _csrf(client: TestClient) -> str:
    payload = web.unsign(SECRET, client.cookies[web.SESSION_COOKIE])
    assert payload is not None
    return str(payload["csrf"])


def _manager(ui: Any) -> tuple[TestClient, WebStore, dict[str, Any]]:
    client, store, state = ui
    manage = frozenset({"alice/new", "arsenyspb/ollama-monitor"})
    state["gh"] = FakeGitHub("alice", {"arsenyspb/ollama-monitor"}, manage)
    _sign_in(client)
    return client, store, state


def test_admin_of_repo_can_add_it_as_tenant(ui):
    client, store, _ = _manager(ui)
    home = client.get("/").text
    assert "alice/new" in home and "Add to atobar-flow" in home
    r = client.post("/tenants", data={"repo": "alice/new", "csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tenants/alice/new"
    t = store.tenant_by_repo("alice/new")
    assert t is not None and t.status == "onboarding" and t.arbitration_mode == "shadow"
    assert client.get("/tenants/alice/new").status_code == 200


def test_add_tenant_requires_repo_admin_and_csrf(ui):
    client, store, _ = _manager(ui)
    assert client.post("/tenants", data={"repo": "evil/repo", "csrf": _csrf(client)}).status_code == 403
    assert client.post("/tenants", data={"repo": "alice/new", "csrf": "forged"}).status_code == 403
    assert client.post("/tenants", data={"repo": "alice/new"}).status_code == 403
    r = client.post(
        "/tenants", data={"repo": "alice/new", "csrf": _csrf(client)}, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403 and store.tenant_by_repo("alice/new") is None


def test_rotate_token_shows_once_and_stores_hash(ui):
    client, store, _ = _manager(ui)
    csrf = _csrf(client)
    first = client.post("/tenants/arsenyspb/ollama-monitor/token", data={"csrf": csrf})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    # we now use navigator.clipboard.writeText('atb_...')
    tok1 = first.text.split("navigator.clipboard.writeText('")[1].split("'")[0]
    assert tok1.startswith("atb_") and hash_token(tok1) in store.tokens and tok1 not in str(store.tokens)
    assert "gh secret set ATOBAR_TENANT_TOKEN -R arsenyspb/ollama-monitor" in first.text
    tok2_html = client.post("/tenants/arsenyspb/ollama-monitor/token", data={"csrf": csrf}).text
    tok2 = tok2_html.split("navigator.clipboard.writeText('")[1].split("'")[0]
    assert store.tokens[hash_token(tok1)]["revoked"] and not store.tokens[hash_token(tok2)]["revoked"]
    assert tok2 not in client.get("/tenants/arsenyspb/ollama-monitor").text


def test_reader_cannot_manage(ui):
    client, _, _ = ui
    _sign_in(client)
    page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "Tenant API token" not in page and "LLM keys" not in page
    csrf = _csrf(client)
    assert client.post("/tenants/arsenyspb/ollama-monitor/token", data={"csrf": csrf}).status_code == 403
    r = client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data={"csrf": csrf, "provider": "gemini",
                                                                        "api_key": GEMINI_KEY})
    assert r.status_code == 403


def test_llm_key_is_validated_encrypted_and_never_echoed(ui):
    client, store, state = _manager(ui)
    data = {"csrf": _csrf(client), "provider": "gemini", "api_key": GEMINI_KEY}
    r = client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data=data, follow_redirects=False)
    assert r.status_code == 303 and state["validated"] == [GEMINI_KEY]
    row = store.keys[("t-a", "gemini")]
    assert GEMINI_KEY not in str(row) and row["key_last4"] == "abcd" and row["created_by"] == "alice"
    assert llm_keys.decrypt(KEK, "t-a", "gemini", row["ciphertext"]) == GEMINI_KEY
    page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "…abcd" in page and GEMINI_KEY not in page
    rm = client.post("/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete", data={"csrf": data["csrf"]})
    assert rm.status_code == 200 and ("t-a", "gemini") not in store.keys


def test_llm_key_rejected_or_unconfigured(ui):
    client, store, state = _manager(ui)
    data = {"csrf": _csrf(client), "provider": "gemini", "api_key": GEMINI_KEY}
    state["valid"] = False
    assert client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data=data).status_code == 422
    assert client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data=data | {"provider": "x"}).status_code == 400
    state["kek"] = None
    assert client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data=data).status_code == 503
    assert "not configured" in client.get("/tenants/arsenyspb/ollama-monitor").text
    assert store.keys == {}


def test_ciphertext_bound_to_tenant_and_provider():
    ct = llm_keys.encrypt(KEK, "t-a", "gemini", GEMINI_KEY)
    assert llm_keys.decrypt(KEK, "t-a", "gemini", ct) == GEMINI_KEY
    for tid, prov in (("t-b", "gemini"), ("t-a", "anthropic")):
        with pytest.raises(Exception):
            llm_keys.decrypt(KEK, tid, prov, ct)
    assert llm_keys.encrypt(KEK, "t-a", "gemini", GEMINI_KEY) != ct


def test_onboarding_guide_is_public_and_links_script(ui):
    client, _, _ = ui
    assert "/onboarding" in client.get("/").text
    r = client.get("/onboarding")
    assert r.status_code == 200
    assert "curl -fsSL https://cp.example/onboard.sh | bash" in r.text
    assert "Writing an evaluation spec" in r.text and "installations/new" in r.text
    assert "href='https://github.com/atobar-flow/atobar-flow'" in r.text


def test_onboard_sh_endpoint_renders_cp_url_and_app_slug(ui, monkeypatch):
    client, _, _ = ui
    r_default = client.get("/onboard.sh")
    assert r_default.status_code == 200
    assert r_default.headers["content-type"].startswith("text/x-shellscript")
    assert 'CP_URL="${ATOBAR_CP_URL:-https://cp.example.invalid}"' in r_default.text
    assert 'APP_SLUG="${ATOBAR_APP_SLUG:-atobar-flow-agent}"' in r_default.text

    monkeypatch.setenv("CP_PUBLIC_URL", "https://cp.custom.example/")
    monkeypatch.setenv("CP_GITHUB_APP_SLUG", "custom-flow-bot")
    r_custom = client.get("/onboard.sh")
    assert r_custom.status_code == 200
    assert 'CP_URL="${ATOBAR_CP_URL:-https://cp.custom.example}"' in r_custom.text
    assert 'APP_SLUG="${ATOBAR_APP_SLUG:-custom-flow-bot}"' in r_custom.text


def test_home_and_tenant_page_show_install_steps(ui):
    client, _, _ = _manager(ui)
    home = client.get("/").text
    assert "Install on your repository" in home and "atobar-flow/atobar-flow" in home
    page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "API token issued" in page and "/onboarding" in page


def test_role_model_is_set_per_role_from_validated_key_models(ui):
    client, store, _ = _manager(ui)
    csrf = _csrf(client)
    client.post("/tenants/arsenyspb/ollama-monitor/llm-keys", data={"csrf": csrf, "provider": "gemini", "api_key": GEMINI_KEY})
    page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "Model per role" in page and "Not configured" in page
    base = "/tenants/arsenyspb/ollama-monitor/llm-roles"
    ok = client.post(f"{base}/evaluator", data={"csrf": csrf, "model": "gemini|model-1", "reasoning_effort": "high"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert store.role_llm_prefs("t-a") == {
        "evaluator": {"provider": "gemini", "model": "model-1", "reasoning_effort": "high"}
    }
    assert client.post(f"{base}/evaluator", data={"csrf": csrf, "model": "gemini|other"}).status_code == 400
    assert client.post(f"{base}/arbitrator", data={"csrf": csrf, "model": "gemini|model-1"}).status_code == 400


def test_arbitration_mode_is_set_from_the_tenant_page(ui):
    client, store, _ = _manager(ui)
    page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "Arbitration mode" in page and "auto_merge" in page and "never merges" in page
    url = "/tenants/arsenyspb/ollama-monitor/arbitration-mode"
    assert client.post(url, data={"mode": "yolo", "csrf": _csrf(client)}).status_code == 400
    assert client.post(url, data={"mode": "recommend", "csrf": "forged"}).status_code == 403
    r = client.post(url, data={"mode": "recommend", "csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303
    tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
    assert tenant is not None and tenant.arbitration_mode == "recommend"


def test_unauthenticated_redirects_to_login_and_back_to_original_page(ui):
    client, _, _ = ui
    # 1. Unauthenticated GET on tenant page redirects to /login?next=...
    r1 = client.get("/tenants/arsenyspb/ollama-monitor", follow_redirects=False)
    assert r1.status_code == 303
    assert r1.headers["location"] == "/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor"

    # 2. Unauthenticated POST on /llm-keys/gemini/delete redirects to /login?next=/tenants/arsenyspb/ollama-monitor
    r_post = client.post("/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete", follow_redirects=False)
    assert r_post.status_code == 303
    assert r_post.headers["location"] == "/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor"

    # 3. Unauthenticated GET on /llm-keys/gemini/delete also redirects to /login?next=/tenants/arsenyspb/ollama-monitor
    r_get_del = client.get("/tenants/arsenyspb/ollama-monitor/llm-keys/gemini/delete", follow_redirects=False)
    assert r_get_del.status_code == 303
    assert r_get_del.headers["location"] == "/login?next=%2Ftenants%2Farsenyspb%2Follama-monitor"

    # 4. Follow /login?next=... -> /auth/callback -> redirects back to /tenants/arsenyspb/ollama-monitor
    login_resp = client.get(r_post.headers["location"], follow_redirects=False)
    assert login_resp.status_code == 302
    q = parse_qs(urlparse(login_resp.headers["location"]).query)
    cb_resp = client.get(f"/auth/callback?code=c&state={q['state'][0]}", follow_redirects=False)
    assert cb_resp.status_code == 303
    assert cb_resp.headers["location"] == "/tenants/arsenyspb/ollama-monitor"

    # 5. Open redirect attempt is sanitized to "/"
    evil_login = client.get("/login?next=//evil.example/phish", follow_redirects=False)
    q_evil = parse_qs(urlparse(evil_login.headers["location"]).query)
    cb_evil = client.get(f"/auth/callback?code=c&state={q_evil['state'][0]}", follow_redirects=False)
    assert cb_evil.status_code == 303
    assert cb_evil.headers["location"] == "/"


def test_lazy_reload_models_endpoint_and_dropdown_attributes(ui):
    client, store, state = _manager(ui)
    csrf = _csrf(client)
    client.post(
        "/tenants/arsenyspb/ollama-monitor/llm-keys",
        data={"csrf": csrf, "provider": "gemini", "api_key": GEMINI_KEY},
    )

    # Provider now adds a newly released model (e.g. gemini-4-argon)
    def validate_with_argon(key: str) -> tuple[bool, list[str]]:
        state["validated"].append(key)
        return True, ["model-1", "gemini-4-argon"]

    cp.app.dependency_overrides[web.get_providers] = lambda: {
        "gemini": Provider("gemini", "Google Gemini", "https://example", validate_with_argon)
    }

    page_resp = client.get("/tenants/arsenyspb/ollama-monitor")
    assert page_resp.status_code == 200
    assert "connect-src 'self'" in page_resp.headers["content-security-policy"]
    assert "data-models-url='/tenants/arsenyspb/ollama-monitor/llm-models'" in page_resp.text
    assert "lazyLoadModels(this)" in page_resp.text

    # Lazy-reload endpoint fetches live models from provider and updates store
    lazy_resp = client.get("/tenants/arsenyspb/ollama-monitor/llm-models")
    assert lazy_resp.status_code == 200
    payload = lazy_resp.json()
    assert {"provider": "gemini", "model": "gemini-4-argon"} in payload["models"]
    assert "gemini-4-argon" in store.keys[("t-a", "gemini")]["available_models"]

    # Now selecting gemini-4-argon for developer succeeds
    sel_resp = client.post(
        "/tenants/arsenyspb/ollama-monitor/llm-roles/developer",
        data={"csrf": csrf, "model": "gemini|gemini-4-argon", "reasoning_effort": "high"},
        follow_redirects=False,
    )
    assert sel_resp.status_code == 303
    assert store.role_llm_prefs("t-a")["developer"]["model"] == "gemini-4-argon"


def test_human_assignee_dropdown_defaults_to_owner_and_updates_collaborator(ui):
    client, store, _ = _manager(ui)

    class _FakeCollabDispatcher:
        def list_collaborators(self, repo: str) -> list[str]:
            return ["arsenyspb", "collab-alice", "collab-bob"]

        def is_valid_assignee(self, repo: str, login: str) -> bool:
            return login in {"arsenyspb", "collab-alice", "collab-bob"}

    cp.app.dependency_overrides[cp.get_dispatcher] = lambda: _FakeCollabDispatcher()
    try:
        page = client.get("/tenants/arsenyspb/ollama-monitor").text
        assert "Human Assignee &amp; Reviewer" in page
        assert "<option value='arsenyspb' selected>@arsenyspb (repo owner — default)</option>" in page
        assert "<option value='collab-alice'>@collab-alice</option>" in page
        assert "<option value='collab-bob'>@collab-bob</option>" in page
        assert "<li><span class='pill pass'>done</span> Human assignee configured (@arsenyspb)</li>" in page

        csrf = _csrf(client)
        # Non-collaborator / non-assignable org is rejected with 422
        r_bad = client.post(
            "/tenants/arsenyspb/ollama-monitor/assignee",
            data={"csrf": csrf, "owner_github_login": "random-stranger"},
            follow_redirects=False,
        )
        assert r_bad.status_code == 422

        # Valid collaborator updates manifest["owner_github_login"]
        r_ok = client.post(
            "/tenants/arsenyspb/ollama-monitor/assignee",
            data={"csrf": csrf, "owner_github_login": "collab-alice"},
            follow_redirects=False,
        )
        assert r_ok.status_code == 303
        tenant = store.tenant_by_repo("arsenyspb/ollama-monitor")
        assert tenant is not None
        assert tenant.manifest.get("owner_github_login") == "collab-alice"

        page_after = client.get("/tenants/arsenyspb/ollama-monitor").text
        assert "<option value='collab-alice' selected>@collab-alice</option>" in page_after

        # If an org login that is not assignable is stored in manifest, readiness checklist flags it as "to do"
        store.tenants["t-a"] = Tenant(
            "t-a",
            "arsenyspb/ollama-monitor",
            "active",
            "shadow",
            {"repo": "arsenyspb/ollama-monitor", "owner_github_login": "acme-org"},
        )
        page_org = client.get("/tenants/arsenyspb/ollama-monitor").text
        assert "<li><span class='pill none'>to do</span> No valid human assignee (@acme-org is not assignable): you won&#x27;t be notified. Pick a collaborator below</li>" in page_org
    finally:
        cp.app.dependency_overrides.pop(cp.get_dispatcher, None)


def test_readiness_checklist_llm_models_and_launchpad_links(ui):
    client, store, _ = _manager(ui)
    home = client.get("/").text
    assert "Atobar Flow — How It Works" in home
    assert "https://github.com/issues/assigned" in home
    assert "https://github.com/pulls/review-requested" in home
    assert "https://github.com/arsenyspb/ollama-monitor/issues?q=is:open+assignee:@me" in home
    assert "https://github.com/arsenyspb/ollama-monitor/pulls?q=is:open+review-requested:@me" in home
    assert "https://github.com/arsenyspb/ollama-monitor/issues/new?template=flow-task.md" in home

    page_before = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "issues/new?template=flow-task.md&amp;labels=flow:feature" in page_before
    assert "issues/new?template=flow-task.md&amp;labels=flow:fix" in page_before
    assert "issues/new?template=flow-task.md&amp;labels=flow:plan" in page_before
    assert (
        "<li><span class='pill none'>to do</span> LLM key &amp; role models configured (planner, developer, evaluator)</li>"
        in page_before
    )

    csrf = _csrf(client)
    client.post(
        "/tenants/arsenyspb/ollama-monitor/llm-keys",
        data={"csrf": csrf, "provider": "gemini", "api_key": GEMINI_KEY},
    )
    # Setting developer + evaluator (with planner falling back to developer) satisfies LLM readiness
    for role in ("developer", "evaluator"):
        client.post(
            f"/tenants/arsenyspb/ollama-monitor/llm-roles/{role}",
            data={"csrf": csrf, "model": "gemini|model-1", "reasoning_effort": "medium"},
            follow_redirects=False,
        )

    page_after = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert (
        "<li><span class='pill pass'>done</span> LLM key &amp; role models configured (planner, developer, evaluator)</li>"
        in page_after
    )

    # If the configured provider key is deleted, LLM readiness turns red ("to do")
    store.delete_llm_key("t-a", "gemini")
    page_missing_key = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert (
        "<li><span class='pill none'>to do</span> LLM key &amp; role models configured (planner, developer, evaluator)</li>"
        in page_missing_key
    )




def test_assignee_readiness_rejects_unassignable_org_login_and_caches() -> None:
    from control_panel import web

    class _D:
        calls = 0

        def is_valid_assignee(self, repo: str, login: str) -> bool:
            _D.calls += 1
            return login == "jane"

    web._ASSIGNEE_CHECK_CACHE.clear()
    d = _D()
    assert web._assignee_is_valid(d, "acme-org/app", "acme-org") is False
    assert web._assignee_is_valid(d, "acme-org/app", "acme-org") is False
    assert _D.calls == 1, "result is cached so tenant_detail does not hit GitHub on every render"
    assert web._assignee_is_valid(d, "acme-org/app", "jane") is True
    assert web._assignee_is_valid(d, "acme-org/app", "dependabot[bot]") is False
    assert web._assignee_is_valid(None, "acme-org/app", "jane") is True


def test_j50_wiki_screen_xss_escaping_and_admin_controls(ui: tuple[TestClient, WebStore, dict[str, Any]]) -> None:
    """J50 AC-6 & AC-7: Wiki section renders safely (XSS escaped) and enforces admin permissions on actions."""
    client, store, state = ui
    store.wikis["t-a"] = {
        "repo_name": "arsenyspb/ollama-monitor",
        "purpose": "Monitor <script>alert(1)</script> models",
        "technologies": ["Python", "<img src=x onerror=alert(2)>"],
        "structure": "src/ & <svg onload=alert(3)>",
        "concepts": ["Zero-trust <b>bold</b>"],
        "architecture_mermaid": 'flowchart TD\n  A["<script>bad</script>"] --> B[Store]',
        "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
        "refresh_status": "idle",
        "merges_since_refresh": 2,
        "refresh_interval_merges": 5,
        "last_updated": "2026-10-25T08:00:00Z",
    }

    # 1. Reader can view the wiki and Mermaid diagram, with all untrusted strings HTML-escaped
    _sign_in(client)
    resp = client.get("/tenants/arsenyspb/ollama-monitor")
    assert resp.status_code == 200
    html = resp.text
    assert "Repository Wiki (SotaWiki)" in html
    assert "abcdef1" in html
    assert "2 / 5 merges since refresh" in html
    assert "<pre class='mermaid'><code>" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=alert(2)>" not in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html
    assert "<svg onload=alert(3)>" not in html
    assert "Force Regenerate Wiki" not in html  # reader does not see admin buttons

    # 2. Reader POST to /wiki/regenerate or /wiki/policy is rejected with 403
    csrf_reader = _csrf(client)
    r_forbid_regen = client.post(
        "/tenants/arsenyspb/ollama-monitor/wiki/regenerate",
        data={"csrf": csrf_reader},
        follow_redirects=False,
    )
    assert r_forbid_regen.status_code == 403
    r_forbid_pol = client.post(
        "/tenants/arsenyspb/ollama-monitor/wiki/policy",
        data={"csrf": csrf_reader, "refresh_interval_merges": "3"},
        follow_redirects=False,
    )
    assert r_forbid_pol.status_code == 403

    # 3. Repo admin sees controls, can update refresh_interval_merges, and can trigger Force Regenerate
    state["gh"] = FakeGitHub("alice", {"arsenyspb/ollama-monitor"}, manage=frozenset({"arsenyspb/ollama-monitor"}))
    _sign_in(client)
    admin_page = client.get("/tenants/arsenyspb/ollama-monitor").text
    assert "Force Regenerate Wiki" in admin_page
    assert "Save wiki cadence" in admin_page

    csrf_admin = _csrf(client)
    r_pol = client.post(
        "/tenants/arsenyspb/ollama-monitor/wiki/policy",
        data={"csrf": csrf_admin, "refresh_interval_merges": "10"},
        follow_redirects=False,
    )
    assert r_pol.status_code == 303
    assert store.wiki_for_tenant("t-a")["refresh_interval_merges"] == 10

    r_regen = client.post(
        "/tenants/arsenyspb/ollama-monitor/wiki/regenerate",
        data={"csrf": csrf_admin},
        follow_redirects=False,
    )
    assert r_regen.status_code == 303
    assert store.wiki_for_tenant("t-a")["refresh_status"] == "running"

    # Second click while running queues the refresh
    r_regen2 = client.post(
        "/tenants/arsenyspb/ollama-monitor/wiki/regenerate",
        data={"csrf": csrf_admin},
        follow_redirects=False,
    )
    assert r_regen2.status_code == 303
    assert store.wiki_for_tenant("t-a")["refresh_status"] == "queued"
    assert store.wiki_for_tenant("t-a")["refresh_queued"] is True

    # GET on action URLs redirects cleanly to the tenant page
    r_get_redir = client.get("/tenants/arsenyspb/ollama-monitor/wiki/regenerate", follow_redirects=False)
    assert r_get_redir.status_code == 303
