"""Browser UI. Sign in with GitHub; a user sees only tenants whose repo they can read on GitHub.

Repo admins (GitHub `admin` permission, via the atobar-flow App's installations) can register
their repo as a tenant, rotate its API token, manage its LLM keys, configure loop/budget guardrails,
and author/compile/approve/retire repository metrics.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from atobar_contracts.conductor_io import resolve_human_assignee
from atobar_contracts.goal import GoalBudget, GoalStallPolicy
from atobar_contracts.manifest import METRIC_KEY_RE
from atobar_contracts.api import (
    ArbitrationModeUpdateRequest,
    AssigneeUpdateRequest,
    CreateTenantRequest,
    GoalUpdateRequest,
    GuardrailsUpdateRequest,
    MetricApproveRequest,
    MetricCompileRequest,
    MetricDraftRequest,
    MetricRetireRequest,
    WikiPolicyUpdateRequest,
    WikiRegenerateRequest,
)
from control_panel import llm_keys
from control_panel.client_templates import (
    INIT_REPO,
    ONBOARD_COMMAND,
    ONBOARD_SCRIPT_URL,
    onboard_command,
    onboard_script_url,
    resolve_cp_url,
)
from control_panel.flow_engine_client import FlowEngineClient, get_flow_engine_client
from control_panel.github import _CP_COLLAB_CACHE, Dispatcher, GitHubAppDispatcher, get_dispatcher
from control_panel.llm_keys import PROVIDERS, Provider
from control_panel.store import Store, Tenant, get_store
from control_panel.tokens import hash_token, mint_token

logger = logging.getLogger(__name__)

SESSION_COOKIE = "cp_session"
STATE_COOKIE = "cp_oauth_state"
NEXT_COOKIE = "cp_oauth_next"
SESSION_TTL_S = 8 * 3600
GITHUB = "https://github.com"
API = "https://api.github.com"
MAX_SESSION_REPOS = 60

router = APIRouter(include_in_schema=False)


def _req_id(request: Request) -> str:
    val = getattr(request.state, "request_id", None) or (request.headers.get("X-Request-ID") or "").strip()
    return str(val) if val else str(uuid.uuid4())


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    session_secret: str
    admin_logins: frozenset[str]
    public_url: str
    factory_repo: str
    app_slug: str = "atobar-flow-agent"


def get_oauth_config() -> Optional[OAuthConfig]:
    cid = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
    secret = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
    session_secret = os.environ.get("CP_SESSION_SECRET", "")
    if not (cid and secret and len(session_secret) >= 32):
        return None
    admins = frozenset(x.strip().lower() for x in os.environ.get("CP_ADMIN_LOGINS", "").split(",") if x.strip())
    return OAuthConfig(
        client_id=cid,
        client_secret=secret,
        session_secret=session_secret,
        admin_logins=admins,
        public_url=resolve_cp_url(),
        factory_repo=os.environ.get("FLOW_FACTORY_REPO", "atobar-flow/atobar-flow"),
        app_slug=os.environ.get("CP_GITHUB_APP_SLUG", "atobar-flow-agent"),
    )


class GitHubUser(Protocol):
    def exchange_code(self, cfg: OAuthConfig, code: str) -> str: ...
    def login(self, token: str) -> str: ...
    def repo_permissions(self, token: str) -> dict[str, dict[str, bool]]: ...


class HttpGitHubUser:
    def exchange_code(self, cfg: OAuthConfig, code: str) -> str:
        resp = httpx.post(
            f"{GITHUB}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": cfg.client_id, "client_secret": cfg.client_secret, "code": code},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "GitHub rejected the sign-in code")
        return str(token)

    def _get(self, token: str, path: str) -> httpx.Response:
        return httpx.get(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )

    def login(self, token: str) -> str:
        resp = self._get(token, "/user")
        resp.raise_for_status()
        return str(resp.json()["login"])

    def repo_permissions(self, token: str) -> dict[str, dict[str, bool]]:
        """Repos the user can access through installations of this App, with their permissions."""
        perms: dict[str, dict[str, bool]] = {}
        resp = self._get(token, "/user/installations?per_page=100")
        resp.raise_for_status()
        for inst in resp.json().get("installations", []):
            for page in range(1, 6):
                r = self._get(token, f"/user/installations/{inst['id']}/repositories?per_page=100&page={page}")
                r.raise_for_status()
                repos = r.json().get("repositories", [])
                for repo in repos:
                    perms[repo["full_name"]] = dict(repo.get("permissions") or {})
                if len(repos) < 100:
                    break
        return perms


def get_github_user() -> GitHubUser:
    return HttpGitHubUser()


def get_kek() -> Optional[bytes]:
    return llm_keys.get_kek()


def get_kms_client() -> Optional[Any]:
    return llm_keys.get_kms_client()


def get_providers() -> dict[str, Provider]:
    return PROVIDERS


def _secret(cfg: Optional[OAuthConfig] = None) -> bytes:
    raw = cfg.session_secret if (cfg is not None and cfg.session_secret) else os.environ.get("CP_SESSION_SECRET", "")
    if len(raw) < 32:
        raise RuntimeError("CP_SESSION_SECRET must be at least 32 characters")
    return raw.encode("utf-8")


@dataclass(frozen=True)
class Session:
    login: str
    repos: frozenset[str]
    admin: bool
    manage: frozenset[str] = frozenset()
    csrf: str = ""
    iat: int = 0

    def can_see(self, repo: str) -> bool:
        return self.admin or repo in self.repos or repo in self.manage

    def can_manage(self, repo: str) -> bool:
        return self.admin or repo in self.manage


def csrf_token(session: Session, secret: Optional[bytes] = None) -> str:
    if session.csrf:
        return session.csrf
    key = secret if secret is not None else _secret()
    return hmac.new(key, f"csrf:{session.login}:{session.iat}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session: Session, submitted_token: str) -> None:
    try:
        expected = csrf_token(session)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "session signing not configured") from exc
    if not (expected and submitted_token and hmac.compare_digest(expected, submitted_token)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "form expired, reload the page")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(secret: str, payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(secret: str, value: str) -> Optional[dict[str, Any]]:
    body, _, mac = value.partition(".")
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not mac or not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def current_session(request: Request, cfg: Optional[OAuthConfig] = Depends(get_oauth_config)) -> Optional[Session]:
    raw = request.cookies.get(SESSION_COOKIE)
    payload = unsign(cfg.session_secret, raw) if cfg and raw else None
    if payload is None:
        return None
    return Session(
        login=payload["login"],
        repos=frozenset(payload["repos"]),
        admin=bool(payload["admin"]),
        manage=frozenset(payload.get("manage", [])),
        csrf=str(payload.get("csrf", "")),
        iat=int(payload.get("iat", 0)),
    )


require_user = current_session


def passes(value: float, comparator: str, threshold: float) -> bool:
    """Display only; the arbitrator has its own deterministic evaluator."""
    ops = {
        ">=": value >= threshold,
        "<=": value <= threshold,
        ">": value > threshold,
        "<": value < threshold,
        "==": math.isclose(value, threshold),
    }
    return ops.get(comparator, False)


CSS = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
header{background:#24292f;color:#fff;padding:12px 24px;display:flex;justify-content:space-between;align-items:center}
header a,header button{color:#fff;text-decoration:none;background:none;border:0;font:inherit;cursor:pointer}
main{max-width:1100px;margin:24px auto;padding:0 24px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de;border-radius:6px;margin-bottom:24px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #d0d7de;vertical-align:top}
th{background:#f6f8fa;font-weight:600}
.pill{display:inline-block;padding:0 8px;border-radius:12px;font-size:13px;font-weight:600}
.pass{background:#dafbe1;color:#116329}.fail{background:#ffebe9;color:#a40e26}.none{background:#eaeef2;color:#57606a}
.btn{display:inline-block;background:#1f883d;color:#fff;padding:8px 16px;border-radius:6px;
text-decoration:none;font-weight:600}
.card{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:16px 20px;margin-bottom:24px}
code{background:#eaeef2;padding:1px 4px;border-radius:4px}
.secret{display:block;padding:12px;font-size:14px;word-break:break-all;user-select:all}
.warn{background:#fff8c5;border-color:#d4a72c}
input,select,textarea{font:inherit;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px}
button.btn,button.danger{border:0;cursor:pointer;font:inherit}
button.btn-outline-danger{cursor:pointer;font:inherit}
.danger{background:#cf222e;color:#fff;padding:6px 12px;border-radius:6px;font-weight:600}
.btn-outline-danger{display:inline-block;background:#fff !important;color:#cf222e !important;
border:1px solid #cf222e !important;padding:7px 15px;border-radius:6px;font-weight:600}
form.inline{display:inline;margin:0}
"""


def _page(title: str, body: str, session: Optional[Session] = None) -> HTMLResponse:
    who = (
        f'<form method="post" action="/logout" style="margin:0">{_csrf_field(session)}{escape(session.login)} · '
        "<button>Sign out</button></form>"
        if session
        else ""
    )
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)} · atobar-flow</title>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}</style></head>"
        f"<body><header><a href='/'><b>atobar-flow</b> Control Panel</a>{who}</header><main>{body}</main></body></html>"
    )
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self' https://github.com"
            ),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Cache-Control": "no-store",
        },
    )


def _pill(ok: Optional[bool]) -> str:
    if ok is None:
        return "<span class='pill none'>no data</span>"
    return "<span class='pill pass'>pass</span>" if ok else "<span class='pill fail'>fail</span>"


