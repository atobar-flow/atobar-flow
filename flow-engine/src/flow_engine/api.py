"""Flow Engine internal FastAPI service on :8081 (#301 WP-2)."""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import httpx
import jwt
from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Request, Response, status

from atobar_contracts.api import (
    ArbitrationDecisionPayload,
    ArbitrationModeUpdateRequest,
    AssigneeUpdateRequest,
    CreateTenantRequest,
    GoalUpdateRequest,
    GuardrailsUpdateRequest,
    HeartbeatPayload,
    InternalEventRequest,
    InternalMetricsRequest,
    MetricApproveRequest,
    MetricCompilationPayload,
    MetricCompileRequest,
    MetricDraftRequest,
    MetricRetireRequest,
    RunEventsBatchPayload,
    TurnCompletePayload,
    WikiPolicyUpdateRequest,
    WikiRegenerateRequest,
)
from atobar_contracts.client_templates import CONTROL_LABELS
from atobar_contracts.conductor_io import (
    compute_event_id,
    handle_merged_pr_event,
    is_issue_awaiting_plan_approval,
    process_flow_event,
    resolve_delegated_issue_number,
    resolve_human_assignee,
    upsert_issue_comment_by_marker,
)
from atobar_contracts.constants import LOOP_EPOCH_MARKER, get_bot_logins
from atobar_contracts.envelopes import (
    GOAL_ISSUE_LABEL,
    execute_goal_plan_proposals,
    is_goal_planned_issue,
    parse,
    parse_and_validate_goal_plan,
)
from atobar_contracts.github_rest import RealGitHubClient, launch_spot_instance
from atobar_contracts.goal import (
    GoalMetAction,
    GoalPlanAction,
    GoalState,
    GoalStopAction,
    GoalWaitAction,
    TenantGoal,
    goal_step,
)
from atobar_contracts.issue_states import (
    ONBOARDING_HOLD_MARKER,
    render_onboarding_blocked_comment,
    render_status_card,
)
from atobar_contracts.manifest import (
    METRIC_KEY_RE,
    REPO_RE,
    Metric,
    TenantManifest,
    canonical_sha,
    validate_compiled_metric_command,
)
from atobar_contracts.run_credentials import (
    ROLE_GITHUB_PERMISSIONS,
    MintRateLimiter,
    generate_run_credentials,
)
from atobar_contracts.store import SESSION_STALE_AFTER_MINUTES, Tenant
from atobar_contracts.sufficiency import assess_issue_sufficiency
from flow_engine.provenance import verify_and_extract_metrics
from flow_engine.store import get_store
from flow_engine.tenancy.render import render_metrics

logger = logging.getLogger("flow_engine.api")

BLOCKED_TRIGGER_LABELS = {
    "flow:in-progress",
    "flow:in-review",
    "flow:escalated",
    "flow:needs-approval",
    "flow:shipped",
}
_GITHUB_TOKEN_MINT_LIMITER = MintRateLimiter()

app = FastAPI(title="atobar-flow Flow Engine", version="0.2.0")


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_internal_token(
    authorization: Optional[str] = Header(default=None),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
) -> None:
    expected = os.environ.get("FE_INTERNAL_TOKEN", "").strip()
    provided = _extract_bearer(authorization) or (x_internal_token or "").strip()
    if not provided:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing internal authentication token")
    if not expected or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid internal authentication token")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Any) -> Response:
    incoming = (request.headers.get("X-Request-ID") or request.headers.get("x-request-id") or "").strip()
    req_id = incoming if incoming else str(uuid.uuid4())
    request.state.request_id = req_id
    request.state.tenant = None
    request.state.run_id = None
    t0 = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    response.headers["X-Request-ID"] = req_id
    log_doc = {
        "request_id": req_id,
        "tenant": getattr(request.state, "tenant", None) or request.path_params.get("tenant_id"),
        "run_id": getattr(request.state, "run_id", None) or request.path_params.get("run_id"),
        "route": request.url.path,
        "status": response.status_code,
        "ms": elapsed_ms,
    }
    logger.info(json.dumps(log_doc, sort_keys=True))
    return response


class FlowEngineGitHubAppClient:
    """GitHub App client for Flow Engine provenance verification and Conductor GitHub I/O."""

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key: str,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key.replace("\\n", "\n")
        self.base_url = base_url.rstrip("/")
        self._token_lock = threading.Lock()
        self._repo_locks: dict[str, threading.Lock] = {}
        self._token_cache: dict[str, tuple[dict[str, str], datetime]] = {}

    def _lock_for_repo(self, repo: Any) -> threading.Lock:
        with self._token_lock:
            lock = self._repo_locks.get(repo)
            if lock is None:
                lock = threading.Lock()
                self._repo_locks[repo] = lock
            return lock

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key, "RS256")

    def mint_repo_token(self, repo: str, role: Optional[str] = None) -> dict[str, str]:
        cache_key: Any = (repo, role) if role is not None else repo
        with self._lock_for_repo(cache_key):
            now_dt = datetime.now(timezone.utc)
            with self._token_lock:
                cached = self._token_cache.get(cache_key)
            if cached is not None:
                cached_entry, valid_until = cached
                if now_dt < valid_until:
                    return dict(cached_entry)

            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/installation",
                headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if resp.status_code == 404:
                raise PermissionError("GitHub App not installed on repo")
            resp.raise_for_status()
            inst_json = resp.json()
            account_obj = inst_json.get("account") if isinstance(inst_json, dict) else None
            account_login = str((account_obj or {}).get("login") or "").strip().lower()
            expected_owner = repo.split("/", 1)[0].strip().lower()
            if not account_login or account_login != expected_owner:
                raise PermissionError(
                    f"GitHub App installation account '{account_login}' does not match repo owner '{expected_owner}'"
                )
            inst_id = inst_json["id"]
            inst_perms: dict[str, Any] = (
                inst_json["permissions"] if isinstance(inst_json.get("permissions"), dict) else {}
            )
            repo_name = repo.split("/", 1)[-1]
            if role is not None:
                role_key = str(role).strip().lower()
                if role_key not in ROLE_GITHUB_PERMISSIONS:
                    raise PermissionError(f"Unsupported run role for GitHub token minting: {role}")
                desired_perms: dict[str, str] = dict(ROLE_GITHUB_PERMISSIONS[role_key])
            else:
                desired_perms = {
                    "contents": "write",
                    "workflows": "write",
                    "pull_requests": "write",
                    "issues": "write",
                    "checks": "write",
                    "metadata": "read",
                }
            if inst_perms:
                desired_perms = {
                    k: ("read" if inst_perms.get(k) == "read" else v)
                    for k, v in desired_perms.items()
                    if k in inst_perms
                }
            resp_tok = httpx.post(
                f"{self.base_url}/app/installations/{inst_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
                json={
                    "repositories": [repo_name],
                    "permissions": desired_perms,
                },
                timeout=15,
            )
            resp_tok.raise_for_status()
            data = resp_tok.json()
            expires_at_str = str(data.get("expires_at") or "")
            entry = {
                "token": str(data["token"]),
                "expires_at": expires_at_str,
            }
            valid_until = now_dt + timedelta(seconds=3540)
            if expires_at_str:
                try:
                    exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    valid_until = exp_dt - timedelta(seconds=60)
                except ValueError:
                    pass
            if now_dt < valid_until:
                with self._token_lock:
                    self._token_cache[cache_key] = (dict(entry), valid_until)
            return entry

    def mint_engine_read_token(self, engine_repo: str = "atobar-flow/atobar-flow") -> str:
        """Mints a strictly read-only installation token scoped to the engine repository for Phase 0 checkout."""
        cache_key = f"__engine_read__:{engine_repo}"
        with self._lock_for_repo(cache_key):
            now_dt = datetime.now(timezone.utc)
            with self._token_lock:
                cached = self._token_cache.get(cache_key)
            if cached is not None:
                cached_entry, valid_until = cached
                if now_dt < valid_until:
                    return str(cached_entry.get("token") or "")

            resp = httpx.get(
                f"{self.base_url}/repos/{engine_repo}/installation",
                headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if resp.status_code == 404:
                inst_id: Any = self._installation_id
            else:
                resp.raise_for_status()
                inst_id = resp.json()["id"]
            repo_name = engine_repo.split("/", 1)[-1]
            resp_tok = httpx.post(
                f"{self.base_url}/app/installations/{inst_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
                json={
                    "repositories": [repo_name],
                    "permissions": {
                        "contents": "read",
                        "metadata": "read",
                    },
                },
                timeout=15,
            )
            resp_tok.raise_for_status()
            data = resp_tok.json()
            tok = str(data.get("token") or "")
            expires_at_str = str(data.get("expires_at") or "")
            valid_until = now_dt + timedelta(seconds=3540)
            if expires_at_str:
                try:
                    exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    valid_until = exp_dt - timedelta(seconds=60)
                except ValueError:
                    pass
            if tok and now_dt < valid_until:
                with self._token_lock:
                    self._token_cache[cache_key] = ({"token": tok, "expires_at": expires_at_str}, valid_until)
            return tok

    def _client_for_repo(self, repo: str) -> RealGitHubClient:
        tok = self.mint_repo_token(repo)["token"]
        return RealGitHubClient(token=tok, base_url=self.base_url)

    def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        tok = self.mint_repo_token(repo)["token"]
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/actions/runs/{run_id}",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
        return dict(data) if isinstance(data, dict) else {}

    def download_flow_metrics_artifact(self, repo: str, run_id: int) -> bytes:
        max_zip_bytes = 1_048_576
        tok = self.mint_repo_token(repo)["token"]
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/artifacts",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        artifacts = (resp.json() or {}).get("artifacts") or []
        target = next((a for a in artifacts if a.get("name") == "flow-metrics" and not a.get("expired")), None)
        if not target or not target.get("id"):
            return b""
        size_in_bytes = target.get("size_in_bytes")
        if size_in_bytes is not None and int(size_in_bytes) > max_zip_bytes:
            raise ValueError(
                f"flow-metrics artifact exceeds {max_zip_bytes} byte limit (size_in_bytes={size_in_bytes})"
            )
        url = f"{self.base_url}/repos/{repo}/actions/artifacts/{target['id']}/zip"
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=30) as dl:
            dl.raise_for_status()
            buf = bytearray()
            for chunk in dl.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_zip_bytes:
                    raise ValueError(f"flow-metrics artifact stream exceeds {max_zip_bytes} byte limit")
            return bytes(buf)

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._client_for_repo(repo).get_pr(repo, pr_number)

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self._client_for_repo(repo).get_issue(repo, issue_number)

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        return self._client_for_repo(repo).get_pr_files(repo, pr_number)

    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]:
        return self._client_for_repo(repo).find_pr_for_sha(repo, sha)

    def get_commit_sha(self, repo: str, ref: str) -> str:
        return self._client_for_repo(repo).get_commit_sha(repo, ref)

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return self._client_for_repo(repo).get_comments(repo, pr_number)

    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        return self._client_for_repo(repo).get_comment(repo, pr_number, comment_id)

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        return self._client_for_repo(repo).get_check_runs(repo, ref)

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        self._client_for_repo(repo).create_check_run(repo, name, head_sha, conclusion, output)

    def create_comment(self, repo: str, issue_number: int, body: str) -> Any:
        return self._client_for_repo(repo).create_comment(repo, issue_number, body)

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> Any:
        return self._client_for_repo(repo).post_pr_comment(repo, pr_number, body)

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        self._client_for_repo(repo).add_label(repo, issue_number, label)

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self._client_for_repo(repo).remove_label(repo, issue_number, label)

    def get_default_branch(self, repo: str) -> str:
        return self._client_for_repo(repo).get_default_branch(repo)

    def list_branches(self, repo: str) -> list[dict[str, Any]]:
        return self._client_for_repo(repo).list_branches(repo)

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        return self._client_for_repo(repo).list_prs_for_branch(repo, branch)

    def compare_commits(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        return self._client_for_repo(repo).compare_commits(repo, base, head)

    def delete_branch(self, repo: str, branch: str) -> None:
        self._client_for_repo(repo).delete_branch(repo, branch)

    def get_collaborator_permission(self, repo: str, username: str) -> Optional[str]:
        tok = self.mint_repo_token(repo)["token"]
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/collaborators/{username}/permission",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        perm = resp.json().get("permission")
        return str(perm) if perm else None

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        tok = self.mint_repo_token(repo)["token"]
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/issues/{issue_number}",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        data = resp.json()
        return f"{data.get('title') or ''}\n\n{data.get('body') or ''}".strip()

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        tok = self.mint_repo_token(repo)["token"]
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/issues",
            params={"state": "open", "labels": label},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200 or not resp.json():
            return False
        issue_number = int(resp.json()[0]["number"])
        self.create_comment(repo, issue_number, comment)
        return True

    def open_or_update_metrics_pr(
        self,
        repo: str,
        workflow_yaml: str,
        default_branch: str = "main",
    ) -> Optional[dict[str, Any]]:
        import base64

        tok = self.mint_repo_token(repo)["token"]
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        branch = "atobar/metrics-policy"
        ref_resp = httpx.get(
            f"{self.base_url}/repos/{repo}/git/ref/heads/{default_branch}",
            headers=headers,
            timeout=15,
        )
        ref_resp.raise_for_status()
        base_sha = str((ref_resp.json().get("object") or {}).get("sha") or "")
        if not base_sha:
            return None

        branch_ref = httpx.get(
            f"{self.base_url}/repos/{repo}/git/ref/heads/{branch}",
            headers=headers,
            timeout=15,
        )
        if branch_ref.status_code == 404:
            httpx.post(
                f"{self.base_url}/repos/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
                timeout=15,
            ).raise_for_status()

        wf_path = ".github/workflows/flow-metrics.yml"
        existing_file = httpx.get(
            f"{self.base_url}/repos/{repo}/contents/{wf_path}",
            params={"ref": branch},
            headers=headers,
            timeout=15,
        )
        put_body: dict[str, Any] = {
            "message": "chore(atobar): update flow-metrics.yml policy",
            "content": base64.b64encode(workflow_yaml.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_file.status_code == 200 and existing_file.json().get("sha"):
            put_body["sha"] = existing_file.json()["sha"]
        httpx.put(
            f"{self.base_url}/repos/{repo}/contents/{wf_path}",
            headers=headers,
            json=put_body,
            timeout=15,
        ).raise_for_status()

        owner = repo.split("/", 1)[0]
        prs_resp = httpx.get(
            f"{self.base_url}/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}"},
            headers=headers,
            timeout=15,
        )
        if prs_resp.status_code == 200 and prs_resp.json():
            pr_obj = prs_resp.json()[0]
            return {"number": pr_obj.get("number"), "html_url": pr_obj.get("html_url")}

        pr_create = httpx.post(
            f"{self.base_url}/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": "chore(atobar): update flow-metrics.yml policy",
                "head": branch,
                "base": default_branch,
                "body": "Automated metric policy update from atobar-flow Control Panel.",
            },
            timeout=15,
        )
        pr_create.raise_for_status()
        pr_data = pr_create.json()
        return {"number": pr_data.get("number"), "html_url": pr_data.get("html_url")}


def get_ec2_client() -> Any:
    import boto3  # type: ignore[import-untyped]

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    return boto3.client("ec2", region_name=region)


@lru_cache(maxsize=4)
def _cached_github_app_client(app_id: str, inst_id: str, priv_key: str) -> FlowEngineGitHubAppClient:
    return FlowEngineGitHubAppClient(app_id=app_id, installation_id=inst_id, private_key=priv_key)


def get_github_client() -> Any:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    inst_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    priv_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if app_id and priv_key:
        return _cached_github_app_client(app_id, inst_id, priv_key)
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token:
        return RealGitHubClient(token=gh_token)
    return None


def _resolve_tenant(store: Any, identifier: str, request: Optional[Request] = None) -> Tenant:
    if not identifier:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant identifier required")
    tenant: Optional[Tenant] = None
    if "/" in identifier:
        if hasattr(store, "tenant_by_repo"):
            tenant = store.tenant_by_repo(identifier)
    elif "__" in identifier:
        if hasattr(store, "tenant_by_repo"):
            tenant = store.tenant_by_repo(identifier.replace("__", "/", 1))
        if tenant is None and hasattr(store, "tenant_by_id"):
            tenant = store.tenant_by_id(identifier)
    else:
        if hasattr(store, "tenant_by_id"):
            tenant = store.tenant_by_id(identifier)
        if tenant is None and hasattr(store, "tenant_by_repo"):
            tenant = store.tenant_by_repo(identifier)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Tenant not found: {identifier}")
    if request is not None:
        scoped_tid = (request.headers.get("X-Tenant-ID") or "").strip()
        scoped_repo = (request.headers.get("X-Tenant-Repo") or "").strip()
        if scoped_tid and scoped_tid != tenant.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant request rejected")
        if scoped_repo and scoped_repo != tenant.repo:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant request rejected")
    return tenant


def _resolve_run_tenant(
    store: Any,
    run_id: str,
    expected_tenant_id: Optional[str] = None,
    request: Optional[Request] = None,
) -> tuple[Tenant, dict[str, Any]]:
    tenant, cred, sess = _resolve_run_context(store, run_id, expected_tenant_id, request=request)
    return tenant, cred or sess or {"run_id": run_id, "tenant_id": tenant.id}


def _resolve_run_context(
    store: Any,
    run_id: str,
    expected_tenant_id: Optional[str] = None,
    request: Optional[Request] = None,
) -> tuple[Tenant, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Resolve (tenant, run_credential, session) for a run with as few store reads as possible.

    The store sits across a WAN link from the Control Panel, so every read here adds
    latency to every agent heartbeat/event POST. The session row is only a fallback
    for runs whose credential row is gone or carries no tenant.
    """
    cred = store.get_run_credential(run_id) if hasattr(store, "get_run_credential") else None
    sess: Optional[dict[str, Any]] = None
    if (cred is None or not cred.get("tenant_id")) and hasattr(store, "get_session_by_run"):
        sess = store.get_session_by_run(run_id)
    if cred is None and sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found")

    if cred is not None:
        exp_str = cred.get("expires_at")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt <= datetime.now(timezone.utc):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Run credential has expired")
            except HTTPException:
                raise
            except ValueError:
                pass
        if request is not None:
            raw_run_cred = (request.headers.get("X-Run-Credential") or "").strip()
            expected_hash = str(cred.get("cred_hash") or "")
            if raw_run_cred and expected_hash:
                provided_hash = hashlib.sha256(raw_run_cred.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(provided_hash.encode("utf-8"), expected_hash.encode("utf-8")):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid run credential")

    tenant_id = (str(cred["tenant_id"]) if cred and cred.get("tenant_id") else None) or (
        str(sess["tenant_id"]) if sess and sess.get("tenant_id") else None
    )
    if not tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found")
    tenant = _resolve_tenant(store, tenant_id, request=request)
    if expected_tenant_id and expected_tenant_id not in (tenant.id, tenant.repo):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Run does not belong to specified tenant")
    return tenant, cred, sess


def _default_branch(tenant: Tenant) -> str:
    return str((tenant.manifest or {}).get("default_branch") or "main")


def _tenant_spot_max_minutes(store: Any, tenant: Tenant) -> int:
    if hasattr(store, "get_spot_max_minutes"):
        try:
            return int(store.get_spot_max_minutes(tenant.id))
        except Exception:
            pass
    if isinstance(tenant.manifest, dict) and tenant.manifest.get("spot_max_instance_minutes"):
        return int(tenant.manifest["spot_max_instance_minutes"])
    return 60


class _FlowEngineGitHubAdapter:
    """Adapts GitHub client + EC2 Spot launcher to Conductor's GitHubClient protocol."""

    def __init__(
        self,
        gh: Any,
        store: Any,
        tenant: Tenant,
        ec2_client: Any,
        pr_number: int = 0,
        head_sha: str = "",
        exclude_comment_id: Optional[int] = None,
    ) -> None:
        self._gh = gh
        self._store = store
        self._tenant = tenant
        self._ec2_client = ec2_client
        self._pr_number = pr_number
        self._head_sha = head_sha if (head_sha and head_sha != "0" * 40) else ""
        self._exclude_comment_id = exclude_comment_id
        self.dispatched_spots: list[dict[str, Any]] = []

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        if self._gh is not None and hasattr(self._gh, "get_pr"):
            pr_data = dict(self._gh.get_pr(repo, pr_number))
            if self._head_sha and not (pr_data.get("head") or {}).get("sha"):
                pr_data["head"] = {"sha": self._head_sha}
            return pr_data
        return {"number": pr_number, "head": {"sha": self._head_sha}, "labels": []}

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        if self._gh is not None and hasattr(self._gh, "get_pr_files"):
            return list(self._gh.get_pr_files(repo, pr_number))
        return []

    def get_commit_sha(self, repo: str, ref: str) -> str:
        if self._gh is not None and hasattr(self._gh, "get_commit_sha"):
            return str(self._gh.get_commit_sha(repo, ref))
        return self._head_sha

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        if self._gh is not None and hasattr(self._gh, "get_comments"):
            comments = list(self._gh.get_comments(repo, pr_number))
        turn_comments = getattr(self._store, "_turn_comments", {})
        if isinstance(turn_comments, dict):
            extra = list(turn_comments.get((repo, pr_number), []))
            seen_ids = {c.get("id") for c in comments if c.get("id") is not None}
            for c in extra:
                if c.get("id") is None or c.get("id") not in seen_ids:
                    comments.append(c)
        return comments

    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_comment"):
            try:
                found = self._gh.get_comment(repo, pr_number, comment_id)
                if isinstance(found, dict):
                    return found
            except Exception:
                pass
        return next(
            (c for c in self.get_comments(repo, pr_number) if c.get("id") is not None and str(c.get("id")) == str(comment_id)),
            None,
        )

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_check_runs"):
            return list(self._gh.get_check_runs(repo, ref))
        return []

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        if self._gh is not None and hasattr(self._gh, "create_check_run"):
            self._gh.create_check_run(repo, name, head_sha, conclusion, output)

    def create_comment(self, repo: str, issue_number: int, body: str) -> Any:
        if self._gh is not None and hasattr(self._gh, "create_comment"):
            return self._gh.create_comment(repo, issue_number, body)
        return None

    def update_comment(self, repo: str, comment_id: int, body: str) -> Any:
        if self._gh is not None and hasattr(self._gh, "update_comment"):
            return self._gh.update_comment(repo, comment_id, body)
        return None

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        if self._gh is not None and hasattr(self._gh, "get_issue"):
            return dict(self._gh.get_issue(repo, issue_number))
        return self.get_pr(repo, issue_number)

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if self._gh is None or not hasattr(self._gh, "create_issue"):
            raise RuntimeError("GitHub client with create_issue is required to open goal issues")
        return dict(self._gh.create_issue(repo, title=title, body=body, labels=labels))

    def list_open_issues(self, repo: str) -> list[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "list_open_issues"):
            return list(self._gh.list_open_issues(repo))
        return []

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> Any:
        return self.create_comment(repo, pr_number, body)

    def add_comment_reaction(
        self, repo: str, comment_id: int, content: str = "eyes", *, is_review_comment: bool = False
    ) -> Any:
        if self._gh is not None and hasattr(self._gh, "add_comment_reaction"):
            return self._gh.add_comment_reaction(
                repo, comment_id, content, is_review_comment=is_review_comment
            )
        return None

    def get_pr_review(self, repo: str, pr_number: int, review_id: int) -> Optional[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_pr_review"):
            res = self._gh.get_pr_review(repo, pr_number, review_id)
            return dict(res) if isinstance(res, dict) else None
        return None

    def get_pr_review_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_pr_review_comment"):
            res = self._gh.get_pr_review_comment(repo, pr_number, comment_id)
            return dict(res) if isinstance(res, dict) else None
        return None

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_pr_reviews"):
            return list(self._gh.get_pr_reviews(repo, pr_number))
        return []

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "get_pr_review_comments"):
            return list(self._gh.get_pr_review_comments(repo, pr_number))
        return []

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        if self._gh is not None and hasattr(self._gh, "list_open_prs"):
            return list(self._gh.list_open_prs(repo))
        return []

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        if self._gh is not None and hasattr(self._gh, "add_label"):
            self._gh.add_label(repo, issue_number, label)

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        if self._gh is not None and hasattr(self._gh, "remove_label"):
            self._gh.remove_label(repo, issue_number, label)

    def is_valid_assignee(self, repo: str, login: str) -> bool:
        if self._gh is not None and hasattr(self._gh, "is_valid_assignee"):
            return bool(self._gh.is_valid_assignee(repo, login))
        return True

    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        if self._gh is not None and hasattr(self._gh, "add_assignees"):
            self._gh.add_assignees(repo, issue_number, assignees)

    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        if self._gh is not None and hasattr(self._gh, "remove_assignees"):
            self._gh.remove_assignees(repo, issue_number, assignees)

    def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        if self._gh is not None and hasattr(self._gh, "request_reviewers"):
            self._gh.request_reviewers(repo, pr_number, reviewers)

    def list_collaborators(self, repo: str) -> list[str]:
        if self._gh is not None and hasattr(self._gh, "list_collaborators"):
            return list(self._gh.list_collaborators(repo))
        owner = repo.split("/", 1)[0] if "/" in repo else ""
        return [owner] if owner else []

    def find_pr_for_sha(self, repo: str, head_sha: str) -> Optional[int]:
        if self._gh is not None and hasattr(self._gh, "find_pr_for_sha"):
            res = self._gh.find_pr_for_sha(repo, head_sha)
            return int(res) if res else None
        return self._pr_number or None

    def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None:
        max_mins = _tenant_spot_max_minutes(self._store, self._tenant)
        spot_payload = {
            **payload,
            "target_repo": repo,
            "spot_max_instance_minutes": max_mins,
            "target_ref": payload.get("target_ref") or _default_branch(self._tenant),
        }
        spot_res = launch_spot_instance(spot_payload, ec2_client=self._ec2_client)
        self.dispatched_spots.append(spot_res)
        run_id = str(payload.get("run_id") or "")
        role = str(payload.get("role") or "developer")
        issue_num = int(payload.get("pr") or payload.get("issue_id") or self._pr_number or 0)
        if run_id and hasattr(self._store, "upsert_session"):
            self._store.upsert_session(
                {
                    "instance_id": f"pending-{run_id}",
                    "run_id": run_id,
                    "tenant_id": self._tenant.id,
                    "role": role,
                    "target_repo": repo,
                    "issue_id": issue_num,
                    "status": "pending",
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                }
            )


@app.get("/healthz")
@app.get("/internal/healthz")
def internal_healthz() -> dict[str, str]:
    return {"status": "ok"}


internal = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_internal_token)])
_ISSUE_LOCKS_GUARD = threading.Lock()
_ISSUE_LOCKS: dict[tuple[str, int], threading.Lock] = {}