def _latest(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for o in observations:
        latest.setdefault(o["key"], o)
    return latest


def _visible(session: Session, tenants: list[Tenant]) -> list[Tenant]:
    return [t for t in tenants if session.can_see(t.repo)]


def _csrf_field(session: Session) -> str:
    tok = escape(csrf_token(session))
    return f'<input type="hidden" name="csrf_token" value="{tok}"><input type="hidden" name="csrf" value="{tok}">'


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or urlparse(origin).netloc == request.headers.get("host", "")


def _safe_next(target: Optional[str], fallback: str = "/") -> str:
    """Ensure redirect targets are local relative paths (prevent open redirects)."""
    if not target or not isinstance(target, str):
        return fallback
    cleaned = target.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme or parsed.netloc or not cleaned.startswith("/") or cleaned.startswith("//"):
        return fallback
    return cleaned


def _login_redirect(next_path: str = "/") -> RedirectResponse:
    safe_target = _safe_next(next_path, "/")
    url = "/login?" + urlencode({"next": safe_target}) if safe_target != "/" else "/login"
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


async def _form(request: Request, session: Session) -> dict[str, str]:
    """Parse a urlencoded POST and enforce sign-in, same-origin and the session CSRF token."""
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request")
    raw = (await request.body()).decode()
    form = {k: v[0] for k, v in parse_qs(raw, max_num_fields=25, keep_blank_values=True).items()}
    submitted = form.get("csrf_token") or form.get("csrf") or ""
    verify_csrf(session, submitted)
    return form


def _managed_tenant(store: Store, session: Session, owner: str, repo: str) -> Tenant:
    full = f"{owner}/{repo}"
    tenant = store.tenant_by_repo(full)
    if tenant is None or not session.can_see(full):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not session.can_manage(full):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only repository admins can change this")
    return tenant


def _cookie_kwargs() -> dict[str, Any]:
    return {"httponly": True, "secure": True, "samesite": "lax", "path": "/"}


@router.get("/", response_class=HTMLResponse)
def home(
    session: Optional[Session] = Depends(current_session),
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    if session is None:
        note = (
            "<p><a class='btn' href='/login'>Sign in with GitHub</a></p>"
            if cfg
            else "<p>GitHub sign-in is not configured on this server yet.</p>"
        )
        return _page(
            "Sign in",
            "<div class='card'><h2>atobar-flow Control Panel</h2>"
            "<p>See the business metrics, arbitration mode and loop state of every repository "
            "you can access on GitHub.</p>"
            f"{note}<p>New here? Read the <a href='/onboarding'>onboarding guide</a>.</p></div>",
        )
    tenants = store.list_tenants()
    rows = []
    for t in _visible(session, tenants):
        defs = [
            d
            for d in store.metric_definitions(t.id)
            if d.get("status", "active") == "active" and d.get("comparator") and d.get("threshold") is not None
        ]
        latest = _latest(store.recent_observations(t.id))
        results = [
            passes(float(latest[d["key"]]["value"]), str(d["comparator"]), float(d["threshold"]))
            for d in defs
            if d["key"] in latest
        ]
        summary = f"{sum(results)}/{len(defs)} passing" if defs else "no metrics defined"
        if t.status != "active":
            blockers = []
            if not _is_llm_ready(store, t.id):
                blockers.append("LLM setup needed")
            if not defs:
                blockers.append("metric needed")
            elif not results:
                blockers.append("calibrating on main")
            if blockers:
                summary += f" ({', '.join(blockers)})"
        rows.append(
            f"<tr><td><a href='/tenants/{quote(t.repo)}'>{escape(t.repo)}</a><br>"
            f"<small>{_launchpad_links(t.repo, active=(t.status == 'active'), compact=True)}</small></td>"
            f"<td>{escape(t.status)}</td>"
            f"<td><code>{escape(t.arbitration_mode)}</code></td><td>{escape(summary)}</td></tr>"
        )
    table = (
        "<table><tr><th>Repository</th><th>Status</th><th>Arbitration</th><th>Metrics</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else "<div class='card'>No onboarded repositories that you can access yet.</div>"
    )
    registered = {t.repo for t in tenants}
    addable = sorted(r for r in session.manage if r not in registered)
    app = cfg.app_slug if cfg else "atobar-flow-agent"
    install = f"https://github.com/apps/{quote(app)}/installations/new"
    add_rows = "".join(
        f"<tr><td>{escape(r)}</td><td><form class='inline' method='post' action='/tenants'>{_csrf_field(session)}"
        f"<input type='hidden' name='repo' value='{escape(r)}'><button class='btn'>Add to atobar-flow</button>"
        "</form></td></tr>"
        for r in addable
    )
    add = (
        "<h2>Add a repository</h2>"
        + (
            "<table><tr><th>Repository (you are admin)</th><th></th></tr>" + add_rows + "</table>"
            if add_rows
            else "<div class='card'>No repositories to add.</div>"
        )
        + f"<p>Missing a repository? <a href='{escape(install)}'>Install the {escape(app)} GitHub App</a> on it, "
        "then sign out and back in. Only repository admins can add a repository.</p>"
        + _install_card()
    )
    return _page("Tenants", f"{_orientation_card()}<h2>Your repositories</h2>{table}{add}", session)


def _orientation_card() -> str:
    return (
        "<div class='card' id='orientation-card'>"
        "<b>Atobar Flow — How It Works</b>"
        "<p><b>1. Connect repository &amp; LLM key</b> &rarr; "
        "<b>2. Calibrate at least 1 metric</b> &rarr; "
        "<b>3. Delegate in GitHub Issues</b></p>"
        "<p><small>Apply <code>flow:feature</code>, <code>flow:fix</code>, or <code>flow:plan</code> on any GitHub "
        "issue to start. When Atobar needs your input (<code>flow:needs-approval</code>, <code>flow:escalated</code>, "
        "or a PR ready to merge), it assigns the issue or requests a PR review from your configured Human Assignee — "
        "<a href='https://github.com/issues/assigned'>🔔 Assigned to Me ↗</a> · "
        "<a href='https://github.com/pulls/review-requested'>👀 PRs to Merge ↗</a>. "
        "Close an issue at any time to cancel and stop compute.</small></p>"
        "</div>"
    )


def _launchpad_links(repo_full: str, *, active: bool = True, compact: bool = False) -> str:
    gh_base = f"https://github.com/{quote(repo_full)}"
    needs_me_url = f"{gh_base}/issues?q=is:open+assignee:@me+label:flow:needs-approval,flow:escalated"
    prs_url = f"{gh_base}/pulls?q=is:open+review-requested:@me"
    new_task_url = f"{gh_base}/issues/new?template=flow-task.md"
    feat_url = f"{gh_base}/issues/new?template=flow-task.md&labels=flow:feature"
    fix_url = f"{gh_base}/issues/new?template=flow-task.md&labels=flow:fix"
    plan_url = f"{gh_base}/issues/new?template=flow-task.md&labels=flow:plan"
    if compact:
        third_link = (
            f"<a href='{escape(new_task_url)}'>➕ New Task ↗</a>"
            if active
            else f"<a href='/tenants/{quote(repo_full)}'>⏳ Complete Setup</a>"
        )
        return (
            f"<a href='{escape(needs_me_url)}'>🔔 Needs Me ↗</a> · "
            f"<a href='{escape(prs_url)}'>👀 PRs to Merge ↗</a> · "
            f"{third_link}"
        )
    if not active:
        return (
            f"<a href='{escape(needs_me_url)}'>🔔 Needs Me ↗</a> · "
            f"<a href='{escape(prs_url)}'>👀 PRs to Merge ↗</a> · "
            "<span>⏳ Complete readiness checklist below to unlock New Task links</span>"
        )
    return (
        f"<a href='{escape(needs_me_url)}'>🔔 Needs Me ↗</a> · "
        f"<a href='{escape(prs_url)}'>👀 PRs to Merge ↗</a> · "
        f"<a href='{escape(feat_url)}'>➕ New Feature</a> · "
        f"<a href='{escape(fix_url)}'>➕ Fix Bug</a> · "
        f"<a href='{escape(plan_url)}'>➕ Plan First</a>"
    )


def _install_card(cp_url: Optional[str] = None) -> str:
    return (
        "<div class='card'><b>Install on your repository</b><ol>"
        "<li>Add the repository above and generate its token on the repository page.</li>"
        "<li>In a clone of the repository, run:<pre>" + escape(onboard_command(cp_url)) + "</pre>"
        "It stores the token as the <code>ATOBAR_TENANT_TOKEN</code> Actions secret, opens a pull request "
        "with the trigger workflow and opens your evaluation spec issue.</li></ol>"
        f"<a href='/onboarding'>Full onboarding guide</a> · "
        f"<a href='https://github.com/{INIT_REPO}'>script source ({INIT_REPO})</a></div>"
    )


ONBOARDING_GUIDE = """
<h2>Onboard a repository to atobar-flow</h2>
<p>atobar-flow runs everything remotely: agents, review rounds, arbitration and metric evaluation. Your laptop is
only used once, to connect the repository. A repository becomes a <b>tenant</b> with its own wiki, metric
definitions, observations and loop state, isolated from every other tenant.</p>

<h3>Before you start</h3>
<ul>
<li>You are an <b>admin</b> of the repository on GitHub.</li>
<li><code>git</code>, <code>curl</code> and the <a href="https://cli.github.com">GitHub CLI</a> are installed, and
<code>gh auth status</code> shows you signed in with the <code>repo</code> and <code>workflow</code> scopes
(<code>gh auth refresh -s repo,workflow</code>).</li>
<li>You have a local clone of the repository, up to date with its default branch.</li>
<li>The repository may be <b>empty</b>: the script then pushes a first commit (README and trigger workflow) instead
of opening a pull request, and the spec issue asks what to build from zero.</li>
</ul>

<h3>Part A: One-Time Repository Setup (~5 min)</h3>
<table>
<tr><th>#</th><th>Stage</th><th>Who</th><th>Done when</th></tr>
<tr><td>1</td><td><b>Install the GitHub App</b> on the repository (<a href="{install}">install</a>,
choose <i>Only select repositories</i>).</td><td>You</td><td>The repository is listed under
<i>Add a repository</i> after you sign out and back in.</td></tr>
<tr><td>2</td><td><b>Register the tenant &amp; issue token</b>: click <i>Add to atobar-flow</i>, then
<i>Generate token</i> on the repository page. The token is shown once, and canonical <code>flow:*</code> labels
are seeded automatically.</td><td>You</td><td>Status <code>onboarding</code>, arbitration
<code>shadow</code>.</td></tr>
<tr><td>3</td><td><b>Configure LLM key, role models &amp; Human Assignee</b> on the repository page
(<i>LLM keys</i>, <i>Model per role</i>, and <i>Human Assignee &amp; Reviewer</i>).</td><td>You</td>
<td>LLM readiness and Human Assignee rows are green.</td></tr>
<tr><td>4</td><td><b>Run the onboarding script &amp; merge the trigger PR</b> in the clone:<pre>{command}</pre>
It checks your access, saves the token as the <code>ATOBAR_TENANT_TOKEN</code> Actions secret (it never prints
it), opens a pull request adding <code>.github/workflows/flow-trigger.yml</code>, and opens an
<b>evaluation spec</b> issue labelled <code>flow:onboard</code>. Prefer to read it first?
<code>curl -fsSLO {script}</code>, inspect, then <code>bash onboard.sh</code>.
Source: <a href='{source}'>{source_name}</a>.</td><td>You</td>
<td>Trigger workflow is on the default branch.</td></tr>
</table>

<h3>Part B: Metric Calibration &amp; Go-Live (First CI Run on <code>main</code>)</h3>
<table>
<tr><th>#</th><th>Stage</th><th>Who</th><th>Done when</th></tr>
<tr><td>5</td><td><b>Author &amp; approve at least 1 metric</b> in the Control Panel under
<i>Repository Metrics</i> (Draft &rarr; Compile &rarr; Approve).</td><td>You</td>
<td>Metric shows on the repository page and opens a <code>flow-metrics.yml</code> PR.</td></tr>
<tr><td>6</td><td><b>Merge the <code>flow-metrics.yml</code> PR.</b> Default-branch CI runs your metric probe and
reports observations.</td><td>atobar-flow, then you</td>
<td>"Metrics received in the last 24 hours" is done.</td></tr>
<tr><td>7</td><td><b>Active.</b> Once every active metric satisfies <code>min_samples</code> on the default branch,
the tenant activates automatically. Applying <code>flow:feature</code>, <code>flow:fix</code>, or
<code>flow:plan</code> on any issue starts an agent run.</td><td>atobar-flow</td><td>Status <code>active</code>.</td></tr>
<tr><td>8</td><td><b>Graduate arbitration</b>: <code>shadow</code> &rarr; <code>recommend</code> (posts a verdict and
requests a review from your Human Assignee) &rarr; <code>auto_merge</code> (merges when every gate passes).</td><td>You</td>
<td>atobar-flow handles the repository end to end.</td></tr>
</table>
<p>While a tenant is still in <code>onboarding</code>, applying a work label removes the label and posts a single
checklist comment so you can simply re-apply it once active.</p>

<h3>Writing an evaluation spec</h3>
<p>The spec says what "better" means for your business, in numbers. For each metric give:</p>
<table>
<tr><th>Field</th><th>Meaning</th></tr>
<tr><td><code>key</code></td><td>snake_case name, e.g. <code>proxyless_access_ratio</code>.</td></tr>
<tr><td>Measurement</td><td>A command run in your CI whose <b>last line of output is a number</b>, e.g.
<code>python .atobar/metrics/proxyless.py</code>. Describe it; atobar-flow writes the probe.</td></tr>
<tr><td><code>comparator</code>, <code>threshold</code></td><td>One of <code>&gt;= &lt;= &gt; &lt; ==</code> and the
target, e.g. <code>&gt;= 1.0</code>.</td></tr>
<tr><td><code>baseline_mode</code></td><td><code>absolute</code> (must meet the threshold) or
<code>no_regression</code> (must not get worse than the default branch).</td></tr>
<tr><td><code>min_samples</code></td><td>Observations needed before the gate trusts the metric (default 1).</td></tr>
<tr><td>Why</td><td>The business outcome it stands for, and how it could be gamed.</td></tr>
</table>
<p>Good metrics measure outcomes a user would notice (for example "the monitor reads Ollama without a proxy
in front of it"), not implementation details ("uses library X"). Prefer one or two strong metrics. Changes to
<code>.github/workflows/flow-metrics.yml</code> or <code>.atobar/metrics/</code> in an agent PR are routed to a
human, and their metrics are not trusted for that PR.</p>
<p>Author or update a metric any time from the <i>Repository Metrics</i> section on the repository page.</p>
"""


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding(
    session: Optional[Session] = Depends(current_session),
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
) -> HTMLResponse:
    app = cfg.app_slug if cfg else "atobar-flow-agent"
    cp_url = cfg.public_url if cfg else resolve_cp_url()
    body = _orientation_card() + ONBOARDING_GUIDE.format(
        install=escape(f"https://github.com/apps/{quote(app)}/installations/new"),
        command=escape(onboard_command(cp_url)),
        script=escape(onboard_script_url(cp_url)),
        source=escape(f"https://github.com/{INIT_REPO}"),
        source_name=escape(INIT_REPO),
    )
    return _page("Onboarding", body, session)


@router.get("/tenants/{owner}/{repo}", response_class=HTMLResponse)
def tenant_detail(
    owner: str,
    repo: str,
    session: Optional[Session] = Depends(current_session),
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
    store: Store = Depends(get_store),
    kek: Optional[bytes] = Depends(get_kek),
    providers: dict[str, Provider] = Depends(get_providers),
    dispatcher: Optional[Dispatcher] = Depends(get_dispatcher),
) -> HTMLResponse:
    full = f"{owner}/{repo}"
    if session is None:
        return _login_redirect(f"/tenants/{quote(full)}")  # type: ignore[return-value]
    tenant = store.tenant_by_repo(full)
    if tenant is None or not session.can_see(full):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    defs = store.metric_definitions(tenant.id)
    obs = store.recent_observations(tenant.id)
    latest = _latest(obs)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    fresh = any(
        datetime.fromisoformat(str(o["observed_at"]).replace("Z", "+00:00")) >= since
        for o in obs
        if o.get("observed_at")
    )

    can_manage = session.can_manage(full)
    metric_rows = []
    for d in defs:
        key = str(d.get("key") or "")
        status_val = str(d.get("status") or "active")
        comp = d.get("comparator")
        thr = d.get("threshold")
        last = latest.get(key)
        dry_run = d.get("dry_run") if isinstance(d.get("dry_run"), dict) else None
        digest_str = str(d.get("digest") or "")
        raw_comp = d.get("compiled")
        compiled_spec: dict[str, Any] = dict(raw_comp) if isinstance(raw_comp, dict) else {}
        raw_src = d.get("source")
        src_spec: dict[str, Any] = dict(raw_src) if isinstance(raw_src, dict) else {}
        run_cmd = str(compiled_spec.get("run") or src_spec.get("run") or d.get("run") or "")

        if last and comp and thr is not None:
            ok: Optional[bool] = passes(float(last["value"]), str(comp), float(thr))
        elif dry_run and dry_run.get("value") is not None and comp and thr is not None:
            ok = passes(float(dry_run["value"]), str(comp), float(thr))
        else:
            ok = None
        target_str = f"{escape(str(comp))} {float(thr):g}" if (comp and thr is not None) else f"— ({escape(status_val)})"

        if last:
            value = f"{float(last['value']):g} @ <code>{escape(str(last.get('commit_sha', ''))[:7])}</code>"
        elif dry_run and dry_run.get("value") is not None:
            value = (
                f"dry-run: {float(dry_run['value']):g} @ "
                f"<code>{escape(str(dry_run.get('sha', ''))[:7])}</code>"
            )
        else:
            value = "—"

        details_parts = []
        if d.get("intent"):
            details_parts.append(f"<br><small>{escape(str(d['intent']))}</small>")
        if run_cmd:
            details_parts.append(f"<br><small>cmd: <code>{escape(run_cmd)}</code></small>")
        if digest_str:
            details_parts.append(f"<br><small>digest: <code>{escape(digest_str)}</code></small>")
        if status_val in ("draft", "compiled"):
            comp_sess = store.latest_metric_compiler_session(tenant.id, key)
            comp_status = str((comp_sess or {}).get("status") or "")
            if comp_status.lower().startswith("halted"):
                reason = str((comp_sess or {}).get("halted_reason") or comp_status)
                details_parts.append(f"<br><small class='pill fail'>last compile failed: {escape(reason[:400])}</small>")
            elif comp_status.lower() in ("running", "pending", "active"):
                details_parts.append("<br><small>compiling on a sandbox…</small>")

        actions_html = ""
        if can_manage:
            base_m = f"/tenants/{quote(full)}/metrics/{quote(key)}"
            btn_parts = []
            if status_val in ("draft", "compiled"):
                btn_parts.append(
                    f"<form class='inline' method='post' action='{base_m}/compile'>"
                    f"{_csrf_field(session)}<button class='btn'>Compile</button></form>"
                )
            if status_val == "compiled" and digest_str:
                btn_parts.append(
                    f"<form class='inline' method='post' action='{base_m}/approve'>"
                    f"{_csrf_field(session)}"
                    f"<input type='hidden' name='digest' value='{escape(digest_str)}'>"
                    f"<button class='btn'>Approve</button></form>"
                )
            if status_val in ("active", "compiled"):
                btn_parts.append(
                    f"<form class='inline' method='post' action='{base_m}/retire'>"
                    f"{_csrf_field(session)}<button class='danger'>Retire</button></form>"
                )
            actions_html = " ".join(btn_parts)

        metric_rows.append(
            f'<tr id="metric-{escape(key)}">'
            f"<td><code>{escape(key)}</code> <span class='pill none'>{escape(status_val)}</span>"
            f"{''.join(details_parts)}</td>"
            f"<td>{target_str}</td>"
            f"<td>{escape(str(d.get('baseline_mode') or 'absolute'))}</td>"
            f"<td>{value}</td>"
            f"<td>{_pill(ok)} {actions_html}</td></tr>"
        )
    metrics = (
        "<table><tr><th>Metric</th><th>Target</th><th>Mode</th><th>Latest</th><th>Result</th></tr>"
        + ("".join(metric_rows) or "<tr><td colspan=5>No metrics defined yet.</td></tr>")
        + "</table>"
    )
    hist_rows = "".join(
        f"<tr><td>{escape(str(o.get('observed_at', ''))[:19].replace('T', ' '))}</td>"
        f"<td><code>{escape(str(o.get('key', '')))}</code></td>"
        f"<td>{float(o.get('value', 0.0)):g}</td>"
        f"<td><code>{escape(str(o.get('commit_sha', ''))[:7])}</code></td>"
        f"<td>{'#' + str(o['pr_number']) if o.get('pr_number') else 'default branch'}</td></tr>"
        for o in obs[:20]
    )
    history = (
        "<table><tr><th>Observed (UTC)</th><th>Metric</th><th>Value</th><th>Commit</th><th>PR</th></tr>"
        + (hist_rows or "<tr><td colspan=5>No observations yet.</td></tr>")
        + "</table>"
    )
    llm_ready = _is_llm_ready(store, tenant.id)
    current_assignee = _effective_assignee_login(tenant)
    assignee_ok = _assignee_is_valid(dispatcher, tenant.repo, current_assignee)
    checks = [
        ("Tenant registered", True),
        ("API token issued (save it as ATOBAR_TENANT_TOKEN)", bool(store.active_tokens(tenant.id))),
        ("LLM key & role models configured (planner, developer, evaluator)", llm_ready),
        (
            f"Human assignee configured (@{current_assignee})"
            if assignee_ok
            else f"No valid human assignee (@{current_assignee} is not assignable): you won't be notified. "
            "Pick a collaborator below",
            assignee_ok,
        ),
        ("Evaluation spec approved: metrics defined", bool(defs)),
        ("Metrics received in the last 24 hours", fresh),
        ("Tenant active", tenant.status == "active"),
    ]
    health = "".join(
        f"<li><span class='pill {'pass' if ok else 'none'}'>{'done' if ok else 'to do'}</span> {escape(label)}</li>"
        for label, ok in checks
    )
    factory = cfg.factory_repo if cfg else os.environ.get("FLOW_FACTORY_REPO", "atobar-flow/atobar-flow")
    propose = f"https://github.com/{factory}/issues/new?" + urlencode(
        {
            "title": f"flow:metrics change for {full}",
            "labels": "flow:metrics",
            "body": f"Tenant: {full}\n\nProposed change (metric key, comparator, threshold, why):\n",
        }
    )

    loop_rounds = store.loop_rounds_for_tenant(tenant.id) if hasattr(store, "loop_rounds_for_tenant") else []
    pr_groups: dict[int, list[dict[str, Any]]] = {}
    for r in loop_rounds:
        pr_num = r.get("pr_number")
        if pr_num is not None:
            pr_groups.setdefault(pr_num, []).append(r)

    loop_policy = tenant.manifest.get("loop", {}) if isinstance(tenant.manifest, dict) else {}
    max_rounds = loop_policy.get("max_review_rounds", 4)
    max_cost = loop_policy.get("max_pr_cost_usd", loop_policy.get("max_cost_usd_per_pr", 25.0))

    loop_rows = []
    for pr_num, rlist in sorted(pr_groups.items(), reverse=True):
        pr_rounds = max(r.get("round", 0) for r in rlist)
        total_pr_cost = sum(r.get("cost_usd", 0.0) for r in rlist)
        latest_role = rlist[-1].get("role", "—")
        loop_rows.append(
            f"<tr><td><a href='/tenants/{quote(full)}/prs/{pr_num}'>PR #{pr_num}</a></td>"
            f"<td>{pr_rounds} / {max_rounds} max</td>"
            f"<td>${total_pr_cost:.2f} / ${max_cost:.2f} max</td>"
            f"<td><code>{escape(latest_role)}</code></td></tr>"
        )
    loop_history_html = (
        "<table><tr><th>Pull Request</th><th>Rounds / Cap</th><th>Spend / Cap</th><th>Latest Role</th></tr>"
        + ("".join(loop_rows) or "<tr><td colspan=4>No loop rounds recorded yet.</td></tr>")
        + "</table>"
    )

    goal_status_html = _goal_status_section(store, tenant, defs, obs)
    wiki_html = _wiki_section(store, session, tenant, can_manage)
    body = (
        f"<h2>{escape(full)}</h2>"
        f"<div class='card'>Status <b>{escape(tenant.status)}</b>"
        f" · arbitration <code>{escape(tenant.arbitration_mode)}</code>"
        f" · <a href='https://github.com/{quote(full)}'>repository</a>"
        f"<p>{_launchpad_links(full, active=(tenant.status == 'active'))}</p>"
        f"<ul>{health}</ul>"
        f"<a href='/onboarding'>What happens at each stage</a></div>"
        f"{goal_status_html}"
        f"{wiki_html}"
        f"<h3>Business metrics</h3>{metrics}"
        f"<p><a class='btn' href='{escape(propose)}'>Propose a metric change</a> "
        "Changes are reviewed as a pull request to the tenant manifest in the factory repo.</p>"
        f"<h3>Recent observations</h3>{history}"
        f"<h3>Loop history</h3>{loop_history_html}"
    )
    if can_manage:
        body += _assignee_section(session, tenant, dispatcher)
        body += _goal_authoring_section(store, session, tenant)
        body += _metrics_authoring_section(session, tenant)
        body += _guardrails_section(store, session, tenant)
        body += _arbitration_section(session, tenant)
        body += _token_section(store, session, tenant) + _llm_section(store, session, tenant, kek, providers)
    return _page(full, body, session)


def _wiki_section(store: Store, session: Session, tenant: Tenant, can_manage: bool) -> str:
    wiki = store.wiki_for_tenant(tenant.id) or {}
    live_onb = store.active_session_for_role(tenant.id, "onboarder")
    raw_status = str(wiki.get("refresh_status") or "idle")
    if live_onb is not None and raw_status == "idle":
        raw_status = "running"
    pill_cls = "pass" if raw_status == "idle" else ("fail" if raw_status == "failed" else "none")

    commit_sha = str(wiki.get("commit_sha") or "").strip()
    if commit_sha and re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_sha):
        sha_html = (
            f"<a href='https://github.com/{quote(tenant.repo)}/commit/{quote(commit_sha)}'>"
            f"<code>{escape(commit_sha[:7])}</code></a>"
        )
    elif commit_sha:
        sha_html = f"<code>{escape(commit_sha[:12])}</code>"
    else:
        sha_html = "<code>—</code>"

    last_updated = str(wiki.get("last_updated") or "")[:19].replace("T", " ") or "never"
    merges_since = int(wiki.get("merges_since_refresh") or 0)
    raw_interval = wiki.get("refresh_interval_merges")
    interval_merges = int(raw_interval) if raw_interval is not None else 5
    if interval_merges > 0:
        cadence_str = f"{merges_since} / {interval_merges} merges since refresh"
    else:
        cadence_str = f"{merges_since} merges since refresh (auto-refresh disabled)"

    err_html = ""
    if wiki.get("last_error"):
        err_html = f"<p><span class='pill fail'>last refresh failed: {escape(str(wiki['last_error'])[:300])}</span></p>"

    purpose = str(wiki.get("purpose") or "").strip()
    techs = wiki.get("technologies") if wiki.get("technologies") is not None else wiki.get("key_technologies")
    raw_struct = wiki.get("structure") if wiki.get("structure") is not None else wiki.get("top_level_structure")
    structure_str = json.dumps(raw_struct) if isinstance(raw_struct, dict) else str(raw_struct or "").strip()
    concepts = wiki.get("concepts") if wiki.get("concepts") is not None else wiki.get("key_concepts")
    mermaid_str = str(wiki.get("architecture_mermaid") or "").strip()

    has_content = bool(purpose or techs or structure_str or concepts or mermaid_str)
    content_parts: list[str] = []
    if has_content:
        if purpose:
            content_parts.append(f"<p><b>Purpose:</b> {escape(purpose)}</p>")
        if isinstance(techs, list) and techs:
            tech_items = "".join(f"<li>{escape(str(t))}</li>" for t in techs)
            content_parts.append(f"<p><b>Key Technologies:</b></p><ul>{tech_items}</ul>")
        if structure_str:
            content_parts.append(f"<p><b>Top-Level Structure:</b></p><pre><code>{escape(structure_str)}</code></pre>")
        if isinstance(concepts, list) and concepts:
            concept_items = "".join(f"<li>{escape(str(c))}</li>" for c in concepts)
            content_parts.append(f"<p><b>Key Concepts:</b></p><ul>{concept_items}</ul>")
        if mermaid_str:
            content_parts.append(
                f"<p><b>Architecture Diagram (Mermaid):</b></p>"
                f"<pre class='mermaid'><code>{escape(mermaid_str)}</code></pre>"
            )
    else:
        content_parts.append("<p>No SotaWiki has been synthesized for this repository yet.</p>")

    admin_html = ""
    if can_manage:
        regen_action = f"/tenants/{quote(tenant.repo)}/wiki/regenerate"
        policy_action = f"/tenants/{quote(tenant.repo)}/wiki/policy"
        btn_label = "Queue Refresh" if raw_status in ("running", "queued") else "Force Regenerate Wiki"
        admin_html = (
            "<hr>"
            f"<form class='inline' method='post' action='{regen_action}'>"
            f"{_csrf_field(session)}<button class='btn'>{btn_label}</button></form> "
            f"<form class='inline' method='post' action='{policy_action}'>"
            f"{_csrf_field(session)}"
            f"<label>Auto-refresh every <input type='number' name='refresh_interval_merges' "
            f"min='0' max='100' value='{interval_merges}' required> default-branch merges (0 = manual only)</label> "
            f"<button class='btn'>Save wiki cadence</button></form>"
        )

    return (
        "<h3>Repository Wiki (SotaWiki)</h3>"
        "<div class='card' id='tenant-wiki'>"
        f"<p>Refresh status: <span class='pill {pill_cls}'>{escape(raw_status)}</span>"
        f" · Commit: {sha_html}"
        f" · Last updated: <code>{escape(last_updated)}</code>"
        f" · Cadence: <code>{escape(cadence_str)}</code></p>"
        f"{err_html}"
        f"{''.join(content_parts)}"
        f"{admin_html}"
        "</div>"
    )


def _goal_status_section(
    store: Store,
    tenant: Tenant,
    defs: list[dict[str, Any]],
    obs: list[dict[str, Any]],
) -> str:
    manifest: dict[str, Any] = dict(tenant.manifest) if isinstance(tenant.manifest, dict) else {}
    goal_cfg = manifest.get("goal")
    if not isinstance(goal_cfg, dict) or not goal_cfg.get("objective"):
        return ""

    goal_state = (store.get_tenant_goal(tenant.id) if hasattr(store, "get_tenant_goal") else None) or {}
    status_str = str(goal_state.get("status") or "open")
    stop_reason = str(goal_state.get("stop_reason") or "")
    spent_usd = float(goal_state.get("spent_usd") or 0.0)
    prs_merged = int(goal_state.get("prs_merged") or 0)
    stalled_merges = int(goal_state.get("merges_without_progress") or 0)

    raw_budget = goal_cfg.get("budget")
    budget_cfg: dict[str, Any] = dict(raw_budget) if isinstance(raw_budget, dict) else {}
    raw_stall = goal_cfg.get("stall")
    stall_cfg: dict[str, Any] = dict(raw_stall) if isinstance(raw_stall, dict) else {}
    max_cost = float(budget_cfg.get("max_goal_cost_usd", 100.0))
    max_prs = int(budget_cfg.get("max_goal_prs", 10))
    max_days = int(budget_cfg.get("max_goal_days", 14))
    max_stall = int(stall_cfg.get("max_merges_without_progress", 3))
    autonomy = str(goal_cfg.get("autonomy") or "approve")
    tracking_issue = goal_cfg.get("tracking_issue_number")

    defs_by_key = {str(d.get("key")): d for d in defs if d.get("key")}
    for m in manifest.get("metrics") or []:
        if isinstance(m, dict) and m.get("key") and m["key"] not in defs_by_key:
            defs_by_key[str(m["key"])] = m

    default_vals: dict[str, float] = {}
    for o in obs:
        if o.get("pr_number") is None and o.get("pr") is None and o.get("key") and o.get("value") is not None:
            default_vals[str(o["key"])] = float(o["value"])

    target_rows = []
    for t_key in goal_cfg.get("targets") or []:
        d = defs_by_key.get(str(t_key), {})
        comp = str(d.get("comparator") or ">=")
        thr = d.get("threshold")
        cur = default_vals.get(str(t_key))
        cur_str = f"{cur:g}" if cur is not None else "—"
        thr_str = f"{float(thr):g}" if thr is not None else "—"
        pct = 0
        if cur is not None and thr is not None and float(thr) != 0:
            if comp in (">=", ">"):
                pct = max(0, min(100, int(round((cur / float(thr)) * 100))))
            elif comp in ("<=", "<"):
                pct = 100 if cur <= float(thr) else max(0, min(100, int(round((float(thr) / max(cur, 1e-9)) * 100))))
            else:
                pct = 100 if abs(cur - float(thr)) <= 1e-9 else 50
        target_rows.append(
            f"<tr><td><code>{escape(str(t_key))}</code> <span class='pill ok'>target</span></td>"
            f"<td>{escape(cur_str)}</td>"
            f"<td>{escape(comp)} {escape(thr_str)}</td>"
            f"<td><progress value='{pct}' max='100'>{pct}%</progress> {pct}%</td></tr>"
        )

    guardrail_rows = []
    for g_key in goal_cfg.get("guardrails") or []:
        d = defs_by_key.get(str(g_key), {})
        comp = str(d.get("comparator") or ">=")
        thr = d.get("threshold")
        cur = default_vals.get(str(g_key))
        cur_str = f"{cur:g}" if cur is not None else "—"
        thr_str = f"{float(thr):g}" if thr is not None else "—"
        guardrail_rows.append(
            f"<tr><td><code>{escape(str(g_key))}</code> <span class='pill'>guardrail</span></td>"
            f"<td>{escape(cur_str)}</td>"
            f"<td>{escape(comp)} {escape(thr_str)}</td>"
            f"<td>active guardrail</td></tr>"
        )

    stop_html = f" · stop reason: <code>{escape(stop_reason)}</code>" if stop_reason else ""
    track_html = f" · tracking issue <code>#{int(tracking_issue)}</code>" if tracking_issue else ""
    metrics_tbl = (
        "<table><tr><th>Metric</th><th>Current (main)</th><th>Target / Rule</th><th>Progress</th></tr>"
        + "".join(target_rows + guardrail_rows)
        + "</table>"
    )
    return (
        "<h3>Outer Goal Loop Status</h3>"
        "<div class='card' id='tenant-goal-status'>"
        f"<p><b>Objective:</b> {escape(str(goal_cfg.get('objective')))}</p>"
        f"<p>Status: <code>{escape(status_str)}</code>{stop_html}"
        f" · Autonomy: <code>{escape(autonomy)}</code>{track_html}</p>"
        f"<p>Spend: <b>${spent_usd:.2f} / ${max_cost:.2f}</b>"
        f" · PRs Merged: <b>{prs_merged} / {max_prs}</b>"
        f" · Stalled Merges: <b>{stalled_merges} / {max_stall}</b>"
        f" · Max Days: <b>{max_days}d</b></p>"
        f"{metrics_tbl}</div>"
    )


def _goal_authoring_section(store: Store, session: Session, tenant: Tenant) -> str:
    action = f"/tenants/{quote(tenant.repo)}/goal"
    manifest: dict[str, Any] = dict(tenant.manifest) if isinstance(tenant.manifest, dict) else {}
    raw_goal = manifest.get("goal")
    goal_cfg: dict[str, Any] = dict(raw_goal) if isinstance(raw_goal, dict) else {}
    raw_budget = goal_cfg.get("budget")
    budget_cfg: dict[str, Any] = dict(raw_budget) if isinstance(raw_budget, dict) else {}
    raw_stall = goal_cfg.get("stall")
    stall_cfg: dict[str, Any] = dict(raw_stall) if isinstance(raw_stall, dict) else {}

    objective = str(goal_cfg.get("objective") or "")
    targets_csv = ", ".join(str(k) for k in (goal_cfg.get("targets") or []))
    guardrails_csv = ", ".join(str(k) for k in (goal_cfg.get("guardrails") or []))
    autonomy = str(goal_cfg.get("autonomy") or "approve")
    tracking_issue = goal_cfg.get("tracking_issue_number") or ""
    max_cost = float(budget_cfg.get("max_goal_cost_usd", 100.0))
    max_prs = int(budget_cfg.get("max_goal_prs", 10))
    max_days = int(budget_cfg.get("max_goal_days", 14))
    max_stall = int(stall_cfg.get("max_merges_without_progress", 3))
    max_wip = int(stall_cfg.get("max_open_planned_issues", 2))

    approve_sel = " selected" if autonomy == "approve" else ""
    auto_sel = " selected" if autonomy == "auto" else ""

    return (
        "<h3>Outer Goal Loop Configuration</h3>"
        "<div class='card' id='tenant-goal-form'>"
        "<p>Define a repository objective, target metrics to improve, guardrails to protect, and outer-loop budgets.</p>"
        f"<form method='post' action='{action}'>{_csrf_field(session)}"
        "<table>"
        f"<tr><th>Repository Objective</th><td><input type='text' name='objective' value='{escape(objective)}' "
        "placeholder='e.g. Raise coverage_pct above 85% without regressing p95_latency_ms' size='60' required></td></tr>"
        f"<tr><th>Target Metric Keys (comma-separated)</th><td><input type='text' name='targets' "
        f"value='{escape(targets_csv)}' placeholder='e.g. coverage_pct' size='40' required></td></tr>"
        f"<tr><th>Guardrail Metric Keys (comma-separated)</th><td><input type='text' name='guardrails' "
        f"value='{escape(guardrails_csv)}' placeholder='e.g. test_pass_rate, p95_latency_ms' size='40'></td></tr>"
        "<tr><th>Autonomy Mode</th><td><select name='autonomy'>"
        f"<option value='approve'{approve_sel}>approve (open with flow:needs-approval)</option>"
        f"<option value='auto'{auto_sel}>auto (open with flow:feature)</option>"
        "</select></td></tr>"
        f"<tr><th>Tracking Issue Number (optional)</th><td><input type='number' name='tracking_issue_number' "
        f"min='1' value='{escape(str(tracking_issue))}'></td></tr>"
        f"<tr><th>Max Goal Spend USD (0.01–5000)</th><td><input type='number' name='max_goal_cost_usd' "
        f"step='0.01' min='0.01' max='5000' value='{max_cost:g}' required></td></tr>"
        f"<tr><th>Max Goal PRs (1–100)</th><td><input type='number' name='max_goal_prs' "
        f"min='1' max='100' value='{max_prs}' required></td></tr>"
        f"<tr><th>Max Goal Days (1–90)</th><td><input type='number' name='max_goal_days' "
        f"min='1' max='90' value='{max_days}' required></td></tr>"
        f"<tr><th>Max Merges Without Progress (1–20)</th><td><input type='number' name='max_merges_without_progress' "
        f"min='1' max='20' value='{max_stall}' required></td></tr>"
        f"<tr><th>Max Open Planned Issues (1–5)</th><td><input type='number' name='max_open_planned_issues' "
        f"min='1' max='5' value='{max_wip}' required></td></tr>"
        "</table>"
        "<button class='btn'>Save goal</button></form></div>"
    )


def _metrics_authoring_section(session: Session, tenant: Tenant) -> str:
    action = f"/tenants/{quote(tenant.repo)}/metrics"
    return (
        "<h3>Repository Metrics (Plain-Language Authoring &amp; Compilation)</h3>"
        "<div class='card'>"
        "<p>Describe a metric in plain language. The metric compiler dry-runs it against your default branch "
        "and produces a deterministic digest for your approval before opening a workflow PR.</p>"
        f"<form method='post' action='{action}'>{_csrf_field(session)}"
        "<table>"
        "<tr><th>Metric key</th><td><input type='text' name='key' placeholder='e.g. dependency_count' "
        "pattern='[a-z][a-z0-9_]*' required></td></tr>"
        "<tr><th>Plain-language intent</th><td><input type='text' name='intent' "
        "placeholder='PR must not add Python dependencies compared with main' size='55' required></td></tr>"
        "<tr><th>Kind</th><td><select name='kind'>"
        "<option value='guardrail' selected>guardrail</option>"
        "<option value='target'>target</option>"
        "</select></td></tr>"
        "<tr><th>Rationale</th><td><input type='text' name='rationale' "
        "placeholder='Why this matters for the repository' size='55'></td></tr>"
        "</table>"
        "<button class='btn'>Save metric draft</button></form></div>"
    )


def _guardrails_section(store: Store, session: Session, tenant: Tenant) -> str:
    action = f"/tenants/{quote(tenant.repo)}/guardrails"
    manifest: dict[str, Any] = dict(tenant.manifest) if isinstance(tenant.manifest, dict) else {}
    raw_loop = manifest.get("loop")
    loop_cfg: dict[str, Any] = dict(raw_loop) if isinstance(raw_loop, dict) else {}
    spot_mins = 60
    if hasattr(store, "get_spot_max_minutes"):
        try:
            spot_mins = int(store.get_spot_max_minutes(tenant.id))
        except Exception:
            spot_mins = int(manifest.get("spot_max_instance_minutes", 60))
    elif manifest.get("spot_max_instance_minutes"):
        spot_mins = int(manifest["spot_max_instance_minutes"])

    wall_mins = int(loop_cfg.get("max_pr_wall_clock_minutes", 240))
    cost_usd = float(loop_cfg.get("max_pr_cost_usd") or loop_cfg.get("max_cost_usd_per_pr") or 25.0)
    rounds = int(loop_cfg.get("max_review_rounds", 4))
    repeated = int(loop_cfg.get("max_repeated_finding", 2))

    return (
        "<h3>Loop Policy &amp; Budget Caps</h3>"
        "<div class='card'>"
        "<p>Configure per-instance Spot runtime and per-PR loop guardrails without editing manifest files.</p>"
        f"<form method='post' action='{action}'>{_csrf_field(session)}"
        "<table>"
        f"<tr><th>Spot Max Instance Minutes (10–480)</th>"
        f"<td><input type='number' name='spot_max_instance_minutes' min='10' max='480' value='{spot_mins}' required></td></tr>"
        f"<tr><th>Max PR Wall-Clock Minutes (10–1440)</th>"
        f"<td><input type='number' name='max_pr_wall_clock_minutes' min='10' max='1440' value='{wall_mins}' required></td></tr>"
        f"<tr><th>Max PR Spend USD (0.01–200)</th>"
        f"<td><input type='number' name='max_pr_cost_usd' step='0.01' min='0.01' max='200' value='{cost_usd:g}' required></td></tr>"
        f"<tr><th>Max Review Rounds (1–10)</th>"
        f"<td><input type='number' name='max_review_rounds' min='1' max='10' value='{rounds}' required></td></tr>"
        f"<tr><th>Max Repeated Finding (1–5)</th>"
        f"<td><input type='number' name='max_repeated_finding' min='1' max='5' value='{repeated}' required></td></tr>"
        "</table>"
        "<button class='btn'>Save guardrails</button></form></div>"
    )


@router.get("/tenants/{owner}/{repo}/prs/{pr_number}", response_class=HTMLResponse)
def pr_timeline(
    owner: str,
    repo: str,
    pr_number: int,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    full = f"{owner}/{repo}"
    if session is None:
        return _login_redirect(f"/tenants/{quote(full)}/prs/{pr_number}")  # type: ignore[return-value]
    tenant = store.tenant_by_repo(full)
    if tenant is None or not session.can_see(full):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    rounds = store.loop_rounds_for_pr(tenant.id, pr_number)
    loop_policy = tenant.manifest.get("loop", {})
    max_rounds = loop_policy.get("max_review_rounds", 4)
    max_cost = loop_policy.get("max_pr_cost_usd", loop_policy.get("max_cost_usd_per_pr", 25.0))

    total_cost = sum(r.get("cost_usd", 0.0) for r in rounds)
    max_round_reached = max((r.get("round", 0) for r in rounds), default=0)

    rows = []
    for r in sorted(rounds, key=lambda x: (x.get("round", 0), x.get("created_at", ""))):
        digest = r.get("envelope_digest") or "—"
        usage_det = r.get("usage_detail") or {}
        cached_tok = usage_det.get("cached_input_tokens")
        if cached_tok is None and isinstance(usage_det.get("total"), dict):
            cached_tok = usage_det["total"].get("cached_input_tokens")
        tok_str = f"{r.get('tokens', 0):,}"
        if cached_tok is not None:
            tok_str += f" ({cached_tok:,} cached)"
        by_tool = usage_det.get("by_tool") if isinstance(usage_det, dict) else None
        if isinstance(by_tool, dict) and by_tool:
            parts = []
            for tname, tstat in sorted(
                by_tool.items(),
                key=lambda kv: int((kv[1] or {}).get("total_tokens", 0)),
                reverse=True,
            ):
                if isinstance(tstat, dict):
                    parts.append(
                        f"<code>{escape(tname)}</code>: {int(tstat.get('total_tokens', 0)):,} tok "
                        f"({int(tstat.get('calls', 0))}x)"
                    )
            if parts:
                tok_str += "<br><small>" + " · ".join(parts) + "</small>"

        rows.append(
            f"<tr><td>Round {r.get('round', 0)}</td>"
            f"<td><code>{escape(r.get('role', '—'))}</code></td>"
            f"<td><code>{escape(r.get('head_sha', '')[:7])}</code></td>"
            f"<td>{tok_str}</td>"
            f"<td>${r.get('cost_usd', 0.0):.4f}</td>"
            f"<td><code>{escape(digest)}</code></td>"
            f"<td>{escape(str(r.get('created_at', ''))[:19].replace('T', ' '))}</td></tr>"
        )

    headers_html = (
        "<tr><th>Round</th><th>Role</th><th>Commit</th>"
        "<th>Tokens</th><th>Cost</th><th>Envelope</th><th>Recorded (UTC)</th></tr>"
    )
    timeline_table = (
        f"<table>{headers_html}"
        + ("".join(rows) or "<tr><td colspan=7>No rounds recorded for this PR.</td></tr>")
        + "</table>"
    )

    body = (
        f"<p><a href='/tenants/{quote(full)}'>&larr; Back to {escape(full)}</a></p>"
        f"<h2>{escape(full)} · PR #{pr_number} Timeline</h2>"
        f"<div class='card'>"
        f"<b>Rounds:</b> {max_round_reached} / {max_rounds} max · "
        f"<b>Spend:</b> ${total_cost:.2f} / ${max_cost:.2f} max"
        f"</div>"
        f"<h3>Round execution history</h3>"
        f"{timeline_table}"
    )
    return _page(f"{full} #{pr_number}", body, session)


def _token_section(store: Store, session: Session, tenant: Tenant) -> str:
    toks = store.active_tokens(tenant.id)
    rows = "".join(
        f"<tr><td>{escape(t.get('label') or 'ci')}</td><td>{escape(t.get('owner_github_login') or '')}</td>"
        f"<td>{escape(str(t.get('created_at', ''))[:19].replace('T', ' '))}</td>"
        f"<td>{escape(str(t.get('last_used_at') or 'never')[:19].replace('T', ' '))}</td></tr>"
        for t in toks
    )
    table = (
        "<table><tr><th>Label</th><th>Issued by</th><th>Issued (UTC)</th><th>Last used (UTC)</th></tr>"
        + (rows or "<tr><td colspan=4>No active token.</td></tr>")
        + "</table>"
    )
    label = "Rotate token" if toks else "Generate token"
    btn_class = "btn-outline-danger" if toks else "btn"
    btn_style = " style='background:#fff;color:#cf222e;border:1px solid #cf222e'" if toks else ""
    warn = " This revokes the current token immediately." if toks else ""
    return (
        "<h3>Tenant API token</h3>"
        "<p>Your repository's CI uses this token to talk to atobar-flow. Keep it in the repository's "
        "Actions secrets as <code>ATOBAR_TENANT_TOKEN</code>; atobar-flow stores only its hash.</p>"
        f"{table}<form method='post' action='/tenants/{quote(tenant.repo)}/token'>{_csrf_field(session)}"
        f"<button class='{btn_class}'{btn_style}>{label}</button> {warn}</form>"
    )


ARBITRATION_MODES = {
    "shadow": "Decides on every PR and posts the verdict as a comment on the issue, plus a comment on the PR "
    "when a metric is violated. No check run, never merges. Start here.",
    "recommend": "Everything shadow does, plus the machine-readable flow-arbitration comment and a "
    "flow/arbiter check run on the PR head. A human merges.",
    "auto_merge": "Everything recommend does, and merges the PR when every metric passes.",
}


def _effective_assignee_login(tenant: Tenant) -> str:
    return resolve_human_assignee(tenant.manifest, tenant.repo)


_ASSIGNEE_CHECK_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}
_ASSIGNEE_CHECK_TTL_S = 300.0


def _assignee_is_valid(dispatcher: Optional[Any], repo: str, login: str) -> bool:
    """Ask GitHub whether `login` is assignable on `repo` (orgs and non-collaborators are not); cached."""
    if not login or login.endswith("[bot]"):
        return False
    if dispatcher is None or not hasattr(dispatcher, "is_valid_assignee"):
        return True
    now = time.monotonic()
    hit = _ASSIGNEE_CHECK_CACHE.get((repo, login))
    if hit is not None and now - hit[0] < _ASSIGNEE_CHECK_TTL_S:
        return hit[1]
    try:
        ok = bool(dispatcher.is_valid_assignee(repo, login))
    except Exception:
        return True
    _ASSIGNEE_CHECK_CACHE[(repo, login)] = (now, ok)
    return ok


def _collaborator_choices(
    tenant: Tenant,
    dispatcher: Optional[Any],
    *,
    allow_live_fetch: bool = False,
) -> list[str]:
    default_owner = tenant.repo.split("/", 1)[0] if "/" in tenant.repo else ""
    current_login = _effective_assignee_login(tenant)
    logins: list[str] = []
    should_call = dispatcher is not None and hasattr(dispatcher, "list_collaborators") and (
        allow_live_fetch
        or not isinstance(dispatcher, GitHubAppDispatcher)
        or tenant.repo in _CP_COLLAB_CACHE
    )
    if should_call and dispatcher is not None:
        try:
            for u in dispatcher.list_collaborators(tenant.repo) or []:
                s = str(u).strip()
                if s and not s.endswith("[bot]") and s not in logins:
                    logins.append(s)
        except Exception as exc:
            logger.warning("Could not list collaborators for %s: %s", tenant.repo, exc)
    if default_owner and default_owner not in logins:
        logins.insert(0, default_owner)
    if current_login and current_login not in logins:
        logins.append(current_login)
    return logins


def _assignee_section(session: Session, tenant: Tenant, dispatcher: Optional[Any]) -> str:
    default_owner = tenant.repo.split("/", 1)[0] if "/" in tenant.repo else ""
    current_login = _effective_assignee_login(tenant)
    choices = _collaborator_choices(tenant, dispatcher, allow_live_fetch=False)
    options_html = "".join(
        f"<option value='{escape(login)}'{' selected' if login == current_login else ''}>"
        f"@{escape(login)}{' (repo owner — default)' if login == default_owner else ''}</option>"
        for login in choices
    )
    action = f"/tenants/{quote(tenant.repo)}/assignee"
    return (
        "<h3>Human Assignee &amp; Reviewer</h3>"
        "<div class='card' id='tenant-assignee-form'>"
        "<p>When an issue needs plan approval (<code>flow:needs-approval</code>), trips a circuit breaker "
        "(<code>flow:escalated</code>), or has a PR ready for merge (<code>recommend</code> / <code>shadow</code>), "
        "Atobar assigns the issue or requests a PR review from this collaborator (defaults to the onboarding "
        "repository admin or owner).</p>"
        f"<form method='post' action='{action}'>{_csrf_field(session)}"
        f"<select name='owner_github_login'>{options_html}</select> "
        "<button class='btn'>Save assignee</button></form></div>"
    )


def _arbitration_section(session: Session, tenant: Tenant) -> str:
    action = f"/tenants/{quote(tenant.repo)}/arbitration-mode"
    rows = "".join(
        f"<tr><td><label><input type='radio' name='mode' value='{m}'"
        f"{' checked' if m == tenant.arbitration_mode else ''} onchange='this.form.submit()'> "
        f"<code>{m}</code></label></td><td>{escape(hint)}</td></tr>"
        for m, hint in ARBITRATION_MODES.items()
    )
    return (
        "<h3>Arbitration mode</h3><p>What the arbitrator does after the evaluator approves a PR. "
        "This setting wins over the tenant manifest.</p>"
        f"<form method='post' action='{action}'>{_csrf_field(session)}<table>{rows}</table></form>"
    )


LLM_ROLES = ("planner", "developer", "evaluator")
REASONING_EFFORTS = ("none", "low", "medium", "high")


def _is_llm_ready(store: Store, tenant_id: str) -> bool:
    keys = store.llm_keys(tenant_id)
    stored_providers = {str(k.get("provider") or "") for k in keys if k.get("provider")}
    if not stored_providers:
        return False
    prefs = store.role_llm_prefs(tenant_id) or {}
    for role in LLM_ROLES:
        pref = prefs.get(role) or {}
        if not (pref.get("provider") and pref.get("model")) and role == "planner":
            pref = prefs.get("developer") or {}
        prov = str(pref.get("provider") or "")
        model = str(pref.get("model") or "")
        if not prov or not model or prov not in stored_providers:
            return False
    return True


def _model_choices(keys: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(k["provider"], m) for k in keys for m in (k.get("available_models") or [])]


def _role_llm_table(store: Store, session: Session, tenant: Tenant, keys: list[dict[str, Any]]) -> str:
    prefs = store.role_llm_prefs(tenant.id)
    base = f"/tenants/{quote(tenant.repo)}/llm-roles"
    models_url = f"/tenants/{quote(tenant.repo)}/llm-models"
    choices = _model_choices(keys)
    rows = ""
    for role in LLM_ROLES:
        pref = prefs.get(role) or {}
        cur = (pref.get("provider", ""), pref.get("model", ""))
        opts = "" if cur[1] else "<option value='' selected disabled>Not configured</option>"
        for prov, model in ([cur] if cur[1] and cur not in choices else []) + choices:
            sel = " selected" if (prov, model) == cur else ""
            opts += f"<option value='{escape(prov)}|{escape(model)}'{sel}>{escape(prov)} · {escape(model)}</option>"
        effort = pref.get("reasoning_effort") or "none"
        efforts = "".join(
            f"<option value='{e}'{' selected' if e == effort else ''}>{e}</option>" for e in REASONING_EFFORTS
        )
        rows += (
            f"<tr><td>{role}</td><td><form method='post' action='{base}/{role}' style='margin:0;display:flex;gap:4px;'>"
            f"{_csrf_field(session)}<select name='model' data-models-url='{escape(models_url)}' "
            f"onmousedown='lazyLoadModels(this)' onfocus='lazyLoadModels(this)' onchange='this.form.submit()'>"
            f"{opts}</select>"
            f"<select name='reasoning_effort' onchange='this.form.submit()'>{efforts}</select></form></td></tr>"
        )
    script = (
        "<script>"
        "async function lazyLoadModels(sel){"
        "if(sel.dataset.loading==='1'||sel.dataset.loaded==='1')return;"
        "var url=sel.getAttribute('data-models-url');if(!url)return;"
        "sel.dataset.loading='1';"
        "try{"
        "var r=await fetch(url,{credentials:'same-origin'});"
        "if(!r.ok)return;"
        "var data=await r.json();"
        "var items=data.models||[];if(!items.length)return;"
        "var cur=sel.value;"
        "var seen={};"
        "var frag=document.createDocumentFragment();"
        "if(!cur){var ph=document.createElement('option');ph.value='';ph.disabled=true;ph.selected=true;"
        "ph.textContent='Not configured';frag.appendChild(ph);}"
        "items.forEach(function(it){"
        "var val=it.provider+'|'+it.model;if(seen[val])return;seen[val]=true;"
        "var o=document.createElement('option');o.value=val;o.textContent=it.provider+' \\u00b7 '+it.model;"
        "if(val===cur)o.selected=true;frag.appendChild(o);"
        "});"
        "if(cur&&!seen[cur]){var parts=cur.split('|');var co=document.createElement('option');"
        "co.value=cur;co.selected=true;co.textContent=parts[0]+' \\u00b7 '+(parts[1]||'');frag.appendChild(co);}"
        "sel.innerHTML='';sel.appendChild(frag);sel.dataset.loaded='1';"
        "}catch(e){}finally{sel.dataset.loading='0';}"
        "}"
        "</script>"
    )
    return (
        "<h4>Model per role</h4><p>A role's runs fail until it has a model here.</p>"
        "<table><tr><th>Role</th><th>Model · reasoning effort</th></tr>" + rows + "</table>" + script
    )


def _llm_section(
    store: Store, session: Session, tenant: Tenant, kek: Optional[bytes], providers: dict[str, Provider]
) -> str:
    keys = store.llm_keys(tenant.id)
    base = f"/tenants/{quote(tenant.repo)}/llm-keys"

    rows = ""
    for k in keys:
        prov = k["provider"]
        prov_label = providers[prov].label if prov in providers else prov

        rows += (
            f"<tr><td>{escape(prov_label)}</td>"
            f"<td><code>…{escape(k['key_last4'])}</code></td>"
            f"<td>{escape(str(k['validated_at'])[:19].replace('T', ' '))}</td><td>{escape(k['created_by'])}</td>"
            f"<td><form class='inline' method='post' action='{base}/{quote(prov)}/delete'>"
            f"{_csrf_field(session)}<button class='danger'>Remove</button></form></td></tr>"
        )

    table = (
        "<table><tr><th>Provider</th><th>Key</th><th>Validated (UTC)</th><th>Added by</th><th></th></tr>"
        + (rows or "<tr><td colspan=5>No keys yet. Agents for this repository cannot run until one is added.</td></tr>")
        + "</table>"
        + _role_llm_table(store, session, tenant, keys)
    )
    if kek is None:
        form = "<p>Key storage is not configured on this server yet.</p>"
    else:
        opts = "".join(f"<option value='{escape(p.id)}'>{escape(p.label)}</option>" for p in providers.values())
        links = " · ".join(
            f"<a href='{escape(p.docs_url)}'>get a {escape(p.label)} key</a>" for p in providers.values()
        )
        form = (
            f"<form method='post' action='{base}'>{_csrf_field(session)}"
            f"<select name='provider'>{opts}</select> "
            "<input type='password' name='api_key' placeholder='API key' autocomplete='off' required size='40'> "
            f"<button class='btn'>Validate and save</button></form><p>{links}</p>"
        )
    return (
        "<h3>LLM keys (bring your own)</h3>"
        "<p>Agents working on this repository use these keys. Each key is checked with a free call to the "
        "provider, encrypted before it is stored, and never shown again; you can only replace or remove it.</p>"
        f"{table}{form}"
    )


@router.get("/tenants/{owner}/{repo}/llm-models")
def refresh_tenant_llm_models(
    owner: str,
    repo: str,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    kek: Optional[bytes] = Depends(get_kek),
    kms_client: Optional[Any] = Depends(get_kms_client),
    providers: dict[str, Provider] = Depends(get_providers),
) -> Any:
    full = f"{owner}/{repo}"
    if session is None:
        return _login_redirect(f"/tenants/{quote(full)}")
    tenant = _managed_tenant(store, session, owner, repo)
    keys = store.llm_keys(tenant.id)
    models_out: list[dict[str, str]] = []
    for k in keys:
        prov_id = k["provider"]
        prov = providers.get(prov_id)
        avail = list(k.get("available_models") or [])
        if prov is not None and (kek is not None or kms_client is not None):
            key_row: Optional[dict[str, Any]] = None
            if hasattr(store, "get_tenant_llm_key_record"):
                key_row = store.get_tenant_llm_key_record(tenant.id, prov_id)
            if key_row is None and hasattr(store, "get_tenant_llm_ciphertext"):
                ct_only = store.get_tenant_llm_ciphertext(tenant.id, prov_id)
                if ct_only:
                    key_row = {"ciphertext": ct_only, "kek_version": k.get("kek_version", llm_keys.KEK_VERSION)}
            if key_row and key_row.get("ciphertext"):
                try:
                    api_key = llm_keys.open_llm_key(
                        key_row,
                        tenant_id=tenant.id,
                        provider=prov_id,
                        kek=kek,
                        kms_client=kms_client,
                    )
                    valid, fresh_models = prov.validate(api_key)
                    if valid and fresh_models:
                        avail = fresh_models
                        store.put_llm_key(
                            tenant.id,
                            prov_id,
                            {
                                **key_row,
                                "key_last4": k.get("key_last4", ""),
                                "validated_at": datetime.now(timezone.utc).isoformat(),
                                "created_by": k.get("created_by", session.login),
                                "available_models": fresh_models,
                            },
                        )
                except Exception:
                    pass
        for m in avail:
            models_out.append({"provider": prov_id, "model": m})
    return {"models": models_out}


@router.post("/tenants")
async def add_tenant(
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect("/")
    form = await _form(request, session)
    repo = form.get("repo", "")
    if not session.can_manage(repo):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only repository admins can add a repository")
    request.state.tenant = repo
    if store.tenant_by_repo(repo) is None:
        fe.create_tenant(
            CreateTenantRequest(repo=repo, created_by=session.login, dispatch_onboard=False),
            request_id=_req_id(request),
            actor_login=session.login,
        )
    return RedirectResponse(f"/tenants/{quote(repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/token", response_class=HTMLResponse)
async def rotate_token(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")  # type: ignore[return-value]
    await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    store.revoke_tokens(tenant.id)
    token = mint_token()
    store.add_token(tenant.id, hash_token(token), session.login, "ci")
    if hasattr(store, "record_security_audit"):
        store.record_security_audit(
            "tenant_token.issued",
            tenant_id=tenant.id,
            actor=session.login,
            metadata={"label": "ci"},
        )
    full = escape(tenant.repo)
    masked_token = "•" * (len(token) - 5) + token[-5:] if len(token) > 5 else token
    copy_js = f"navigator.clipboard.writeText('{escape(token)}'); this.textContent = 'Copied!';"
    body = (
        f"<h2>{full}: new API token</h2><div class='card warn'><p><b>Copy it now; it will not be shown "
        "again.</b> Any previous token has been revoked.</p>"
        f"<div style='display:flex;gap:12px;align-items:center'>"
        f"<code class='secret' style='flex-grow:1;user-select:none'>{escape(masked_token)}</code>"
        f"<button class='btn' onclick=\"{copy_js}\">Copy token</button>"
        f"</div></div>"
        "<div class='card'><p>Save it into the repository's Actions secrets. Either run this and paste the "
        "token when prompted:</p>"
        f"<code class='secret'>gh secret set ATOBAR_TENANT_TOKEN -R {full}</code>"
        f"<p>or add it under <a href='https://github.com/{quote(tenant.repo)}/settings/secrets/actions/new'>"
        "Settings → Secrets and variables → Actions</a> with the name <code>ATOBAR_TENANT_TOKEN</code>.</p>"
        f"<p><a href='/tenants/{quote(tenant.repo)}'>Back to {full}</a></p></div>"
    )
    return _page(f"{tenant.repo} token", body, session)


@router.post("/tenants/{owner}/{repo}/llm-keys")
async def save_llm_key(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    kek: Optional[bytes] = Depends(get_kek),
    kms_client: Optional[Any] = Depends(get_kms_client),
    providers: dict[str, Provider] = Depends(get_providers),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    if kek is None and kms_client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "key storage is not configured")
    provider = providers.get(form.get("provider", ""))
    api_key = form.get("api_key", "").strip()
    if provider is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported provider")
    if not (8 <= len(api_key) <= 512) or any(c.isspace() for c in api_key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that does not look like an API key")
    try:
        valid, models = provider.validate(api_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    except httpx.HTTPError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not reach {provider.label}; try again")
    if not valid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{provider.label} rejected this key")
    sealed = llm_keys.seal_llm_key(
        api_key,
        tenant_id=tenant.id,
        provider=provider.id,
        kek=kek,
        kms_client=kms_client,
    )
    store.put_llm_key(
        tenant.id,
        provider.id,
        {
            "ciphertext": sealed.ciphertext,
            "wrapped_dek": sealed.wrapped_dek,
            "kek_version": sealed.kek_version,
            "kms_key_id": sealed.kms_key_id,
            "encryption_mode": sealed.encryption_mode,
            "key_last4": llm_keys.last4(api_key),
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "created_by": session.login,
            "available_models": models,
        },
    )
    if hasattr(store, "record_security_audit"):
        store.record_security_audit(
            "llm_key.upserted",
            tenant_id=tenant.id,
            actor=session.login,
            metadata={
                "provider": provider.id,
                "kek_version": sealed.kek_version,
                "encryption_mode": sealed.encryption_mode,
                "EncryptionContext": {"tenant_id": str(tenant.id), "provider": provider.id},
            },
        )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/arbitration-mode")
async def update_arbitration_mode(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    mode = form.get("mode", "")
    if mode not in ARBITRATION_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown arbitration mode")
    fe.update_arbitration_mode(
        tenant.id,
        ArbitrationModeUpdateRequest(mode=mode, actor=session.login),  # type: ignore[arg-type]
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/assignee")
async def update_tenant_assignee(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
    dispatcher: Optional[Dispatcher] = Depends(get_dispatcher),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    chosen = (form.get("owner_github_login") or "").strip() or owner
    if dispatcher is not None and hasattr(dispatcher, "is_valid_assignee"):
        if not dispatcher.is_valid_assignee(tenant.repo, chosen):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"'{chosen}' cannot be assigned issues/PRs on {tenant.repo}",
            )
    allowed = _collaborator_choices(tenant, dispatcher, allow_live_fetch=True)
    if allowed and chosen not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"'{chosen}' is not a collaborator on {tenant.repo}",
        )
    try:
        payload = AssigneeUpdateRequest(owner_github_login=chosen, actor=session.login)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    fe.update_assignee(
        tenant.id,
        payload,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/guardrails")
@router.post("/tenants/{owner}/{repo}/loop-policy")
async def update_tenant_guardrails(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo

    manifest: dict[str, Any] = dict(tenant.manifest) if isinstance(tenant.manifest, dict) else {}
    raw_loop = manifest.get("loop")
    loop_cfg: dict[str, Any] = dict(raw_loop) if isinstance(raw_loop, dict) else {}
    cur_spot = 60
    if hasattr(store, "get_spot_max_minutes"):
        try:
            cur_spot = int(store.get_spot_max_minutes(tenant.id))
        except Exception:
            cur_spot = 60

    try:
        req_payload = GuardrailsUpdateRequest(
            spot_max_instance_minutes=int(form.get("spot_max_instance_minutes") or cur_spot),
            max_pr_wall_clock_minutes=int(
                form.get("max_pr_wall_clock_minutes") or loop_cfg.get("max_pr_wall_clock_minutes") or 240
            ),
            max_pr_cost_usd=float(
                form.get("max_pr_cost_usd")
                or loop_cfg.get("max_pr_cost_usd")
                or loop_cfg.get("max_cost_usd_per_pr")
                or 25.0
            ),
            max_review_rounds=int(form.get("max_review_rounds") or loop_cfg.get("max_review_rounds") or 4),
            max_repeated_finding=int(form.get("max_repeated_finding") or loop_cfg.get("max_repeated_finding") or 2),
            actor=session.login,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    fe.update_guardrails(
        tenant.id,
        req_payload,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/goal")
async def update_tenant_goal(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo

    targets = [k.strip() for k in str(form.get("targets") or "").split(",") if k.strip()]
    guardrails = [k.strip() for k in str(form.get("guardrails") or "").split(",") if k.strip()]
    raw_tracking = str(form.get("tracking_issue_number") or "").strip()
    autonomy_val = str(form.get("autonomy") or "approve").strip()
    if autonomy_val not in ("approve", "auto"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "autonomy must be 'approve' or 'auto'")

    prev_goal = (tenant.manifest or {}).get("goal") if isinstance(tenant.manifest, dict) else None
    prev_autonomy = prev_goal.get("autonomy") if isinstance(prev_goal, dict) else None

    try:
        req_payload = GoalUpdateRequest(
            objective=str(form.get("objective") or "").strip(),
            targets=targets,
            guardrails=guardrails,
            autonomy="auto" if autonomy_val == "auto" else "approve",
            tracking_issue_number=int(raw_tracking) if raw_tracking else None,
            budget=GoalBudget(
                max_goal_cost_usd=float(form.get("max_goal_cost_usd") or 100.0),
                max_goal_prs=int(form.get("max_goal_prs") or 10),
                max_goal_days=int(form.get("max_goal_days") or 14),
            ),
            stall=GoalStallPolicy(
                max_merges_without_progress=int(form.get("max_merges_without_progress") or 3),
                max_open_planned_issues=int(form.get("max_open_planned_issues") or 2),
            ),
            actor=session.login,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    fe.update_goal(
        tenant.id,
        req_payload,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    store.record_security_audit(
        "goal.updated",
        tenant_id=tenant.id,
        actor=session.login,
        metadata={
            "autonomy": req_payload.autonomy,
            "previous_autonomy": prev_autonomy,
            "targets": req_payload.targets,
            "guardrails": req_payload.guardrails,
            "tracking_issue_number": req_payload.tracking_issue_number,
        },
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/metrics")
async def create_or_update_metric_draft_from_form(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo

    key = (form.get("key") or "").strip()
    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
    try:
        draft_req = MetricDraftRequest(
            intent=(form.get("intent") or "").strip(),
            kind=(form.get("kind") or "guardrail").strip(),  # type: ignore[arg-type]
            rationale=(form.get("rationale") or "").strip(),
            actor=session.login,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    fe.upsert_metric_draft(
        tenant.id,
        key,
        draft_req,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/metrics/{key}")
async def upsert_metric_draft_by_key(
    owner: str,
    repo: str,
    key: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo

    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
    try:
        draft_req = MetricDraftRequest(
            intent=(form.get("intent") or "").strip(),
            kind=(form.get("kind") or "guardrail").strip(),  # type: ignore[arg-type]
            rationale=(form.get("rationale") or "").strip(),
            actor=session.login,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    fe.upsert_metric_draft(
        tenant.id,
        key,
        draft_req,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/metrics/{key}/compile")
async def trigger_metric_compilation(
    owner: str,
    repo: str,
    key: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")

    fe.compile_metric(
        tenant.id,
        key,
        MetricCompileRequest(actor=session.login),
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/metrics/{key}/approve")
async def approve_compiled_metric(
    owner: str,
    repo: str,
    key: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")

    digest = (form.get("digest") or "").strip()
    if not digest:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "digest is required")
    cp_url = cfg.public_url if cfg else resolve_cp_url()
    fe.approve_metric(
        tenant.id,
        key,
        MetricApproveRequest(digest=digest, actor=session.login, cp_url=cp_url),
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/metrics/{key}/retire")
async def retire_active_metric(
    owner: str,
    repo: str,
    key: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")

    cp_url = cfg.public_url if cfg else resolve_cp_url()
    fe.retire_metric(
        tenant.id,
        key,
        MetricRetireRequest(actor=session.login, cp_url=cp_url),
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/llm-roles/{role}")
async def update_role_llm(
    owner: str,
    repo: str,
    role: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    provider, _, model = form.get("model", "").partition("|")
    effort = form.get("reasoning_effort", "none")
    if role not in LLM_ROLES or effort not in REASONING_EFFORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown role or reasoning effort")
    if (provider, model) not in _model_choices(store.llm_keys(tenant.id)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "pick a model offered by one of this repository's keys")
    store.set_role_llm_pref(tenant.id, role, provider, model, effort)
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/llm-keys/{provider}/delete")
async def delete_llm_key(
    owner: str,
    repo: str,
    provider: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    store.delete_llm_key(tenant.id, provider)
    if hasattr(store, "record_security_audit"):
        store.record_security_audit(
            "llm_key.deleted",
            tenant_id=tenant.id,
            actor=session.login,
            metadata={"provider": provider},
        )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/wiki/regenerate")
async def regenerate_tenant_wiki(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    fe.regenerate_wiki(
        tenant.id,
        WikiRegenerateRequest(actor=session.login),
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}#tenant-wiki", status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{owner}/{repo}/wiki/policy")
async def update_tenant_wiki_policy(
    owner: str,
    repo: str,
    request: Request,
    session: Optional[Session] = Depends(current_session),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> RedirectResponse:
    if session is None:
        return _login_redirect(f"/tenants/{quote(f'{owner}/{repo}')}")
    form = await _form(request, session)
    tenant = _managed_tenant(store, session, owner, repo)
    request.state.tenant = tenant.repo
    try:
        raw_interval = str(form.get("refresh_interval_merges", "")).strip()
        req_pol = WikiPolicyUpdateRequest(refresh_interval_merges=int(raw_interval), actor=session.login)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid wiki refresh cadence: {exc}") from exc
    fe.update_wiki_policy(
        tenant.id,
        req_pol,
        request_id=_req_id(request),
        tenant_repo=tenant.repo,
        actor_login=session.login,
    )
    return RedirectResponse(f"/tenants/{quote(tenant.repo)}#tenant-wiki", status.HTTP_303_SEE_OTHER)


@router.get("/tenants/{owner}/{repo}/llm-keys/{provider}/delete")
@router.get("/tenants/{owner}/{repo}/llm-keys")
@router.get("/tenants/{owner}/{repo}/token")
@router.get("/tenants/{owner}/{repo}/arbitration-mode")
@router.get("/tenants/{owner}/{repo}/guardrails")
@router.get("/tenants/{owner}/{repo}/loop-policy")
@router.get("/tenants/{owner}/{repo}/goal")
@router.get("/tenants/{owner}/{repo}/wiki")
@router.get("/tenants/{owner}/{repo}/wiki/regenerate")
@router.get("/tenants/{owner}/{repo}/wiki/policy")
@router.get("/tenants/{owner}/{repo}/metrics")
@router.get("/tenants/{owner}/{repo}/metrics/{key}")
@router.get("/tenants/{owner}/{repo}/metrics/{key}/compile")
@router.get("/tenants/{owner}/{repo}/metrics/{key}/approve")
@router.get("/tenants/{owner}/{repo}/metrics/{key}/retire")
@router.get("/tenants/{owner}/{repo}/llm-roles/{role}")
def tenant_action_get_redirect(
    owner: str,
    repo: str,
    session: Optional[Session] = Depends(current_session),
) -> RedirectResponse:
    target = f"/tenants/{quote(f'{owner}/{repo}')}"
    if session is None:
        return _login_redirect(target)
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)


@router.get("/login")
def login(
    next: str = "/",
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
) -> RedirectResponse:
    if cfg is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub sign-in is not configured")
    state = secrets.token_urlsafe(24)
    safe_next = _safe_next(next, "/")
    url = f"{GITHUB}/login/oauth/authorize?" + urlencode(
        {"client_id": cfg.client_id, "redirect_uri": f"{cfg.public_url}/auth/callback", "state": state}
    )
    resp = RedirectResponse(url, status.HTTP_302_FOUND)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, **_cookie_kwargs())
    if safe_next != "/":
        resp.set_cookie(NEXT_COOKIE, safe_next, max_age=600, **_cookie_kwargs())
    else:
        resp.delete_cookie(NEXT_COOKIE, path="/")
    return resp


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str = "",
    state: str = "",
    cfg: Optional[OAuthConfig] = Depends(get_oauth_config),
    gh: GitHubUser = Depends(get_github_user),
    store: Store = Depends(get_store),
) -> RedirectResponse:
    if cfg is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub sign-in is not configured")
    expected = request.cookies.get(STATE_COOKIE, "")
    if not (code and state and expected and hmac.compare_digest(state, expected)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sign-in expired, please try again")
    token = gh.exchange_code(cfg, code)
    who = gh.login(token)
    admin = who.lower() in cfg.admin_logins
    perms = gh.repo_permissions(token)
    tenants = {t.repo for t in store.list_tenants()}
    repos = sorted(r for r, p in perms.items() if r in tenants and p.get("pull"))[:MAX_SESSION_REPOS]
    manage = sorted(r for r, p in perms.items() if p.get("admin"))[:MAX_SESSION_REPOS]
    iat = int(time.time())
    csrf_val = hmac.new(
        cfg.session_secret.encode("utf-8"),
        f"csrf:{who}:{iat}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "login": who,
        "repos": repos,
        "manage": manage,
        "admin": admin,
        "csrf": csrf_val,
        "iat": iat,
        "exp": iat + SESSION_TTL_S,
    }
    session = sign(cfg.session_secret, payload)
    target = _safe_next(request.cookies.get(NEXT_COOKIE), "/")
    resp = RedirectResponse(target, status.HTTP_303_SEE_OTHER)
    resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL_S, **_cookie_kwargs())
    resp.delete_cookie(STATE_COOKIE, path="/")
    resp.delete_cookie(NEXT_COOKIE, path="/")
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    session: Optional[Session] = Depends(current_session),
) -> RedirectResponse:
    if not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request")
    if session is not None:
        await _form(request, session)
    resp = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