def _lock_for_issue(tenant_id: str, pr_number: int) -> threading.Lock:
    key = (str(tenant_id), int(pr_number))
    with _ISSUE_LOCKS_GUARD:
        lock = _ISSUE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ISSUE_LOCKS[key] = lock
        return lock


@internal.post("/events")
def handle_internal_event(
    body: InternalEventRequest,
    request: Request,
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    raw_outer = dict(body.event or {})
    repo_or_id = (
        body.tenant
        or body.tenant_id
        or str(raw_outer.get("target_repo") or raw_outer.get("tenant") or raw_outer.get("repo") or "")
    )
    tenant = _resolve_tenant(store, repo_or_id, request=request)
    request.state.tenant = tenant.repo

    if tenant.status not in ("active", "onboarding"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"tenant is {tenant.status}")

    inner_event = dict(raw_outer["event"]) if isinstance(raw_outer.get("event"), dict) else raw_outer
    kind = str(inner_event.get("kind") or body.event_type or raw_outer.get("event_type") or "")
    explicit_dedup_id = body.delivery_id or body.event_id
    dedup_id = explicit_dedup_id or compute_event_id(inner_event)

    if (explicit_dedup_id is not None or kind not in ("issues.labeled", "issues.edited")) and store.has_processed_event(
        tenant.id, dedup_id
    ):
        return {"status": "duplicate", "dispatched": False, "event_id": dedup_id}

    comment_id = inner_event.get("comment_id")
    issue_or_pr = inner_event.get("pr_number") or inner_event.get("issue_number") or inner_event.get("issue_id")
    comment_dedup_key = f"comment:{issue_or_pr}:{comment_id}" if (issue_or_pr and comment_id is not None) else None
    if comment_dedup_key is not None and store.has_processed_event(tenant.id, comment_dedup_key):
        return {"status": "duplicate", "dispatched": False, "event_id": dedup_id}

    max_mins = _tenant_spot_max_minutes(store, tenant)
    label = str(inner_event.get("label") or "")

    # 1. Onboarding trigger (flow:onboard label or flow_onboard event)
    if kind == "flow_onboard" or (kind in ("issues.labeled", "issues.edited") and label == "flow:onboard"):
        issue_num = int(inner_event.get("issue_number") or inner_event.get("issue_id") or 0)
        run_id = str(inner_event.get("run_id") or "")
        run_cred = str(inner_event.get("run_credential") or "")
        if not run_id or not run_cred:
            run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
            store.create_run_credential(run_id, tenant.id, "onboarder", cred_hash, expires_at)
        elif store.get_run_credential(run_id) is None:
            cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=max(max_mins, 60))).isoformat()
            store.create_run_credential(
                run_id,
                tenant.id,
                "onboarder",
                cred_hash,
                expires_at,
            )
        request.state.run_id = run_id
        store.claim_wiki_refresh(tenant.id, tenant.repo, run_id)
        spot_res = launch_spot_instance(
            {
                "role": "onboarder",
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "pr": issue_num,
                "label": "flow:onboard",
                "run_id": run_id,
                "run_credential": run_cred,
                "spot_max_instance_minutes": max_mins,
                "target_ref": _default_branch(tenant),
            },
            ec2_client=ec2_client,
        )
        store.upsert_session(
            {
                "instance_id": f"pending-{run_id}",
                "run_id": run_id,
                "tenant_id": tenant.id,
                "role": "onboarder",
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "status": "pending",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        store.record_processed_event(
            tenant.id,
            dedup_id,
            payload=inner_event,
            kind="flow:onboard",
            sender=inner_event.get("sender"),
        )
        return {
            "status": "dispatched",
            "dispatched": True,
            "role": "onboarder",
            "run_id": run_id,
            "instance_id": spot_res.get("instance_id", ""),
        }

    # 2. Direct flow_run Spot dispatch
    if kind == "flow_run" or ("role" in inner_event and not inner_event.get("kind")):
        role = str(inner_event.get("role") or "developer")
        issue_num = int(inner_event.get("issue_id") or inner_event.get("pr") or inner_event.get("issue_number") or 0)
        head_sha = str(inner_event.get("head_sha") or "")
        run_id = str(inner_event.get("run_id") or "")
        run_cred = str(inner_event.get("run_credential") or "")
        if not run_id or not run_cred:
            run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
            store.create_run_credential(run_id, tenant.id, role, cred_hash, expires_at, head_sha=head_sha or None)
        elif store.get_run_credential(run_id) is None:
            cred_hash = hashlib.sha256(run_cred.encode("utf-8")).hexdigest()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=max(max_mins, 60))).isoformat()
            store.create_run_credential(
                run_id,
                tenant.id,
                role,
                cred_hash,
                expires_at,
                head_sha=head_sha or None,
            )
        request.state.run_id = run_id
        agent_runtime = str(
            inner_event.get("agent_runtime")
            or ((tenant.manifest or {}).get("loop") or {}).get("agent_runtime")
            or "nanobot"
        )
        spot_res = launch_spot_instance(
            {
                "role": role,
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "pr": issue_num,
                "head_sha": head_sha,
                "label": label,
                "run_id": run_id,
                "run_credential": run_cred,
                "agent_runtime": agent_runtime,
                "spot_max_instance_minutes": max_mins,
                "target_ref": _default_branch(tenant),
            },
            ec2_client=ec2_client,
        )
        store.upsert_session(
            {
                "instance_id": f"pending-{run_id}",
                "run_id": run_id,
                "tenant_id": tenant.id,
                "role": role,
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "status": "pending",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        store.record_processed_event(
            tenant.id,
            dedup_id,
            payload=inner_event,
            kind="flow_run",
            sender=inner_event.get("sender"),
        )
        return {
            "status": "dispatched",
            "dispatched": True,
            "role": role,
            "run_id": run_id,
            "instance_id": spot_res.get("instance_id", ""),
        }

    # 3. Issue label / edit event
    if kind in ("issues.labeled", "issues.edited"):
        issue_num = int(inner_event.get("issue_number") or inner_event.get("issue_id") or 0)
        if kind == "issues.labeled" and label in BLOCKED_TRIGGER_LABELS:
            return {"status": "ignored", "dispatched": False, "reason": f"label {label} cannot spawn a dispatch"}
        if not label.startswith("flow:"):
            return {"status": "ignored", "dispatched": False, "reason": "label is not flow:*"}
        if label in ("flow:onboard", "flow:metrics"):
            return {"status": "ignored", "dispatched": False, "reason": f"{label} is not an agent run trigger"}
        if tenant.status != "active":
            if label == "flow:approved":
                return {"status": "ignored", "dispatched": False, "reason": f"{label} is not an agent run trigger"}
            if gh is not None and issue_num > 0:
                if hasattr(gh, "remove_label"):
                    try:
                        gh.remove_label(tenant.repo, issue_num, label)
                    except Exception as exc:
                        logger.warning("Failed to remove label %s on %s#%s: %s", label, tenant.repo, issue_num, exc)
                from atobar_contracts.client_templates import resolve_cp_url

                cp_url = resolve_cp_url()
                hold_body = render_onboarding_blocked_comment(tenant.repo, label, cp_url=cp_url)
                upsert_issue_comment_by_marker(gh, tenant.repo, issue_num, ONBOARDING_HOLD_MARKER, hold_body)
            return {
                "status": "ignored",
                "dispatched": False,
                "reason": "tenant is still onboarding; agent runs start once it is active",
            }
        if label == "flow:approved" and not is_issue_awaiting_plan_approval(
            tenant.repo, issue_num, inner_event, gh, store
        ):
            return {"status": "ignored", "dispatched": False, "reason": f"{label} is not an agent run trigger"}
        if store.issue_session_active(tenant.id, issue_num):
            if gh is not None and hasattr(gh, "create_comment"):
                try:
                    gh.create_comment(
                        tenant.repo,
                        issue_num,
                        f"🌊 **flow:** `{label}` ignored — a run is already active on this issue. "
                        f"Wait for it to finish; a run whose heartbeat is silent for "
                        f"{SESSION_STALE_AFTER_MINUTES} minutes no longer blocks a new trigger.",
                    )
                except Exception:
                    pass
            return {"status": "ignored", "dispatched": False, "reason": "a run is already active on this issue"}

        human_login = resolve_human_assignee(tenant.manifest, tenant.repo)
        stale_labels = (
            "flow:escalated",
            "flow:in-review",
            "flow:in-progress",
            "flow:needs-approval",
            "flow:resume",
            "flow:approved",
            *(("flow:plan",) if label == "flow:approved" else ()),
        )
        if gh is not None:
            for stale in stale_labels:
                if hasattr(gh, "remove_label"):
                    try:
                        gh.remove_label(tenant.repo, issue_num, stale)
                    except Exception:
                        pass
            if label in ("flow:resume", "flow:approved") and human_login and hasattr(gh, "remove_assignees"):
                try:
                    gh.remove_assignees(tenant.repo, issue_num, [human_login])
                except Exception as exc:
                    logger.warning("Failed to unassign %s on %s#%s: %s", human_login, tenant.repo, issue_num, exc)
            if hasattr(gh, "create_comment"):
                try:
                    epoch_id = f"epoch-{uuid.uuid4().hex}"
                    store.create_run_credential(epoch_id, tenant.id, "epoch", epoch_id, "2099-01-01T00:00:00+00:00")
                    gh.create_comment(
                        tenant.repo,
                        issue_num,
                        f"{LOOP_EPOCH_MARKER} <!-- flow:epoch-id:{epoch_id} -->\n"
                        f"🌊 **flow:** `{label}` applied — starting a fresh loop "
                        "(previous rounds and escalations on this issue are reset).",
                    )
                except Exception:
                    pass

        if label in ("flow:resume", "flow:approved"):
            initial_role = "developer"
        elif label == "flow:plan":
            initial_role = "planner"
        else:
            issue_text = inner_event.get("issue_body")
            if issue_text is None and gh is not None and hasattr(gh, "get_issue_body"):
                try:
                    issue_text = gh.get_issue_body(tenant.repo, issue_num)
                except Exception:
                    issue_text = None
            initial_role = assess_issue_sufficiency(issue_text, label).route if issue_text is not None else "developer"

        run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
        store.create_run_credential(run_id, tenant.id, initial_role, cred_hash, expires_at)
        request.state.run_id = run_id
        agent_runtime = str(((tenant.manifest or {}).get("loop") or {}).get("agent_runtime") or "nanobot")

        spot_res = launch_spot_instance(
            {
                "role": initial_role,
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "pr": issue_num,
                "label": label,
                "run_id": run_id,
                "run_credential": run_cred,
                "agent_runtime": agent_runtime,
                "spot_max_instance_minutes": max_mins,
                "target_ref": _default_branch(tenant),
            },
            ec2_client=ec2_client,
        )
        store.upsert_session(
            {
                "instance_id": f"pending-{run_id}",
                "run_id": run_id,
                "tenant_id": tenant.id,
                "role": initial_role,
                "target_repo": tenant.repo,
                "issue_id": issue_num,
                "status": "pending",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if explicit_dedup_id is not None:
            store.record_processed_event(
                tenant.id,
                dedup_id,
                payload=inner_event,
                kind=kind,
                sender=inner_event.get("sender"),
            )
        return {
            "status": "dispatched",
            "dispatched": True,
            "role": initial_role,
            "run_id": run_id,
            "instance_id": spot_res.get("instance_id", ""),
        }

    # 3b. PR merged event -> mark delegated issue as flow:shipped and clear review/assignees
    if kind == "pull_request.closed" and bool(inner_event.get("merged")):
        merged_res = handle_merged_pr_event(tenant.repo, inner_event, gh, store)
        if merged_res is not None:
            return merged_res
        return {"status": "ignored", "dispatched": False, "reason": "merged PR is not a tracked factory PR"}

    # 4. Conductor flow_event (PR, issue_comment, check_run)
    if tenant.status != "active":
        return {
            "status": "ignored",
            "dispatched": False,
            "reason": "tenant is still onboarding; events are only forwarded for active tenants",
        }

    bots = get_bot_logins()
    pr_num = int(inner_event.get("pr_number") or inner_event.get("issue_number") or 0)
    head_sha = str(inner_event.get("head_sha") or "")
    if not head_sha and kind == "issue_comment.created":
        cid = inner_event.get("comment_id")
        if cid is not None and gh is not None and hasattr(gh, "get_comments") and pr_num > 0:
            try:
                fetched = next(
                    (c for c in gh.get_comments(tenant.repo, pr_num) if c.get("id") is not None and str(c.get("id")) == str(cid)),
                    None,
                )
            except Exception:
                fetched = None
            if fetched is not None:
                parsed_env = parse(
                    str(fetched.get("body") or ""),
                    author_login=str((fetched.get("user") or {}).get("login") or "atobar-flow-agent[bot]"),
                    bots=bots,
                )
                if parsed_env is not None and getattr(parsed_env, "head_sha", None):
                    head_sha = str(getattr(parsed_env, "head_sha"))

    gh_adapter = _FlowEngineGitHubAdapter(
        gh=gh,
        store=store,
        tenant=tenant,
        ec2_client=ec2_client,
        pr_number=pr_num,
        head_sha=head_sha,
        exclude_comment_id=inner_event.get("comment_id"),
    )
    resolved_issue = resolve_delegated_issue_number(tenant.repo, inner_event, gh_adapter, store)
    if resolved_issue is not None and inner_event.get("delegated_issue_number") is None:
        inner_event = {**inner_event, "delegated_issue_number": resolved_issue}
    lock_issue_num = resolved_issue or pr_num
    with _lock_for_issue(tenant.id, lock_issue_num):
        res = process_flow_event(
            {"tenant": tenant.repo, "event_id": dedup_id, "event": inner_event},
            gh=gh_adapter,
            store=store,
            bot_logins=bots,
        )
        if res.get("status") == "skipped":
            return {"status": "duplicate", "dispatched": False, "event_id": dedup_id}
        if not store.has_processed_event(tenant.id, dedup_id):
            store.record_processed_event(
                tenant.id,
                dedup_id,
                payload=inner_event,
                kind=kind,
                sender=inner_event.get("sender"),
            )
    return {**res, "dispatched": True}


def _parse_iso_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str) and val:
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _build_manifest_for_goal(tenant: Tenant, manifest_dict: dict[str, Any], store: Any) -> Optional[TenantManifest]:
    raw_goal = manifest_dict.get("goal")
    if not isinstance(raw_goal, dict):
        return None
    metrics_list = list(manifest_dict.get("metrics") or [])
    if not metrics_list and hasattr(store, "metric_definitions"):
        for d in store.metric_definitions(tenant.id):
            if d.get("status", "active") == "active" and d.get("key") and d.get("comparator") is not None and d.get("threshold") is not None:
                metrics_list.append(
                    {
                        "key": d["key"],
                        "comparator": d["comparator"],
                        "threshold": float(d["threshold"]),
                        "baseline_mode": d.get("baseline_mode") or "absolute",
                        "min_samples": int(d.get("min_samples") or 1),
                    }
                )
    candidate = {
        "repo": tenant.repo,
        "default_branch": manifest_dict.get("default_branch") or "main",
        "owner_github_login": manifest_dict.get("owner_github_login") or tenant.repo.split("/", 1)[0],
        "metrics": metrics_list,
        "goal": raw_goal,
    }
    try:
        return TenantManifest.model_validate(candidate)
    except Exception as exc:
        logger.warning("goal.manifest_invalid for tenant %s: %s", tenant.repo, exc)
        try:
            store.record_run_event(None, "goal.manifest_invalid", {"tenant": tenant.repo, "error": str(exc)})
        except Exception:
            pass
        return None


def _evaluate_and_execute_goal_step(
    *,
    tenant: Tenant,
    manifest_dict: dict[str, Any],
    goal_row: dict[str, Any],
    verified_values: dict[str, float],
    store: Any,
    gh: Any,
    ec2_client: Any,
) -> Optional[dict[str, Any]]:
    manifest_obj = _build_manifest_for_goal(tenant, manifest_dict, store)
    if manifest_obj is None or manifest_obj.goal is None:
        return None

    started_dt = _parse_iso_dt(goal_row.get("started_at"))
    goal_state = GoalState(
        goal_sha=str(goal_row.get("goal_sha") or manifest_obj.goal.sha256()),
        status="open",
        stop_reason=goal_row.get("stop_reason"),
        spent_usd=float(goal_row.get("spent_usd") or 0.0),
        prs_merged=int(goal_row.get("prs_merged") or 0),
        merges_without_progress=int(goal_row.get("merges_without_progress") or 0),
        started_at=started_dt,
        last_progress_at=_parse_iso_dt(goal_row.get("last_progress_at")) if goal_row.get("last_progress_at") else started_dt,
    )

    latest_default_obs: dict[str, float] = {}
    if hasattr(store, "default_branch_observations"):
        for r in store.default_branch_observations(tenant.id, limit=100):
            if r.get("key") and r.get("value") is not None:
                latest_default_obs[str(r["key"])] = float(r["value"])
    elif hasattr(store, "recent_observations"):
        for r in store.recent_observations(tenant.id, limit=100):
            if r.get("pr_number") is None and r.get("key") and r.get("value") is not None:
                latest_default_obs[str(r["key"])] = float(r["value"])
    latest_default_obs.update(verified_values)

    tracking_issue = manifest_obj.goal.tracking_issue_number
    gh_adapter = _FlowEngineGitHubAdapter(
        gh=gh,
        store=store,
        tenant=tenant,
        ec2_client=ec2_client,
        pr_number=int(tracking_issue or 0),
    )

    open_issues_raw = gh_adapter.list_open_issues(tenant.repo)
    open_planned = 0
    for item in open_issues_raw:
        if not isinstance(item, dict):
            continue
        if tracking_issue and int(item.get("number") or 0) == int(tracking_issue):
            continue
        if is_goal_planned_issue(item, goal_sha=goal_state.goal_sha):
            open_planned += 1

    action = goal_step(
        manifest=manifest_obj,
        goal_state=goal_state,
        latest_default_obs=latest_default_obs,
        open_planned_issues=open_planned,
        now=datetime.now(timezone.utc),
    )

    if isinstance(action, GoalMetAction):
        store.update_tenant_goal(
            tenant.id,
            {"status": "met", "last_progress_at": datetime.now(timezone.utc).isoformat()},
        )
        if tracking_issue:
            gh_adapter.add_label(tenant.repo, int(tracking_issue), "flow:shipped")
            card = render_status_card(
                "flow:shipped",
                spend=goal_state.spent_usd,
                caps={"max_goal_cost_usd": manifest_obj.goal.budget.max_goal_cost_usd},
            )
            gh_adapter.create_comment(tenant.repo, int(tracking_issue), card)
    elif isinstance(action, GoalStopAction):
        store.update_tenant_goal(
            tenant.id,
            {"status": "stopped", "stop_reason": action.reason},
        )
        if tracking_issue:
            gh_adapter.add_label(tenant.repo, int(tracking_issue), "flow:escalated")
            card = (
                render_status_card(
                    "flow:escalated",
                    spend=goal_state.spent_usd,
                    caps={"max_goal_cost_usd": manifest_obj.goal.budget.max_goal_cost_usd},
                )
                + f"\n**Goal Stopped Reason:** `{action.reason}`\n"
            )
            gh_adapter.create_comment(tenant.repo, int(tracking_issue), card)
    elif isinstance(action, GoalPlanAction):
        run_id, raw_cred, cred_hash, expires_at = generate_run_credentials()
        if not store.claim_goal_planning(tenant.id, run_id):
            return GoalWaitAction().model_dump(mode="json")
        store.create_run_credential(
            run_id,
            tenant.id,
            "planner",
            cred_hash,
            expires_at,
            goal_mode=True,
            plan_count=action.count,
        )
        gh_adapter.dispatch_flow_run(
            tenant.repo,
            {
                "role": "planner",
                "run_id": run_id,
                "run_credential": raw_cred,
                "pr": int(tracking_issue or 0),
                "issue_id": int(tracking_issue or 0),
                "goal_mode": True,
                "plan_count": action.count,
                "goal_objective": manifest_obj.goal.objective,
                "issue_text": (
                    f"Propose up to {action.count} issue(s) to advance repository goal: "
                    f"{manifest_obj.goal.objective}"
                ),
            },
        )

    return action.model_dump(mode="json")


@internal.post("/metrics", status_code=status.HTTP_201_CREATED)
def handle_internal_metrics(
    body: InternalMetricsRequest,
    request: Request,
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
    ec2_client: Any = Depends(get_ec2_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, body.tenant_id or body.tenant or "", request=request)
    request.state.tenant = tenant.repo

    if tenant.status not in ("active", "onboarding"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"tenant is {tenant.status}")

    manifest = dict(tenant.manifest or {})
    expected_wf = str(manifest.get("metrics_workflow_path") or ".github/workflows/flow-metrics.yml")

    try:
        verified_values, artifact_sha256, verified_attempt = verify_and_extract_metrics(
            repo=tenant.repo,
            commit_sha=body.commit_sha,
            run_id=body.run_id,
            run_attempt=body.run_attempt,
            workflow_path=body.workflow_path,
            expected_workflow_path=expected_wf,
            gh=gh,
            pr_number=body.pr_number,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    if not all(math.isfinite(v) for v in verified_values.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "metric values must be finite")

    if body.values:
        if not all(math.isfinite(v) for v in body.values.values()):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "metric values must be finite")
        if {str(k): float(v) for k, v in body.values.items()} != verified_values:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Reported metric values do not match verified GitHub artifact values",
            )

    active_keys = store.metric_keys(tenant.id)
    recorded_keys = sorted(k for k in verified_values if k in active_keys)
    ignored_keys = sorted(k for k in verified_values if k not in active_keys)
    manifest_sha = canonical_sha(manifest)

    rows = [
        {
            "tenant_id": tenant.id,
            "key": k,
            "value": float(verified_values[k]),
            "commit_sha": body.commit_sha,
            "pr_number": body.pr_number,
            "check_run_id": body.run_id,
            "run_id": body.run_id,
            "workflow_path": body.workflow_path,
            "run_attempt": verified_attempt,
            "artifact_sha256": artifact_sha256,
            "manifest_sha": manifest_sha,
        }
        for k in recorded_keys
    ]
    if rows:
        store.record_observations(rows)

    activated = False
    if tenant.status == "onboarding" and body.pr_number is None:
        if store.ready_to_activate(tenant.id):
            store.update_tenant_status(tenant.id, "active")
            activated = True
            if gh is not None and hasattr(gh, "seed_repository_labels"):
                try:
                    gh.seed_repository_labels(tenant.repo)
                except Exception as exc:
                    logger.warning("Failed to seed labels on %s during activation: %s", tenant.repo, exc)
            if gh is not None and hasattr(gh, "comment_on_open_issue_by_label"):
                try:
                    gh.comment_on_open_issue_by_label(
                        tenant.repo,
                        "flow:onboard",
                        f"Tenant `{tenant.repo}` is now **active**! All declared metrics have satisfied `min_samples`.",
                    )
                except Exception:
                    pass

    goal_action: Optional[dict[str, Any]] = None
    wiki_refresh_action: Optional[dict[str, Any]] = None
    if body.pr_number is None:
        if recorded_keys:
            wiki_row = store.record_wiki_default_branch_merge(tenant.id, tenant.repo, body.commit_sha)
            interval_merges = int(
                wiki_row["refresh_interval_merges"]
                if wiki_row.get("refresh_interval_merges") is not None
                else 5
            )
            merges_since = int(wiki_row.get("merges_since_refresh") or 0)
            if interval_merges > 0 and merges_since >= interval_merges:
                wiki_refresh_action = _dispatch_or_queue_wiki_refresh(
                    store, tenant, ec2_client, background=True
                )
        goal_row = store.get_tenant_goal(tenant.id)
        if goal_row is not None and goal_row.get("status") == "open":
            goal_action = _evaluate_and_execute_goal_step(
                tenant=tenant,
                manifest_dict=manifest,
                goal_row=goal_row,
                verified_values={k: float(verified_values[k]) for k in recorded_keys},
                store=store,
                gh=gh,
                ec2_client=ec2_client,
            )

    res_out: dict[str, Any] = {
        "recorded": recorded_keys,
        "ignored": ignored_keys,
        "activated": activated,
        "manifest_sha": manifest_sha,
        "artifact_sha256": artifact_sha256,
    }
    if goal_action is not None:
        res_out["goal_action"] = goal_action
    if wiki_refresh_action is not None:
        res_out["wiki_refresh"] = wiki_refresh_action
    return res_out


@internal.post("/runs/{run_id}/heartbeat")
def handle_run_heartbeat(
    run_id: str,
    payload: HeartbeatPayload,
    request: Request,
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
) -> dict[str, str]:
    request.state.run_id = run_id
    tenant, cred, sess = _resolve_run_context(store, run_id, payload.tenant_id, request=request)
    rec: dict[str, Any] = cred or sess or {"run_id": run_id, "tenant_id": tenant.id}
    request.state.tenant = tenant.repo

    # Reuse the credential row already read above; only the first heartbeat of a run
    # needs the claim write.
    if cred is not None and hasattr(store, "claim_run_credential_instance"):
        claimed_by = str(cred.get("claimed_instance_id") or "")
        already_ours = bool(claimed_by) and claimed_by == payload.instance_id
        if not already_ours and (
            claimed_by or not store.claim_run_credential_instance(run_id, payload.instance_id)
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Run credential already claimed by another instance")

    role = str(rec.get("role") or payload.role or "developer")
    halted_reason = payload.halt_detail if payload.halt_detail else payload.halt_reason
    bound_metric_key = str(
        rec.get("metric_key") or (run_id.split("__", 1)[1] if "__" in run_id and role == "metric_compiler" else "")
    ).strip()

    session_row: dict[str, Any] = {
        "run_id": run_id,
        "tenant_id": tenant.id,
        "role": role,
        "status": payload.status,
        "current_todo": payload.current_todo,
        "last_tool": payload.last_tool,
        "cost_estimate": payload.cost_estimate,
        "instance_id": payload.instance_id,
        "target_repo": tenant.repo,
        "issue_id": payload.issue_id,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "agent_runtime": payload.agent_runtime,
        "iterations": payload.iterations,
        "usage": payload.usage,
        "last_activity_at": payload.last_activity_at,
        "head_sha": payload.head_sha,
        "halt_reason": payload.halt_reason,
        "halt_detail": payload.halt_detail,
    }
    if bound_metric_key:
        session_row["metric_key"] = bound_metric_key
        # A compiler that halts before its first attempt heartbeat must still be findable as `compile:<key>`.
        if not session_row.get("current_todo"):
            session_row["current_todo"] = f"compile:{bound_metric_key}"
    if halted_reason:
        session_row["halted_reason"] = f"Halted: {halted_reason}"

    pending_id = f"pending-{run_id}"
    pending_sess = store.get_session_by_instance(pending_id) if hasattr(store, "get_session_by_instance") else None
    if isinstance(pending_sess, dict) and payload.instance_id != pending_id:
        if not session_row.get("metric_key") and pending_sess.get("metric_key"):
            session_row["metric_key"] = pending_sess["metric_key"]
        if not session_row.get("started_at") and pending_sess.get("started_at"):
            session_row["started_at"] = pending_sess["started_at"]
    if (
        isinstance(pending_sess, dict)
        and pending_sess.get("status") == "pending"
        and hasattr(store, "update_session")
        and payload.instance_id != pending_id
    ):
        store.update_session(pending_id, {"status": "dispatched"})
    store.upsert_session(session_row)
    if str(payload.status or "").startswith("Halted") and role == "planner":
        store.clear_goal_planning(tenant.id, run_id)
    if role == "onboarder":
        status_str = str(payload.status or "")
        if status_str.startswith("Halted"):
            _finalize_onboarder_run(
                store,
                tenant,
                run_id,
                halted_error=str(halted_reason or status_str),
                ec2_client=ec2_client,
            )
        elif status_str.lower() in ("complete", "completed"):
            _finalize_onboarder_run(
                store,
                tenant,
                run_id,
                halted_error=None,
                ec2_client=ec2_client,
            )

    if (
        str(payload.status or "").startswith("Halted")
        and payload.issue_id
        and int(payload.issue_id) > 0
        and role in ("planner", "developer", "evaluator", "arbitrator", "conductor")
        and hasattr(store, "loop_rounds_for_pr")
        and hasattr(store, "record_loop_round")
    ):
        pr_num = int(payload.issue_id)
        existing_rounds = store.loop_rounds_for_pr(tenant.id, pr_num)
        already_recorded = any(
            isinstance(r.get("usage_detail"), dict) and r["usage_detail"].get("run_id") == run_id
            for r in existing_rounds
        )
        raw_usage: dict[str, Any] = dict(payload.usage) if isinstance(payload.usage, dict) else {}
        tot_raw = raw_usage.get("total")
        tot: dict[str, Any] = dict(tot_raw) if isinstance(tot_raw, dict) else {}
        in_tok = int(tot.get("input_tokens") or raw_usage.get("input_tokens") or 0)
        out_tok = int(tot.get("output_tokens") or raw_usage.get("output_tokens") or 0)
        tokens_val = int(tot.get("total_tokens") or raw_usage.get("total_tokens") or (in_tok + out_tok))
        cost_val = float(tot.get("cost_usd") or raw_usage.get("cost_usd") or payload.cost_estimate or 0.0)
        if not already_recorded:
            next_round = max((int(r.get("round", 0)) for r in existing_rounds), default=-1) + 1
            raw_usage.setdefault("agent_runtime", str(payload.agent_runtime or "nanobot"))
            raw_usage.setdefault(
                "total",
                {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": tokens_val, "cost_usd": cost_val},
            )
            raw_usage.setdefault("calls", [])
            raw_usage["run_id"] = run_id
            if payload.halt_reason:
                raw_usage["halt_reason"] = payload.halt_reason
            if halted_reason:
                raw_usage["halt_detail"] = halted_reason
            raw_sha = str(payload.head_sha or "")
            valid_sha = (
                raw_sha.lower()
                if (len(raw_sha) == 40 and all(c in "0123456789abcdef" for c in raw_sha.lower()))
                else ("0" * 40)
            )
            try:
                store.record_loop_round(
                    {
                        "tenant_id": tenant.id,
                        "pr_number": pr_num,
                        "round": max(0, next_round),
                        "head_sha": valid_sha,
                        "role": role,
                        "tokens": tokens_val,
                        "cost_usd": cost_val,
                        "wall_clock_minutes": 0.01,
                        "envelope_digest": f"halted:{payload.halt_reason or 'error'}",
                        "usage_detail": raw_usage,
                    }
                )
            except Exception as exc:
                logger.warning("Failed to record halted loop_round for run %s: %s", run_id, exc)
        goal_spend_key = f"goal-spend:{run_id}"
        if cost_val > 0.0 and not store.has_processed_event(tenant.id, goal_spend_key):
            try:
                store.record_goal_spend(tenant.id, float(cost_val))
                store.record_processed_event(
                    tenant.id,
                    goal_spend_key,
                    payload={"run_id": run_id, "cost_usd": cost_val},
                    kind="goal_spend",
                    sender="heartbeat",
                )
            except Exception as exc:
                logger.warning("Failed to record halted goal spend for run %s: %s", run_id, exc)

    return {"status": "ok"}


@internal.post("/runs/{run_id}/events")
def handle_run_events(
    run_id: str,
    batch: RunEventsBatchPayload,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, _ = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo
    if len(batch.events) > 500:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Batch size exceeds 500 events limit")

    rows: list[dict[str, Any]] = []
    for evt in batch.events:
        payload_bytes = len(json.dumps(evt.payload).encode("utf-8"))
        if payload_bytes > 65536:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Event payload exceeds 64KB limit")
        row: dict[str, Any] = {
            "run_id": run_id,
            "seq": evt.seq,
            "kind": evt.kind,
            "payload": evt.payload,
        }
        if evt.ts:
            row["ts"] = evt.ts
        rows.append(row)

    store.record_run_events(run_id, rows)
    return {"status": "ok", "count": len(rows)}


@internal.post("/runs/{run_id}/turn-complete")
def handle_run_turn_complete(
    run_id: str,
    body: TurnCompletePayload,
    request: Request,
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, rec = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    bot_logins = get_bot_logins()
    role = str(rec.get("role") or "developer")
    sender = bot_logins.get(role, "atobar-flow-agent[bot]")

    goal_plan = None
    if role == "planner":
        try:
            goal_plan = parse_and_validate_goal_plan(
                body.comment_body,
                author_login=sender,
                bots=bot_logins,
                manifest=tenant.manifest,
                force_goal_mode=bool(rec.get("goal_mode")),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    sess = store.get_session_by_run(run_id) if hasattr(store, "get_session_by_run") else None
    if role == "onboarder":
        now_iso = datetime.now(timezone.utc).isoformat()
        store.update_session(f"pending-{run_id}", {"status": "Complete", "heartbeat_at": now_iso})
        if sess:
            store.upsert_session({**sess, "status": "Complete", "heartbeat_at": now_iso})
        followup = _finalize_onboarder_run(
            store,
            tenant,
            run_id,
            halted_error=None,
            ec2_client=ec2_client,
        )
        out_onb: dict[str, Any] = {"action": "shutdown", "reason": "onboarder_complete"}
        if followup is not None:
            out_onb["followup"] = followup
        return out_onb
    goal_tracking_issue = ((tenant.manifest or {}).get("goal") or {}).get("tracking_issue_number")
    issue_number = (
        body.issue_number
        or (int(sess["issue_id"]) if sess and sess.get("issue_id") else None)
        or (int(goal_tracking_issue) if goal_tracking_issue else None)
    )
    if issue_number is None:
        parsed_env = goal_plan or parse(body.comment_body, author_login=sender, bots=bot_logins)
        if parsed_env is not None:
            issue_number = parsed_env.pr
    if issue_number is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unable to determine issue_number for turn-complete")

    instance_id = body.instance_id or (
        str(sess["instance_id"]) if sess and sess.get("instance_id") else f"spot-{run_id}"
    )
    if body.usage and sess and hasattr(store, "upsert_session"):
        store.upsert_session({**sess, "usage": body.usage, "heartbeat_at": datetime.now(timezone.utc).isoformat()})

    gh_adapter = _FlowEngineGitHubAdapter(
        gh=gh,
        store=store,
        tenant=tenant,
        ec2_client=ec2_client,
        pr_number=int(issue_number),
        head_sha=body.head_sha or "",
        exclude_comment_id=body.comment_id,
    )

    webhook_event: dict[str, Any] = {
        "kind": "issue_comment.created",
        "issue_number": int(issue_number),
        "comment_body": body.comment_body,
        "comment_id": body.comment_id,
        "sender": sender,
    }
    event_id = compute_event_id(webhook_event)

    raw_event = dict(webhook_event)
    if body.trigger_label:
        raw_event["trigger_label"] = body.trigger_label
    if body.wall_clock_minutes:
        raw_event["wall_clock_minutes"] = body.wall_clock_minutes

    with _lock_for_issue(tenant.id, int(issue_number)):
        if goal_plan is None:
            parsed_turn_env = parse(body.comment_body, author_login=sender, bots=bot_logins)
            if parsed_turn_env is None:
                if sess:
                    store.upsert_session(
                        {
                            **sess,
                            "status": "Complete",
                            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                return {"action": "shutdown", "reason": "no_envelope"}
            if parsed_turn_env.run_id != run_id or parsed_turn_env.role != role:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Envelope run_id or role does not match authenticated run credential",
                )
        if body.comment_id is not None and rec:
            if not store.claim_run_credential_envelope(run_id, int(body.comment_id)):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Run {run_id} has already claimed a different envelope comment",
                )

        if goal_plan is not None:
            dedup_key = f"goal-plan:{run_id}"
            if store.has_processed_event(tenant.id, dedup_key):
                store.clear_goal_planning(tenant.id, run_id)
                return {"action": "terminate", "reason": "goal_planned", "created_issues": [], "deduped": True}
            created_issues = execute_goal_plan_proposals(
                goal_plan,
                repo=tenant.repo,
                manifest=tenant.manifest,
                gh=gh_adapter,
                fallback_tracking_issue=int(issue_number),
            )
            store.record_processed_event(
                tenant.id,
                dedup_key,
                payload={"run_id": run_id, "created_issues": created_issues},
                kind="goal_plan",
                sender=sender,
            )
            store.clear_goal_planning(tenant.id, run_id)
            raw_usage = dict(body.usage) if isinstance(body.usage, dict) else (dict(sess.get("usage")) if sess and isinstance(sess.get("usage"), dict) else {})
            tot_raw = raw_usage.get("total")
            tot = dict(tot_raw) if isinstance(tot_raw, dict) else {}
            in_tok = int(tot.get("input_tokens") or raw_usage.get("input_tokens") or 0)
            out_tok = int(tot.get("output_tokens") or raw_usage.get("output_tokens") or 0)
            tokens_val = int(tot.get("total_tokens") or raw_usage.get("total_tokens") or (in_tok + out_tok))
            cost_val = float(tot.get("cost_usd") or raw_usage.get("cost_usd") or 0.0)
            existing_rounds = store.loop_rounds_for_pr(tenant.id, int(issue_number))
            next_round = max((int(r.get("round", 0)) for r in existing_rounds), default=-1) + 1
            store.record_loop_round(
                {
                    "tenant_id": tenant.id,
                    "pr_number": int(issue_number),
                    "round": max(0, next_round),
                    "head_sha": "0" * 40,
                    "role": "planner",
                    "tokens": tokens_val,
                    "cost_usd": cost_val,
                    "wall_clock_minutes": float(body.wall_clock_minutes or 0.01),
                    "envelope_digest": hashlib.sha256(body.comment_body.encode("utf-8")).hexdigest(),
                    "usage_detail": {**raw_usage, "run_id": run_id},
                }
            )
            if cost_val > 0.0:
                store.record_goal_spend(tenant.id, cost_val)
            if sess and hasattr(store, "upsert_session"):
                store.upsert_session(
                    {
                        **sess,
                        "status": "Complete",
                        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            return {"action": "terminate", "reason": "goal_planned", "created_issues": created_issues}

        comment_entry = {
            "id": body.comment_id,
            "user": {"login": sender},
            "body": body.comment_body,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        turn_comments = getattr(store, "_turn_comments", None)
        if not isinstance(turn_comments, dict):
            turn_comments = {}
            try:
                setattr(store, "_turn_comments", turn_comments)
            except Exception:
                pass
        c_list = turn_comments.setdefault((tenant.repo, int(issue_number)), [])
        if body.comment_id is None or not any(c.get("id") == body.comment_id for c in c_list):
            c_list.append(comment_entry)

        res = process_flow_event(
            payload={
                "tenant": tenant.repo,
                "event_id": event_id,
                "event": raw_event,
            },
            gh=gh_adapter,
            store=store,
            bot_logins=bot_logins,
            claim_warm=True,
            warm_instance_id=instance_id,
            authenticated_run_id=run_id,
        )

    handover = res.get("handover")
    if isinstance(handover, dict) and handover.get("action") == "continue":
        if sess and sess.get("instance_id") != instance_id:
            store.upsert_session(
                {
                    **sess,
                    "status": "Complete",
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return handover

    reason = str(res.get("new_status") or res.get("reason") or "complete")
    if sess and hasattr(store, "upsert_session"):
        terminal_status = "Halted: escalated" if reason == "escalated" else "Complete"
        store.upsert_session(
            {
                **sess,
                "status": terminal_status,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return {"action": "terminate", "reason": reason}


@internal.post("/runs/{run_id}/arbitration-decisions")
def handle_run_arbitration_decision(
    run_id: str,
    payload: ArbitrationDecisionPayload,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, _ = _resolve_run_tenant(store, run_id, payload.tenant_id, request=request)
    request.state.tenant = tenant.repo

    manifest_sha = canonical_sha(dict(tenant.manifest or {}))
    decision_row = {
        "tenant_id": tenant.id,
        "pr_number": payload.pr_number,
        "head_sha": payload.head_sha,
        "mode": payload.mode,
        "decision": payload.decision,
        "reasons": payload.reasons or "",
        "manifest_sha": manifest_sha,
    }
    store.insert_arbitration_decision(decision_row)
    return {"status": "ok", "manifest_sha": manifest_sha}


@internal.get("/runs/{run_id}/arbitration-data")
def handle_run_arbitration_data(
    run_id: str,
    request: Request,
    pr_number: Optional[int] = None,
    commit_sha: Optional[str] = None,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, rec = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    sess = store.get_session_by_run(run_id) if hasattr(store, "get_session_by_run") else None
    target_pr = (
        pr_number
        or (int(sess["issue_id"]) if sess and sess.get("issue_id") else None)
        or (int(rec["issue_id"]) if rec.get("issue_id") else None)
    )
    target_sha = commit_sha or (str(sess["head_sha"]) if sess and sess.get("head_sha") else None)

    raw_obs: list[dict[str, Any]] = []
    seen_sigs: set[tuple[Any, ...]] = set()

    def _merge_obs(items: list[dict[str, Any]]) -> None:
        for o in items:
            sig = (
                o.get("key"),
                o.get("commit_sha"),
                o.get("pr_number") if o.get("pr_number") is not None else o.get("pr"),
                o.get("run_id") or o.get("check_run_id"),
                o.get("run_attempt"),
            )
            if sig not in seen_sigs:
                raw_obs.append(o)
                seen_sigs.add(sig)

    if hasattr(store, "observations_for_pr_or_sha") and (target_pr is not None or target_sha):
        _merge_obs(list(store.observations_for_pr_or_sha(tenant.id, pr_number=target_pr, commit_sha=target_sha)))
    _merge_obs(list(store.recent_observations(tenant.id)))
    if hasattr(store, "default_branch_observations"):
        _merge_obs(list(store.default_branch_observations(tenant.id)))

    recent: list[dict[str, Any]] = []
    for o in raw_obs:
        pr_num = o.get("pr_number") if o.get("pr_number") is not None else o.get("pr")
        recent.append(
            {
                "key": o.get("key", ""),
                "value": float(o.get("value", 0.0)),
                "workflow_path": o.get("workflow_path", ""),
                "commit_sha": o.get("commit_sha", ""),
                "pr_number": pr_num,
                "run_id": o.get("run_id") if o.get("run_id") is not None else o.get("check_run_id"),
                "run_attempt": o.get("run_attempt"),
                "observed_at": o.get("observed_at"),
                "artifact_sha256": o.get("artifact_sha256"),
                "manifest_sha": o.get("manifest_sha"),
            }
        )

    manifest = dict(tenant.manifest or {})
    manifest_sha = canonical_sha(manifest)
    return {
        "tenant_id": tenant.id,
        "arbitration_mode": tenant.arbitration_mode,
        "manifest": manifest,
        "manifest_sha": manifest_sha,
        "recent_observations": recent,
        "pr_observations": [o for o in recent if o.get("pr_number") is not None],
        "baseline_observations": [o for o in recent if o.get("pr_number") is None],
    }


@internal.get("/runs/{run_id}/wiki")
def handle_get_run_wiki(
    run_id: str,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, _ = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    wiki = store.wiki_for_tenant(tenant.id)
    if wiki is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "wiki not generated yet")
    return dict(wiki)


@internal.post("/runs/{run_id}/wiki")
def handle_post_run_wiki(
    run_id: str,
    body: dict[str, Any],
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, rec = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    role = str((rec or {}).get("role") or "")
    if role != "onboarder":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only onboarder runs can update repository wiki")

    store.upsert_wiki(tenant.id, tenant.repo, body)
    return {"ok": True, "repo_name": tenant.repo}


@internal.post("/runs/{run_id}/github-token")
def handle_run_github_token(
    run_id: str,
    request: Request,
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
) -> dict[str, str]:
    request.state.run_id = run_id
    tenant, _ = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    raw_rec = store.get_run_credential(run_id) if hasattr(store, "get_run_credential") else None
    expected_hash = str(raw_rec.get("cred_hash") or "") if isinstance(raw_rec, dict) else ""
    if not expected_hash:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Valid run credential record required to mint GitHub token")
    raw_run_cred = (request.headers.get("X-Run-Credential") or "").strip()
    if not raw_run_cred:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "X-Run-Credential header required to mint GitHub token")
    provided_hash = hashlib.sha256(raw_run_cred.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(provided_hash.encode("utf-8"), expected_hash.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid run credential")

    stored_role = str(raw_rec.get("role") or "").strip().lower() if isinstance(raw_rec, dict) else ""
    if stored_role not in ROLE_GITHUB_PERMISSIONS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Unsupported role for GitHub token minting: {stored_role}")

    if not _GITHUB_TOKEN_MINT_LIMITER.try_acquire(run_id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded: at most 1 github-token mint per 60 seconds per run",
        )

    if gh is not None and hasattr(gh, "mint_repo_token"):
        try:
            minted = gh.mint_repo_token(tenant.repo, stored_role)
        except PermissionError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        res: dict[str, str] = {"token": str(minted["token"]), "expires_at": str(minted.get("expires_at") or "")}
        if hasattr(gh, "mint_engine_read_token"):
            try:
                eng_tok = gh.mint_engine_read_token()
                if eng_tok:
                    res["engine_token"] = str(eng_tok)
            except Exception as exc:
                logger.warning("Failed to mint read-only engine token: %s", exc)
        return res
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App token minter unavailable")


@internal.post("/runs/{run_id}/credential-used")
def handle_run_credential_used(
    run_id: str,
    request: Request,
    body: Optional[dict[str, Any]] = Body(default=None),
    store: Any = Depends(get_store),
) -> dict[str, str]:
    request.state.run_id = run_id
    tenant, _ = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    inst_id = (
        str((body or {}).get("instance_id") or "").strip()
        or (request.headers.get("X-Instance-ID") or "").strip()
    )
    if inst_id and hasattr(store, "claim_run_credential_instance") and store.get_run_credential(run_id) is not None:
        if not store.claim_run_credential_instance(run_id, inst_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Run credential already claimed by another instance")

    if hasattr(store, "mark_run_credential_used"):
        store.mark_run_credential_used(run_id)
    return {"status": "ok"}


@internal.post("/gc/sweep")
def handle_internal_gc_sweep(
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
    ec2_client: Any = Depends(get_ec2_client),
) -> dict[str, Any]:
    from flow_engine.janitor import sweep_instances

    decisions = sweep_instances(ec2=ec2_client, store=store, gh=gh, dry_run=False)
    reaped_ids: list[str] = []
    for dec in decisions:
        inst_id = dec.instance_id
        if dec.dry_run or not inst_id:
            continue
        if (
            dec.rule == "stale_heartbeat"
            and dec.tenant
            and dec.pr > 0
            and gh is not None
            and hasattr(gh, "create_comment")
        ):
            try:
                gh.create_comment(
                    dec.tenant,
                    dec.pr,
                    f"🌊 **flow Event Bus:** session `{inst_id}` terminated by GC sweep (stale heartbeat).",
                )
            except Exception:
                pass
        reaped_ids.append(inst_id)
    return {"reaped_sessions": reaped_ids, "count": len(reaped_ids)}


@internal.post("/runs/{run_id}/metric-compilation")
def handle_run_metric_compilation(
    run_id: str,
    payload: MetricCompilationPayload,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    request.state.run_id = run_id
    tenant, rec = _resolve_run_tenant(store, run_id, request=request)
    request.state.tenant = tenant.repo

    role = str(rec.get("role") or "")
    if role and role != "metric_compiler":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only metric_compiler runs can submit metric compilations")

    sess = store.get_session_by_run(run_id) if hasattr(store, "get_session_by_run") else None
    bound_key = (
        store.bound_metric_key_for_run(run_id)
        if hasattr(store, "bound_metric_key_for_run")
        else str(
            rec.get("metric_key")
            or (sess or {}).get("metric_key")
            or (run_id.split("__", 1)[1] if "__" in run_id else "")
        ).strip()
    )
    if bound_key and bound_key != payload.key:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Compiler run is bound to metric '{bound_key}', not '{payload.key}'",
        )

    existing = store.get_metric_definition(tenant.id, payload.key) or {}
    if existing.get("status") == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Metric '{payload.key}' is already active; create a draft before recompiling",
        )

    if not math.isfinite(payload.value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Compiled metric dry-run value must be finite")

    raw_src = payload.compiled.get("source")
    run_cmd = str(
        payload.compiled.get("run") or (raw_src.get("run") if isinstance(raw_src, dict) else "") or ""
    ).strip()

    protected_paths = (tenant.manifest or {}).get("protected_paths")
    problems = validate_compiled_metric_command(run_cmd, protected_paths=protected_paths)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(problems))

    raw_thr = payload.compiled.get("threshold")
    raw_comp = payload.compiled.get("comparator")
    if raw_thr is None or not raw_comp:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Compiled metric must include non-null comparator and threshold",
        )

    try:
        thr_val = float(raw_thr)
        if not math.isfinite(thr_val):
            raise ValueError("threshold must be finite")
        min_samples_val = int(payload.compiled.get("min_samples") or 1)
        compiled_metric_dict: dict[str, Any] = {
            "key": payload.key,
            "run": run_cmd,
            "comparator": str(raw_comp),
            "threshold": thr_val,
            "baseline_mode": str(payload.compiled.get("baseline_mode") or "absolute"),
            "min_samples": min_samples_val,
        }
        if payload.compiled.get("tolerance_pct") is not None:
            tol_val = float(payload.compiled["tolerance_pct"])
            if not math.isfinite(tol_val):
                raise ValueError("tolerance_pct must be finite")
            compiled_metric_dict["tolerance_pct"] = tol_val
        Metric.model_validate(compiled_metric_dict)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid compiled metric spec: {exc}")

    digest = canonical_sha(compiled_metric_dict)
    dry_run = {
        "value": float(payload.value),
        "sha": payload.sha,
        "stdout_tail": payload.stdout_tail,
    }
    upsert_row: dict[str, Any] = {
        **existing,
        "key": payload.key,
        "status": "compiled",
        "source": {"kind": "command", "run": run_cmd},
        "comparator": compiled_metric_dict["comparator"],
        "threshold": compiled_metric_dict["threshold"],
        "baseline_mode": compiled_metric_dict["baseline_mode"],
        "min_samples": compiled_metric_dict["min_samples"],
        "compiled": compiled_metric_dict,
        "digest": digest,
        "dry_run": dry_run,
    }
    upsert_row.pop("tolerance_pct", None)
    store.upsert_metric_definition(tenant.id, upsert_row)
    if hasattr(store, "update_session"):
        if sess and sess.get("instance_id") and not str(sess["instance_id"]).startswith("pending-"):
            store.update_session(f"pending-{run_id}", {"status": "dispatched"})
            store.update_session(str(sess["instance_id"]), {"status": "Complete"})
        else:
            store.update_session(f"pending-{run_id}", {"status": "Complete"})
    return {
        "status": "compiled",
        "key": payload.key,
        "digest": digest,
        "compiled": compiled_metric_dict,
        "dry_run": dry_run,
    }


@internal.post("/tenants", status_code=status.HTTP_201_CREATED)
def handle_create_tenant(
    body: CreateTenantRequest,
    request: Request,
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    if not REPO_RE.fullmatch(body.repo):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "repo must be owner/name")
    repo_owner = body.repo.split("/", 1)[0]
    actor = (body.created_by or request.headers.get("X-Actor-Login") or "").strip()
    if body.dispatch_onboard:
        if not actor:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Actor login required to dispatch onboarding")
        if gh is not None and hasattr(gh, "get_collaborator_permission"):
            perm = gh.get_collaborator_permission(body.repo, actor)
            if perm not in ("admin", "maintain"):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Actor {actor} lacks admin/maintain permission on {body.repo}",
                )
        elif actor.lower() != repo_owner.lower():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Actor {actor} is not the owner of {body.repo}",
            )
    owner = actor or repo_owner
    tenant = store.tenant_by_repo(body.repo)
    if tenant is None:
        tenant = store.create_tenant(body.repo, owner)
    request.state.tenant = tenant.repo
    if gh is not None and hasattr(gh, "seed_repository_labels"):
        try:
            gh.seed_repository_labels(tenant.repo)
        except Exception as exc:
            logger.warning("Failed to seed labels on %s during tenant creation: %s", tenant.repo, exc)

    result: dict[str, Any] = {
        "id": tenant.id,
        "tenant_id": tenant.id,
        "repo": tenant.repo,
        "status": tenant.status,
        "arbitration_mode": tenant.arbitration_mode,
        "manifest": tenant.manifest,
        "manifest_sha": canonical_sha(tenant.manifest or {}),
    }
    if body.dispatch_onboard:
        existing_onb = (
            store.active_session_for_role(tenant.id, "onboarder", issue_id=body.issue_number)
            if hasattr(store, "active_session_for_role")
            else None
        )
        if existing_onb is not None:
            result["run_id"] = str(existing_onb.get("run_id") or "")
            result["instance_id"] = str(existing_onb.get("instance_id") or "")
            result["dispatched"] = False
            result["duplicate"] = True
            return result
        run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
        store.claim_wiki_refresh(tenant.id, tenant.repo, run_id)
        store.create_run_credential(run_id, tenant.id, "onboarder", cred_hash, expires_at)
        max_mins = _tenant_spot_max_minutes(store, tenant)
        spot_res = launch_spot_instance(
            {
                "role": "onboarder",
                "target_repo": tenant.repo,
                "issue_id": body.issue_number,
                "pr": body.issue_number,
                "label": "flow:onboard",
                "run_id": run_id,
                "run_credential": run_cred,
                "spot_max_instance_minutes": max_mins,
                "target_ref": _default_branch(tenant),
            },
            ec2_client=ec2_client,
        )
        store.upsert_session(
            {
                "instance_id": f"pending-{run_id}",
                "run_id": run_id,
                "tenant_id": tenant.id,
                "role": "onboarder",
                "target_repo": tenant.repo,
                "issue_id": body.issue_number,
                "status": "pending",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        result["run_id"] = run_id
        result["instance_id"] = spot_res.get("instance_id", "")
        result["dispatched"] = True
    return result


@internal.put("/tenants/{tenant_id}/arbitration-mode")
def handle_update_arbitration_mode(
    tenant_id: str,
    body: ArbitrationModeUpdateRequest,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    store.set_arbitration_mode(tenant.id, body.mode)
    manifest = dict(tenant.manifest or {})
    arb = dict(manifest.get("arbitration") or {})
    arb["mode"] = body.mode
    manifest["arbitration"] = arb
    sha = canonical_sha(manifest)
    store.update_tenant_manifest(tenant.id, manifest, sha)
    return {
        "tenant_id": tenant.id,
        "arbitration_mode": body.mode,
        "manifest": manifest,
        "manifest_sha": sha,
    }


@internal.patch("/tenants/{tenant_id}/assignee")
@internal.put("/tenants/{tenant_id}/assignee")
def handle_update_assignee(
    tenant_id: str,
    body: AssigneeUpdateRequest,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    manifest = dict(tenant.manifest or {})
    manifest["owner_github_login"] = body.owner_github_login.strip()
    sha = canonical_sha(manifest)
    store.update_tenant_manifest(tenant.id, manifest, sha)
    store.record_security_audit(
        "assignee.updated",
        tenant_id=tenant.id,
        actor=body.actor,
        metadata={"owner_github_login": manifest["owner_github_login"]},
    )
    return {
        "tenant_id": tenant.id,
        "owner_github_login": manifest["owner_github_login"],
        "manifest": manifest,
        "manifest_sha": sha,
    }


@internal.put("/tenants/{tenant_id}/guardrails")
def handle_update_guardrails(
    tenant_id: str,
    body: GuardrailsUpdateRequest,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    res = store.set_tenant_guardrails(
        tenant.id,
        spot_max_instance_minutes=body.spot_max_instance_minutes,
        max_pr_wall_clock_minutes=body.max_pr_wall_clock_minutes,
        max_pr_cost_usd=body.max_pr_cost_usd,
        max_review_rounds=body.max_review_rounds,
        max_repeated_finding=body.max_repeated_finding,
    )
    return {"tenant_id": tenant.id, **res}


@internal.put("/tenants/{tenant_id}/goal")
def handle_update_goal(
    tenant_id: str,
    body: GoalUpdateRequest,
    request: Request,
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    overlap = set(body.targets) & set(body.guardrails)
    if overlap:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"metric key(s) cannot be in both goal.targets and goal.guardrails: {sorted(overlap)}",
        )

    tracking_issue_number = body.tracking_issue_number
    if tracking_issue_number is None and gh is not None and hasattr(gh, "create_issue"):
        try:
            created = gh.create_issue(
                tenant.repo,
                title=f"[Goal] {body.objective[:80]}",
                body=f"Tracking issue for repository goal:\n\n{body.objective}",
                labels=[GOAL_ISSUE_LABEL],
            )
            if isinstance(created, dict) and created.get("number"):
                tracking_issue_number = int(created["number"])
        except Exception:
            pass
    if tracking_issue_number is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "tracking_issue_number is required when GitHub issue auto-creation is unavailable",
        )

    prev_goal = (tenant.manifest or {}).get("goal") if isinstance(tenant.manifest, dict) else None
    prev_autonomy = prev_goal.get("autonomy") if isinstance(prev_goal, dict) else None

    goal_payload = {
        "objective": body.objective,
        "targets": body.targets,
        "guardrails": body.guardrails,
        "autonomy": body.autonomy,
        "tracking_issue_number": tracking_issue_number,
        "budget": body.budget.model_dump(mode="json"),
        "stall": body.stall.model_dump(mode="json"),
    }
    try:
        res = store.set_tenant_goal(tenant.id, goal_payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    store.record_security_audit(
        "goal.updated",
        tenant_id=tenant.id,
        actor=body.actor,
        metadata={
            "autonomy": body.autonomy,
            "previous_autonomy": prev_autonomy,
            "targets": body.targets,
            "guardrails": body.guardrails,
            "tracking_issue_number": tracking_issue_number,
        },
    )
    return {"tenant_id": tenant.id, **res}


@internal.put("/tenants/{tenant_id}/metrics/{key}")
def handle_upsert_metric_draft(
    tenant_id: str,
    key: str,
    body: MetricDraftRequest,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if not METRIC_KEY_RE.fullmatch(key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    row = store.upsert_metric_definition(
        tenant.id,
        {
            "key": key,
            "intent": body.intent,
            "kind": body.kind,
            "rationale": body.rationale or "",
            "owner_login": body.actor,
            "status": "draft",
            "source": None,
            "comparator": None,
            "threshold": None,
            "compiled": None,
            "digest": None,
            "dry_run": None,
        },
    )
    return {"status": "draft", "key": key, "metric": row}


@internal.post("/tenants/{tenant_id}/metrics/{key}/compile")
def handle_compile_metric(
    tenant_id: str,
    key: str,
    request: Request,
    body: Optional[MetricCompileRequest] = Body(default=None),
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    defn = store.get_metric_definition(tenant.id, key)
    if defn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Metric {key} not found")

    existing_comp = (
        store.active_session_for_role(tenant.id, "metric_compiler", metric_key=key)
        if hasattr(store, "active_session_for_role")
        else None
    )
    if existing_comp is not None:
        return {
            "status": "already_compiling",
            "dispatched": False,
            "role": "metric_compiler",
            "key": key,
            "run_id": str(existing_comp.get("run_id") or ""),
            "instance_id": str(existing_comp.get("instance_id") or ""),
        }

    base_run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
    run_id = f"{base_run_id}__{key}"
    store.create_run_credential(run_id, tenant.id, "metric_compiler", cred_hash, expires_at, metric_key=key)
    request.state.run_id = run_id

    max_mins = _tenant_spot_max_minutes(store, tenant)
    spot_res = launch_spot_instance(
        {
            "role": "metric_compiler",
            "target_repo": tenant.repo,
            "issue_id": 0,
            "pr": 0,
            "run_id": run_id,
            "run_credential": run_cred,
            "spot_max_instance_minutes": max_mins,
            "metric_key": key,
            "metric_intent": str(defn.get("intent") or ""),
            "target_ref": _default_branch(tenant),
        },
        ec2_client=ec2_client,
    )
    store.upsert_session(
        {
            "instance_id": f"pending-{run_id}",
            "run_id": run_id,
            "tenant_id": tenant.id,
            "role": "metric_compiler",
            "target_repo": tenant.repo,
            "issue_id": 0,
            "status": "pending",
            "metric_key": key,
            "current_todo": f"compile:{key}",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return {
        "status": "dispatched",
        "dispatched": True,
        "role": "metric_compiler",
        "key": key,
        "run_id": run_id,
        "instance_id": spot_res.get("instance_id", ""),
    }


def _candidate_manifest_with_approved_metric(
    store: Any,
    tenant: Tenant,
    key: str,
    defn: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    manifest = dict(tenant.manifest or {})
    manifest.pop("created_by", None)
    manifest.pop("spot_max_instance_minutes", None)
    manifest.setdefault("repo", tenant.repo)
    manifest.setdefault("owner_github_login", tenant.repo.split("/", 1)[0])
    manifest.setdefault("arbitration", {"mode": tenant.arbitration_mode})

    all_defs = list(store.metric_definitions(tenant.id))
    active_defs: list[dict[str, Any]] = []
    found = False
    raw_compiled = defn.get("compiled")
    compiled_data: dict[str, Any] = dict(raw_compiled) if isinstance(raw_compiled, dict) else {}
    promoted = {**defn, **compiled_data, "status": "active"}

    for d in all_defs:
        if d.get("key") == key:
            active_defs.append(promoted)
            found = True
        elif d.get("status", "active") == "active":
            active_defs.append(d)
    if not found:
        active_defs.append(promoted)

    active_defs.sort(key=lambda r: str(r.get("key", "")))
    metrics_list = []
    for d in active_defs:
        src = d.get("source") or {}
        run_cmd = d.get("run") or (src.get("run") if isinstance(src, dict) else None)
        comparator = d.get("comparator")
        raw_thr = d.get("threshold")
        if not run_cmd or not comparator or raw_thr is None:
            raise ValueError(
                f"Active metric '{d.get('key')}' is incomplete: run, comparator, and threshold are required"
            )
        d_comp = d.get("compiled")
        tol = d.get("tolerance_pct")
        if tol is None and isinstance(d_comp, dict):
            tol = d_comp.get("tolerance_pct")
        m_entry: dict[str, Any] = {
            "key": d["key"],
            "run": run_cmd,
            "comparator": comparator,
            "threshold": float(raw_thr),
            "baseline_mode": d.get("baseline_mode") or "absolute",
            "min_samples": int(d.get("min_samples") or 1),
        }
        if tol is not None:
            m_entry["tolerance_pct"] = float(tol)
        metrics_list.append(m_entry)
    manifest["metrics"] = metrics_list
    TenantManifest.model_validate(manifest)
    return manifest, canonical_sha(manifest)


def _render_metrics_workflow_for_manifest(manifest: dict[str, Any], cp_url: str) -> str:
    if manifest.get("metrics"):
        m_obj = TenantManifest.model_validate(manifest)
        return render_metrics(m_obj, cp_url)
    from atobar_contracts.manifest import MetricsRuntime

    repo = str(manifest.get("repo") or "owner/repo")
    default_branch = str(manifest.get("default_branch") or "main")
    owner = str(manifest.get("owner_github_login") or repo.split("/", 1)[0])
    rt = MetricsRuntime.model_validate(manifest.get("metrics_runtime") or {})
    m_empty = TenantManifest.model_construct(
        repo=repo,
        default_branch=default_branch,
        owner_github_login=owner,
        metrics_runtime=rt,
        metrics=[],
    )
    return render_metrics(m_empty, cp_url)


@internal.post("/tenants/{tenant_id}/metrics/{key}/approve")
def handle_approve_metric(
    tenant_id: str,
    key: str,
    body: MetricApproveRequest,
    request: Request,
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    defn = store.get_metric_definition(tenant.id, key)
    if defn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Metric {key} not found")

    candidate_manifest = dict(tenant.manifest or {})
    candidate_sha = canonical_sha(candidate_manifest)
    already_approved_same_digest = defn.get("status") == "active" and str(defn.get("digest") or "") == str(body.digest)
    if defn.get("status") in ("compiled", "active") and str(defn.get("digest") or "") == str(body.digest):
        try:
            candidate_manifest, candidate_sha = _candidate_manifest_with_approved_metric(store, tenant, key, defn)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    if not already_approved_same_digest:
        try:
            store.approve_metric_atomic(
                tenant.id,
                key,
                body.digest,
                body.actor,
                candidate_manifest,
                candidate_sha,
            )
        except LookupError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Metric {key} not found")
        except ValueError as exc:
            if "digest_mismatch" in str(exc):
                raise HTTPException(status.HTTP_409_CONFLICT, "digest_mismatch")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
        except Exception as exc:
            msg = str(exc)
            if "metric_not_found" in msg:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Metric {key} not found")
            if "digest_mismatch" in msg:
                raise HTTPException(status.HTTP_409_CONFLICT, "digest_mismatch")
            if "incomplete_metric_definition" in msg:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "incomplete_metric_definition")
            raise

    manifest, manifest_sha = store.rebuild_tenant_manifest(tenant.id)
    workflow_yaml = _render_metrics_workflow_for_manifest(manifest, body.cp_url)
    pr_info: Optional[dict[str, Any]] = None
    if gh is not None and hasattr(gh, "open_or_update_metrics_pr"):
        try:
            pr_info = gh.open_or_update_metrics_pr(
                tenant.repo,
                workflow_yaml,
                default_branch=str(manifest.get("default_branch") or "main"),
            )
        except Exception as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Metric approved in policy store, but opening workflow PR failed: {exc}",
            )

    result: dict[str, Any] = {
        "status": "active",
        "key": key,
        "manifest": manifest,
        "manifest_sha": manifest_sha,
        "workflow_yaml": workflow_yaml,
    }
    if pr_info:
        result["pr"] = pr_info
    return result


@internal.post("/tenants/{tenant_id}/metrics/{key}/retire")
def handle_retire_metric(
    tenant_id: str,
    key: str,
    request: Request,
    body: Optional[MetricRetireRequest] = Body(default=None),
    store: Any = Depends(get_store),
    gh: Any = Depends(get_github_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo

    defn = store.get_metric_definition(tenant.id, key)
    if defn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Metric {key} not found")

    try:
        store.retire_metric(tenant.id, key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    manifest, manifest_sha = store.rebuild_tenant_manifest(tenant.id)
    from atobar_contracts.client_templates import resolve_cp_url

    cp_url = resolve_cp_url(body.cp_url if body else None)
    workflow_yaml = _render_metrics_workflow_for_manifest(manifest, cp_url)
    pr_info: Optional[dict[str, Any]] = None
    if gh is not None and hasattr(gh, "open_or_update_metrics_pr"):
        try:
            pr_info = gh.open_or_update_metrics_pr(
                tenant.repo,
                workflow_yaml,
                default_branch=str(manifest.get("default_branch") or "main"),
            )
        except Exception as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Metric retired in policy store, but updating workflow PR failed: {exc}",
            )

    result: dict[str, Any] = {
        "status": "retired",
        "key": key,
        "manifest": manifest,
        "manifest_sha": manifest_sha,
        "workflow_yaml": workflow_yaml,
    }
    if pr_info:
        result["pr"] = pr_info
    return result


def _dispatch_or_queue_wiki_refresh(
    store: Any,
    tenant: Tenant,
    ec2_client: Any,
    *,
    background: bool = False,
) -> dict[str, Any]:
    run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
    claim_res = store.claim_wiki_refresh(tenant.id, tenant.repo, run_id)
    if not claim_res.get("claimed"):
        active_run_id = str(claim_res.get("active_run_id") or "")
        existing_onb = (
            store.get_session_by_run(active_run_id)
            if active_run_id
            else store.active_session_for_role(tenant.id, "onboarder")
        )
        return {
            "status": "queued",
            "dispatched": False,
            "queued": True,
            "role": "onboarder",
            "run_id": active_run_id or str((existing_onb or {}).get("run_id") or ""),
            "instance_id": str((existing_onb or {}).get("instance_id") or ""),
        }

    store.create_run_credential(run_id, tenant.id, "onboarder", cred_hash, expires_at)
    store.upsert_session(
        {
            "instance_id": f"pending-{run_id}",
            "run_id": run_id,
            "tenant_id": tenant.id,
            "role": "onboarder",
            "target_repo": tenant.repo,
            "issue_id": 0,
            "status": "pending",
            "current_todo": "wiki:refresh",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    max_mins = _tenant_spot_max_minutes(store, tenant)
    try:
        spot_res = launch_spot_instance(
            {
                "role": "onboarder",
                "target_repo": tenant.repo,
                "issue_id": 0,
                "pr": 0,
                "label": "flow:wiki-refresh",
                "run_id": run_id,
                "run_credential": run_cred,
                "spot_max_instance_minutes": max_mins,
                "target_ref": _default_branch(tenant),
            },
            ec2_client=ec2_client,
        )
    except Exception as exc:
        err_str = str(exc)[:500]
        now_iso = datetime.now(timezone.utc).isoformat()
        store.update_session(f"pending-{run_id}", {"status": f"Halted: {err_str}", "heartbeat_at": now_iso})
        store.complete_wiki_refresh_run(tenant.id, tenant.repo, run_id, halted_error=err_str)
        if background:
            logger.warning("Background wiki refresh launch failed for %s: %s", tenant.repo, exc)
            return {"status": "failed", "dispatched": False, "queued": False, "error": err_str}
        raise

    return {
        "status": "dispatched",
        "dispatched": True,
        "queued": False,
        "role": "onboarder",
        "run_id": run_id,
        "instance_id": str(spot_res.get("instance_id") or ""),
    }


def _finalize_onboarder_run(
    store: Any,
    tenant: Tenant,
    run_id: str,
    *,
    halted_error: Optional[str] = None,
    ec2_client: Any = None,
) -> Optional[dict[str, Any]]:
    now_iso = datetime.now(timezone.utc).isoformat()
    terminal_status = f"Halted: {halted_error}" if halted_error else "Complete"
    store.update_session(f"pending-{run_id}", {"status": terminal_status, "heartbeat_at": now_iso})
    fin = store.complete_wiki_refresh_run(
        tenant.id,
        tenant.repo,
        run_id,
        halted_error=str(halted_error)[:500] if halted_error else None,
    )
    if fin.get("finalized") and fin.get("drain_queued") and ec2_client is not None:
        return _dispatch_or_queue_wiki_refresh(store, tenant, ec2_client, background=True)
    return None


@internal.post("/tenants/{tenant_id}/wiki/regenerate")
def handle_regenerate_wiki(
    tenant_id: str,
    request: Request,
    body: Optional[WikiRegenerateRequest] = Body(default=None),
    store: Any = Depends(get_store),
    ec2_client: Any = Depends(get_ec2_client),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo
    actor = (body.actor if body else None) or request.headers.get("X-Actor-Login") or "admin"
    res = _dispatch_or_queue_wiki_refresh(store, tenant, ec2_client, background=False)
    if res.get("run_id"):
        request.state.run_id = res["run_id"]
    store.record_security_audit(
        "wiki.regenerate_requested",
        tenant_id=tenant.id,
        actor=actor,
        run_id=str(res.get("run_id") or "") or None,
        metadata={"status": res.get("status"), "queued": res.get("queued", False)},
    )
    return res


@internal.post("/tenants/{tenant_id}/wiki/policy")
def handle_update_wiki_policy(
    tenant_id: str,
    body: WikiPolicyUpdateRequest,
    request: Request,
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    tenant = _resolve_tenant(store, tenant_id, request=request)
    request.state.tenant = tenant.repo
    updated = store.update_wiki_state(
        tenant.id,
        tenant.repo,
        {"refresh_interval_merges": body.refresh_interval_merges},
    )
    store.record_security_audit(
        "wiki.policy_updated",
        tenant_id=tenant.id,
        actor=body.actor,
        metadata={"refresh_interval_merges": body.refresh_interval_merges},
    )
    return {
        "tenant_id": tenant.id,
        "refresh_interval_merges": body.refresh_interval_merges,
        "wiki": updated,
    }


app.include_router(internal)
