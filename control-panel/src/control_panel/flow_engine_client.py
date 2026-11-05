"""HTTP client for Control Panel (:8080) -> Flow Engine (:8081) internal RPC (#301 WP-3)."""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

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
    resolve_human_assignee,
    compute_event_id,
    handle_merged_pr_event,
    is_issue_awaiting_plan_approval,
    process_flow_event,
    upsert_issue_comment_by_marker,
)
from atobar_contracts.constants import LOOP_EPOCH_MARKER, get_bot_logins
from atobar_contracts.envelopes import (
    execute_goal_plan_proposals,
    parse,
    parse_and_validate_goal_plan,
)
from atobar_contracts.issue_states import (
    ONBOARDING_HOLD_MARKER,
    render_onboarding_blocked_comment,
)
from atobar_contracts.manifest import METRIC_KEY_RE, REPO_RE, Metric, canonical_sha
from atobar_contracts.run_credentials import (
    ROLE_GITHUB_PERMISSIONS,
    MintRateLimiter,
    generate_run_credentials,
)
from atobar_contracts.store import SESSION_STALE_AFTER_MINUTES, Store, Tenant
from atobar_contracts.sufficiency import assess_issue_sufficiency
from control_panel.github import Dispatcher
from control_panel.store import get_store

logger = logging.getLogger(__name__)

DEFAULT_FE_URL = "http://flow-engine:8081"
DEFAULT_TIMEOUT_S = 5.0
LONG_OPERATION_TIMEOUT_S = 35.0
_GITHUB_TOKEN_MINT_LIMITER = MintRateLimiter()
_BLOCKED_TRIGGER_LABELS = {
    "flow:in-progress",
    "flow:in-review",
    "flow:escalated",
    "flow:needs-approval",
    "flow:shipped",
}


def _to_json(payload: BaseModel | dict[str, Any] | None) -> Optional[dict[str, Any]]:
    if payload is None:
        return None
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return dict(payload)


def _seg(value: str, label: str = "identifier") -> str:
    cleaned = str(value or "").strip()
    if not cleaned or "/" in cleaned or ".." in cleaned:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid {label}")
    return quote(cleaned, safe="")


class FlowEngineClient:
    """Thin internal HTTP client from Control Panel to Flow Engine (:8081)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        internal_token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("FE_URL") or os.environ.get("FLOW_ENGINE_URL") or DEFAULT_FE_URL
        ).rstrip("/")
        self._internal_token = internal_token
        self.timeout = timeout
        self._transport = transport

    def _headers(
        self,
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, str]:
        req_id = (request_id or "").strip() or str(uuid.uuid4())
        tok = (
            self._internal_token
            if self._internal_token is not None
            else os.environ.get("FE_INTERNAL_TOKEN", "")
        ).strip()
        headers: dict[str, str] = {
            "Authorization": f"Bearer {tok}",
            "X-Request-ID": req_id,
        }
        if tenant_id:
            headers["X-Tenant-ID"] = str(tenant_id)
        if tenant_repo:
            headers["X-Tenant-Repo"] = str(tenant_repo)
        if run_credential:
            headers["X-Run-Credential"] = str(run_credential)
        if actor_login:
            headers["X-Actor-Login"] = str(actor_login)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
        actor_login: Optional[str] = None,
        timeout: Optional[float | httpx.Timeout] = None,
    ) -> dict[str, Any]:
        headers = self._headers(
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
            actor_login=actor_login,
        )
        req_id = headers["X-Request-ID"]
        resp: Optional[httpx.Response] = None
        eff_timeout = timeout if timeout is not None else httpx.Timeout(LONG_OPERATION_TIMEOUT_S, connect=self.timeout)

        with httpx.Client(base_url=self.base_url, timeout=eff_timeout, transport=self._transport) as client:
            for attempt in range(2):
                try:
                    resp = client.request(
                        method,
                        path,
                        headers=headers,
                        json=json_body,
                        params=params,
                    )
                    break
                except httpx.ConnectError:
                    if attempt == 0:
                        continue
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY,
                        detail={"error": "flow_engine_unavailable", "request_id": req_id},
                    )
                except httpx.RequestError:
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY,
                        detail={"error": "flow_engine_unavailable", "request_id": req_id},
                    )

        if resp is None or resp.status_code >= 500:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"error": "flow_engine_unavailable", "request_id": req_id},
            )
        if resp.status_code >= 400:
            try:
                err_data = resp.json()
                detail = err_data.get("detail", err_data) if isinstance(err_data, dict) else err_data
            except Exception:
                detail = resp.text or "Flow Engine request failed"
            if resp.status_code in (401, 403) and isinstance(detail, str) and "internal authentication token" in detail.lower():
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    detail={"error": "flow_engine_unavailable", "request_id": req_id},
                )
            raise HTTPException(status_code=resp.status_code, detail=detail)

        try:
            data = resp.json()
        except Exception:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"error": "flow_engine_unavailable", "request_id": req_id},
            )
        return dict(data) if isinstance(data, dict) else {"result": data}

    def post_events(
        self,
        payload: InternalEventRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/v1/events",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
        )

    def post_metrics(
        self,
        payload: InternalMetricsRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/v1/metrics",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
        )

    def post_run_heartbeat(
        self,
        run_id: str,
        payload: HeartbeatPayload | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/heartbeat",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_events(
        self,
        run_id: str,
        payload: RunEventsBatchPayload | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/events",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_turn_complete(
        self,
        run_id: str,
        payload: TurnCompletePayload | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/turn-complete",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_arbitration_decision(
        self,
        run_id: str,
        payload: ArbitrationDecisionPayload | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/arbitration-decisions",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def get_run_arbitration_data(
        self,
        run_id: str,
        *,
        pr_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if pr_number is not None:
            params["pr_number"] = pr_number
        if commit_sha:
            params["commit_sha"] = commit_sha
        return self._request(
            "GET",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/arbitration-data",
            params=params or None,
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def get_run_wiki(
        self,
        run_id: str,
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/wiki",
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_wiki(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/wiki",
            json_body=dict(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_github_token(
        self,
        run_id: str,
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/github-token",
            json_body={},
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_run_credential_used(
        self,
        run_id: str,
        *,
        instance_id: Optional[str] = None,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if instance_id:
            payload["instance_id"] = instance_id
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/credential-used",
            json_body=payload,
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def post_gc_sweep(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/v1/gc/sweep",
            json_body={},
            request_id=request_id,
        )

    def post_run_metric_compilation(
        self,
        run_id: str,
        payload: MetricCompilationPayload | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        run_credential: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/runs/{_seg(run_id, 'run_id')}/metric-compilation",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            run_credential=run_credential,
        )

    def create_tenant(
        self,
        payload: CreateTenantRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/v1/tenants",
            json_body=_to_json(payload),
            request_id=request_id,
            actor_login=actor_login,
        )

    def update_arbitration_mode(
        self,
        tenant_id: str,
        payload: ArbitrationModeUpdateRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/arbitration-mode",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def update_assignee(
        self,
        tenant_id: str,
        payload: AssigneeUpdateRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/assignee",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def update_guardrails(
        self,
        tenant_id: str,
        payload: GuardrailsUpdateRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/guardrails",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def update_goal(
        self,
        tenant_id: str,
        payload: GoalUpdateRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/goal",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def upsert_metric_draft(
        self,
        tenant_id: str,
        key: str,
        payload: MetricDraftRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        if not METRIC_KEY_RE.fullmatch(key):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
        return self._request(
            "PUT",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/metrics/{_seg(key, 'key')}",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def compile_metric(
        self,
        tenant_id: str,
        key: str,
        payload: Optional[MetricCompileRequest | dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        if not METRIC_KEY_RE.fullmatch(key):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
        return self._request(
            "POST",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/metrics/{_seg(key, 'key')}/compile",
            json_body=_to_json(payload or MetricCompileRequest(actor=actor_login or "admin")),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def approve_metric(
        self,
        tenant_id: str,
        key: str,
        payload: MetricApproveRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        if not METRIC_KEY_RE.fullmatch(key):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
        return self._request(
            "POST",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/metrics/{_seg(key, 'key')}/approve",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def retire_metric(
        self,
        tenant_id: str,
        key: str,
        payload: Optional[MetricRetireRequest | dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        if not METRIC_KEY_RE.fullmatch(key):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid metric key format")
        return self._request(
            "POST",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/metrics/{_seg(key, 'key')}/retire",
            json_body=_to_json(payload or MetricRetireRequest(actor=actor_login or "admin")),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def regenerate_wiki(
        self,
        tenant_id: str,
        payload: Optional[WikiRegenerateRequest | dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/wiki/regenerate",
            json_body=_to_json(payload or WikiRegenerateRequest(actor=actor_login or "admin")),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )

    def update_wiki_policy(
        self,
        tenant_id: str,
        payload: WikiPolicyUpdateRequest | dict[str, Any],
        *,
        request_id: Optional[str] = None,
        tenant_repo: Optional[str] = None,
        actor_login: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/v1/tenants/{_seg(tenant_id, 'tenant_id')}/wiki/policy",
            json_body=_to_json(payload),
            request_id=request_id,
            tenant_id=tenant_id,
            tenant_repo=tenant_repo,
            actor_login=actor_login,
        )


class _LocalGitHubAdapter:
    """In-memory GitHub adapter for hermetic Control Panel unit tests when FE_URL is unset."""

    def __init__(
        self,
        dispatcher: Any,
        store: Any,
        repo: str,
        pr_number: int,
        head_sha: str,
        exclude_comment_id: Optional[int] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._store = store
        self._repo = repo
        self._pr_number = pr_number
        self._head_sha = head_sha if (head_sha and head_sha != "0" * 40) else ""
        self._exclude_comment_id = exclude_comment_id

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        if hasattr(self._dispatcher, "get_pr"):
            return dict(self._dispatcher.get_pr(repo, pr_number))
        labels_map = getattr(self._dispatcher, "labels", {})
        raw_labels = list(labels_map.get((repo, pr_number), set())) if isinstance(labels_map, dict) else []
        return {
            "number": pr_number,
            "head": {"sha": self._head_sha},
            "labels": [{"name": lbl} for lbl in raw_labels],
        }

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        return []

    def get_commit_sha(self, repo: str, ref: str) -> str:
        return self._head_sha

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        if hasattr(self._dispatcher, "comments") and isinstance(getattr(self._dispatcher, "comments"), dict):
            comments = list(getattr(self._dispatcher, "comments").get((repo, pr_number), []))
        turn_comments = getattr(self._store, "_turn_comments", {})
        if isinstance(turn_comments, dict):
            seen_ids = {c.get("id") for c in comments if c.get("id") is not None}
            for c in turn_comments.get((repo, pr_number), []):
                if c.get("id") is None or c.get("id") not in seen_ids:
                    comments.append(c)
        return comments

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        if hasattr(self._dispatcher, "check_runs") and isinstance(getattr(self._dispatcher, "check_runs"), dict):
            return list(getattr(self._dispatcher, "check_runs").get((repo, ref), []))
        return []

    def create_comment(self, repo: str, issue_number: int, body: str) -> None:
        if hasattr(self._dispatcher, "create_issue_comment"):
            self._dispatcher.create_issue_comment(repo, issue_number, body)

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if self._dispatcher is None or not hasattr(self._dispatcher, "create_issue"):
            raise RuntimeError("Dispatcher with create_issue is required to open goal issues")
        return dict(self._dispatcher.create_issue(repo, title=title, body=body, labels=labels))

    def list_open_issues(self, repo: str) -> list[dict[str, Any]]:
        if self._dispatcher is not None and hasattr(self._dispatcher, "list_open_issues"):
            return list(self._dispatcher.list_open_issues(repo))
        return []

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        self.create_comment(repo, pr_number, body)

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        pass

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        if hasattr(self._dispatcher, "add_issue_label"):
            self._dispatcher.add_issue_label(repo, issue_number, label)

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        if hasattr(self._dispatcher, "remove_issue_label"):
            self._dispatcher.remove_issue_label(repo, issue_number, label)

    def is_valid_assignee(self, repo: str, login: str) -> bool:
        if hasattr(self._dispatcher, "is_valid_assignee"):
            return bool(self._dispatcher.is_valid_assignee(repo, login))
        return True

    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        if hasattr(self._dispatcher, "add_assignees"):
            self._dispatcher.add_assignees(repo, issue_number, assignees)

    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        if hasattr(self._dispatcher, "remove_assignees"):
            self._dispatcher.remove_assignees(repo, issue_number, assignees)

    def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        if hasattr(self._dispatcher, "request_reviewers"):
            self._dispatcher.request_reviewers(repo, pr_number, reviewers)

    def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None:
        if hasattr(self._dispatcher, "dispatch"):
            self._dispatcher.dispatch("flow_run", payload)

    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]:
        if hasattr(self._dispatcher, "find_pr_for_sha"):
            res = self._dispatcher.find_pr_for_sha(repo, sha)
            return int(res) if res else None
        return self._pr_number or None

    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        return next(
            (c for c in self.get_comments(repo, pr_number) if str(c.get("id")) == str(comment_id)),
            None,
        )

    def get_pr_review(self, repo: str, pr_number: int, review_id: int) -> Optional[dict[str, Any]]:
        return None

    def get_pr_review_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        return None

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return []

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return []

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return []

    def update_comment(self, repo: str, comment_id: int, body: str) -> Any:
        return None

    def add_comment_reaction(
        self, repo: str, comment_id: int, content: str = "eyes", *, is_review_comment: bool = False
    ) -> Any:
        return None

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self.get_pr(repo, issue_number)


def _lookup_tenant(store: Any, ident: str) -> Optional[Tenant]:
    if not ident:
        return None
    tenant: Optional[Tenant] = None
    if hasattr(store, "tenant_by_id"):
        tenant = store.tenant_by_id(ident)
    if tenant is None and hasattr(store, "tenant_by_repo"):
        tenant = store.tenant_by_repo(ident)
    if tenant is None and hasattr(store, "tenants") and isinstance(store.tenants, dict):
        tenant = store.tenants.get(ident)
        if tenant is None:
            tenant = next((t for t in store.tenants.values() if t.repo == ident), None)
    return tenant


def _build_local_fallback_transport(store: Store, dispatcher: Optional[Dispatcher] = None) -> httpx.MockTransport:
    """In-memory MockTransport used ONLY when FE_URL is unset in hermetic unit tests."""

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body: dict[str, Any] = json.loads(request.content.decode("utf-8")) if request.content else {}
        tid_hdr = request.headers.get("X-Tenant-ID", "")
        repo_hdr = request.headers.get("X-Tenant-Repo", "")

        # 1. POST /internal/v1/events
        if method == "POST" and path == "/internal/v1/events":
            tenant = _lookup_tenant(store, str(body.get("tenant_id") or body.get("tenant") or tid_hdr or repo_hdr))
            if tenant is None:
                return httpx.Response(404, json={"detail": "Tenant not found"}, request=request)
            if tenant.status not in ("active", "onboarding"):
                return httpx.Response(409, json={"detail": f"tenant is {tenant.status}"}, request=request)

            raw_evt = dict(body.get("event") or {})
            inner = dict(raw_evt["event"]) if isinstance(raw_evt.get("event"), dict) else raw_evt
            kind = str(inner.get("kind") or "")
            label = str(inner.get("label") or "")

            if kind in ("issues.labeled", "issues.edited"):
                issue_num = int(inner.get("issue_number") or inner.get("issue_id") or 0)
                if kind == "issues.labeled" and label in _BLOCKED_TRIGGER_LABELS:
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "reason": f"label {label} cannot spawn a dispatch"},
                        request=request,
                    )
                if not label.startswith("flow:"):
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "reason": "label is not flow:*"},
                        request=request,
                    )
                if label == "flow:onboard":
                    run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
                    if hasattr(store, "create_run_credential"):
                        store.create_run_credential(run_id, tenant.id, "onboarder", cred_hash, expires_at)
                    if dispatcher is not None and hasattr(dispatcher, "dispatch"):
                        dispatcher.dispatch("flow_onboard", {"repo": tenant.repo, "issue_number": issue_num})
                    if dispatcher is not None and hasattr(dispatcher, "onboard_tenant"):
                        getattr(dispatcher, "onboard_tenant")(
                            tenant.repo,
                            issue_num,
                            run_id=run_id,
                            run_credential=run_cred,
                        )
                    return httpx.Response(200, json={"dispatched": True}, request=request)
                if label in ("flow:onboard", "flow:metrics"):
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "reason": f"{label} is handled by onboarding, not by an agent run"},
                        request=request,
                    )
                if tenant.status != "active":
                    if label == "flow:approved":
                        return httpx.Response(
                            200,
                            json={"dispatched": False, "reason": f"{label} is not an agent run trigger"},
                            request=request,
                        )
                    if dispatcher is not None and issue_num > 0:
                        if hasattr(dispatcher, "remove_issue_label"):
                            try:
                                dispatcher.remove_issue_label(tenant.repo, issue_num, label)
                            except Exception as exc:
                                logger.warning("Failed to remove label %s on %s#%s: %s", label, tenant.repo, issue_num, exc)
                        from atobar_contracts.client_templates import resolve_cp_url

                        cp_url = resolve_cp_url()
                        hold_body = render_onboarding_blocked_comment(tenant.repo, label, cp_url=cp_url)
                        upsert_issue_comment_by_marker(dispatcher, tenant.repo, issue_num, ONBOARDING_HOLD_MARKER, hold_body)
                    return httpx.Response(
                        200,
                        json={
                            "dispatched": False,
                            "reason": "tenant is still onboarding; agent runs start once it is active",
                        },
                        request=request,
                    )
                if label == "flow:approved" and not is_issue_awaiting_plan_approval(
                    tenant.repo, issue_num, inner, dispatcher, store
                ):
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "reason": f"{label} is not an agent run trigger"},
                        request=request,
                    )
                if hasattr(store, "issue_session_active") and store.issue_session_active(tenant.id, issue_num):
                    if dispatcher is not None and hasattr(dispatcher, "create_issue_comment"):
                        dispatcher.create_issue_comment(
                            tenant.repo,
                            issue_num,
                            f"🌊 **flow:** `{label}` ignored — a run is already active on this issue. "
                            "Wait for it to finish; a run whose heartbeat is silent for "
                            f"{SESSION_STALE_AFTER_MINUTES} minutes no longer blocks a new trigger.",
                        )
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "reason": "a run is already active on this issue"},
                        request=request,
                    )
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
                if dispatcher is not None:
                    for stale in stale_labels:
                        if hasattr(dispatcher, "remove_issue_label"):
                            dispatcher.remove_issue_label(tenant.repo, issue_num, stale)
                    if label in ("flow:resume", "flow:approved") and human_login and hasattr(dispatcher, "remove_assignees"):
                        try:
                            dispatcher.remove_assignees(tenant.repo, issue_num, [human_login])
                        except Exception as exc:
                            logger.warning("Failed to unassign %s on %s#%s: %s", human_login, tenant.repo, issue_num, exc)
                    if hasattr(dispatcher, "create_issue_comment"):
                        epoch_id = f"epoch-{uuid.uuid4().hex}"
                        store.create_run_credential(epoch_id, tenant.id, "epoch", epoch_id, "2099-01-01T00:00:00+00:00")
                        dispatcher.create_issue_comment(
                            tenant.repo,
                            issue_num,
                            f"{LOOP_EPOCH_MARKER} <!-- flow:epoch-id:{epoch_id} -->\n"
                            f"🌊 **flow:** `{label}` applied — starting a fresh loop "
                            "(previous rounds and escalations on this issue are reset).",
                        )

                if label in ("flow:resume", "flow:approved"):
                    initial_role = "developer"
                elif label == "flow:plan":
                    initial_role = "planner"
                else:
                    issue_text = inner.get("issue_body")
                    if issue_text is None and dispatcher is not None and hasattr(dispatcher, "get_issue_body"):
                        try:
                            issue_text = dispatcher.get_issue_body(tenant.repo, issue_num)
                        except Exception:
                            issue_text = None
                    initial_role = (
                        assess_issue_sufficiency(issue_text, label).route if issue_text is not None else "developer"
                    )

                run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
                if hasattr(store, "create_run_credential"):
                    store.create_run_credential(run_id, tenant.id, initial_role, cred_hash, expires_at)
                agent_runtime = "nanobot"
                if isinstance(tenant.manifest, dict):
                    agent_runtime = (tenant.manifest.get("loop") or {}).get("agent_runtime", "nanobot")
                if dispatcher is not None and hasattr(dispatcher, "dispatch"):
                    dispatcher.dispatch(
                        "flow_run",
                        {
                            "role": initial_role,
                            "target_repo": tenant.repo,
                            "issue_id": issue_num,
                            "label": label,
                            "run_id": run_id,
                            "run_credential": run_cred,
                            "agent_runtime": agent_runtime,
                        },
                    )
                if hasattr(store, "upsert_session"):
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
                return httpx.Response(200, json={"dispatched": True}, request=request)

            if kind == "pull_request.closed" and bool(inner.get("merged")):
                merged_res = handle_merged_pr_event(tenant.repo, inner, dispatcher, store)
                if merged_res is not None:
                    return httpx.Response(200, json=merged_res, request=request)
                return httpx.Response(
                    200,
                    json={"status": "ignored", "dispatched": False, "reason": "merged PR is not a tracked factory PR"},
                    request=request,
                )

            if tenant.status != "active":
                return httpx.Response(
                    200,
                    json={
                        "dispatched": False,
                        "reason": "tenant is still onboarding; events are only forwarded for active tenants",
                    },
                    request=request,
                )
            if kind == "issue_comment.created" and hasattr(store, "has_processed_event"):
                evt_id = compute_event_id(inner)
                c_id = inner.get("comment_id")
                c_key = f"comment:{inner.get('issue_number')}:{c_id}" if c_id is not None else None
                if store.has_processed_event(tenant.id, evt_id) or (
                    c_key is not None and store.has_processed_event(tenant.id, c_key)
                ):
                    return httpx.Response(
                        200,
                        json={"dispatched": False, "status": "skipped", "reason": "duplicate event"},
                        request=request,
                    )
            if dispatcher is not None and hasattr(dispatcher, "dispatch"):
                dispatcher.dispatch("flow_event", {"target_repo": tenant.repo, "event": inner})
            if (
                not hasattr(dispatcher, "_factory_repo")
                and hasattr(store, "has_processed_event")
                and hasattr(store, "loop_rounds_for_pr")
            ):
                bots = get_bot_logins()
                pr_num = int(inner.get("pr_number") or inner.get("issue_number") or 0)
                head_sha = str(inner.get("head_sha") or "")
                if not head_sha and kind == "issue_comment.created":
                    cid = inner.get("comment_id")
                    if cid is not None and hasattr(dispatcher, "comments") and isinstance(getattr(dispatcher, "comments"), dict):
                        fetched = next(
                            (
                                c
                                for c in getattr(dispatcher, "comments").get((tenant.repo, pr_num), [])
                                if c.get("id") is not None and str(c.get("id")) == str(cid)
                            ),
                            None,
                        )
                        if fetched is not None:
                            parsed_env = parse(
                                str(fetched.get("body") or ""),
                                author_login=str((fetched.get("user") or {}).get("login") or "atobar-flow-agent[bot]"),
                                bots=bots,
                            )
                            if parsed_env is not None and getattr(parsed_env, "head_sha", None):
                                head_sha = str(getattr(parsed_env, "head_sha"))
                gh_adapter = _LocalGitHubAdapter(
                    dispatcher=dispatcher,
                    store=store,
                    repo=tenant.repo,
                    pr_number=pr_num,
                    head_sha=head_sha,
                    exclude_comment_id=inner.get("comment_id"),
                )
                process_flow_event(
                    {"target_repo": tenant.repo, "event": inner},
                    gh=gh_adapter,
                    store=store,
                    bot_logins=bots,
                )
            return httpx.Response(200, json={"dispatched": True}, request=request)

        # 2. POST /internal/v1/metrics
        if method == "POST" and path == "/internal/v1/metrics":
            tenant = _lookup_tenant(store, str(body.get("tenant_id") or body.get("tenant") or tid_hdr or repo_hdr))
            if tenant is None:
                return httpx.Response(404, json={"detail": "Tenant not found"}, request=request)
            if tenant.status not in ("active", "onboarding"):
                return httpx.Response(409, json={"detail": f"tenant is {tenant.status}"}, request=request)
            values = dict(body.get("values") or {})
            required_keys = store.metric_keys(tenant.id) if hasattr(store, "metric_keys") else set()
            unknown = sorted(set(values) - required_keys)
            if unknown:
                return httpx.Response(
                    422, json={"detail": f"undeclared metric keys: {unknown}"}, request=request
                )
            if not all(math.isfinite(float(v)) for v in values.values()):
                return httpx.Response(422, json={"detail": "metric values must be finite"}, request=request)
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            manifest_sha = canonical_sha(tenant.manifest or {})
            if hasattr(store, "record_observations"):
                store.record_observations(
                    [
                        {
                            "tenant_id": tenant.id,
                            "key": k,
                            "value": float(v),
                            "commit_sha": body.get("commit_sha", ""),
                            "pr_number": body.get("pr_number"),
                            "check_run_id": body.get("run_id"),
                            "run_id": body.get("run_id"),
                            "workflow_path": body.get("workflow_path", ""),
                            "run_attempt": body.get("run_attempt"),
                            "artifact_sha256": digest,
                            "manifest_sha": manifest_sha,
                        }
                        for k, v in sorted(values.items())
                    ]
                )
            if tenant.status == "onboarding" and body.get("pr_number") is None:
                observed = store.observed_metric_keys(tenant.id) if hasattr(store, "observed_metric_keys") else set()
                if required_keys and required_keys.issubset(observed):
                    if hasattr(store, "update_tenant_status"):
                        store.update_tenant_status(tenant.id, "active")
                    if dispatcher is not None and hasattr(dispatcher, "comment_on_open_issue_by_label"):
                        card = (
                            "<!-- atobar:status -->\n"
                            "## atobar-flow Status: `flow:shipped`\n\n"
                            "**Next Action:** No action needed; this work has been verified and shipped.\n\n"
                            "**Links:**\n"
                            f"- [Repository](https://github.com/{tenant.repo})\n"
                        )
                        comment = (
                            f"{card}\n"
                            f"Tenant `{tenant.repo}` is now **active**! All declared metrics have reported "
                            "default-branch observations.\n\n"
                            "Labels that now start agent runs:\n"
                            "- `flow:feature`\n"
                            "- `flow:fix`"
                        )
                        dispatcher.comment_on_open_issue_by_label(tenant.repo, "flow:onboard", comment)
                    return httpx.Response(
                        201,
                        json={"recorded": len(values), "activated": True, "manifest_sha": manifest_sha},
                        request=request,
                    )
            return httpx.Response(201, json={"recorded": len(values), "manifest_sha": manifest_sha}, request=request)

        # 3. POST /internal/v1/tenants
        if method == "POST" and path == "/internal/v1/tenants":
            repo = str(body.get("repo") or "")
            if not REPO_RE.fullmatch(repo):
                return httpx.Response(422, json={"detail": "repo must be owner/name"}, request=request)
            owner = str(body.get("created_by") or request.headers.get("X-Actor-Login") or repo.split("/", 1)[0])
            tenant = _lookup_tenant(store, repo)
            if tenant is None and hasattr(store, "create_tenant"):
                tenant = store.create_tenant(repo, owner)
            if tenant is None:
                return httpx.Response(404, json={"detail": "tenant not registered"}, request=request)
            if dispatcher is not None and hasattr(dispatcher, "seed_repository_labels"):
                try:
                    dispatcher.seed_repository_labels(tenant.repo)
                except Exception as exc:
                    logger.warning("Failed to seed labels on %s: %s", tenant.repo, exc)
            res_body: dict[str, Any] = {
                "id": tenant.id,
                "tenant_id": tenant.id,
                "repo": tenant.repo,
                "status": tenant.status,
                "arbitration_mode": tenant.arbitration_mode,
                "manifest": tenant.manifest,
                "manifest_sha": canonical_sha(tenant.manifest or {}),
            }
            if body.get("dispatch_onboard"):
                issue_num = int(body.get("issue_number") or 0)
                run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
                store.claim_wiki_refresh(tenant.id, tenant.repo, run_id)
                if hasattr(store, "create_run_credential"):
                    store.create_run_credential(run_id, tenant.id, "onboarder", cred_hash, expires_at)
                if dispatcher is not None and hasattr(dispatcher, "dispatch"):
                    dispatcher.dispatch("flow_onboard", {"repo": tenant.repo, "issue_number": issue_num})
                if dispatcher is not None and hasattr(dispatcher, "onboard_tenant"):
                    getattr(dispatcher, "onboard_tenant")(
                        tenant.repo,
                        issue_num,
                        run_id=run_id,
                        run_credential=run_cred,
                    )
                res_body["run_id"] = run_id
                res_body["dispatched"] = True
            return httpx.Response(201, json=res_body, request=request)

        # 4. Tenant-scoped routes: /internal/v1/tenants/{tenant_id}/...
        m_tenant = re.fullmatch(r"/internal/v1/tenants/([^/]+)/(.+)", path)
        if m_tenant:
            tid, subpath = m_tenant.group(1), m_tenant.group(2)
            tenant = _lookup_tenant(store, tid) or _lookup_tenant(store, repo_hdr)
            if tenant is None:
                return httpx.Response(404, json={"detail": "Tenant not found"}, request=request)
            if method == "PUT" and subpath == "arbitration-mode":
                mode = str(body.get("mode") or "")
                if hasattr(store, "set_arbitration_mode"):
                    store.set_arbitration_mode(tenant.id, mode)
                return httpx.Response(200, json={"tenant_id": tenant.id, "arbitration_mode": mode}, request=request)
            if method in ("PATCH", "PUT") and subpath == "assignee":
                login = str(body.get("owner_github_login") or "").strip()
                manifest = dict(tenant.manifest or {})
                manifest["owner_github_login"] = login
                sha = canonical_sha(manifest)
                try:
                    store.update_tenant_manifest(tenant.id, manifest, sha)  # type: ignore[attr-defined]
                except AttributeError:
                    from dataclasses import replace

                    store.tenants[tenant.id] = replace(tenant, manifest=manifest)  # type: ignore[attr-defined]
                return httpx.Response(
                    200,
                    json={
                        "tenant_id": tenant.id,
                        "owner_github_login": login,
                        "manifest": manifest,
                        "manifest_sha": sha,
                    },
                    request=request,
                )
            if method == "PUT" and subpath == "guardrails":
                if hasattr(store, "set_tenant_guardrails"):
                    res = store.set_tenant_guardrails(
                        tenant.id,
                        spot_max_instance_minutes=int(body.get("spot_max_instance_minutes", 60)),
                        max_pr_wall_clock_minutes=int(body.get("max_pr_wall_clock_minutes", 240)),
                        max_pr_cost_usd=float(body.get("max_pr_cost_usd", 25.0)),
                        max_review_rounds=int(body.get("max_review_rounds", 4)),
                        max_repeated_finding=int(body.get("max_repeated_finding", 2)),
                    )
                    return httpx.Response(200, json={"tenant_id": tenant.id, **res}, request=request)
                return httpx.Response(200, json={"tenant_id": tenant.id}, request=request)

            if method == "PUT" and subpath == "goal":
                try:
                    req_goal = GoalUpdateRequest.model_validate(body)
                except Exception as exc:
                    return httpx.Response(422, json={"detail": str(exc)}, request=request)
                if set(req_goal.targets) & set(req_goal.guardrails):
                    return httpx.Response(
                        422,
                        json={"detail": "metric keys cannot be in both targets and guardrails"},
                        request=request,
                    )
                tracking_issue_number = req_goal.tracking_issue_number
                if tracking_issue_number is None and dispatcher is not None and hasattr(dispatcher, "create_issue"):
                    try:
                        created = dispatcher.create_issue(
                            tenant.repo,
                            title=f"[Goal] {req_goal.objective[:80]}",
                            body=f"Tracking issue for repository goal:\n\n{req_goal.objective}",
                            labels=["flow:goal"],
                        )
                        if isinstance(created, dict) and created.get("number"):
                            tracking_issue_number = int(created["number"])
                    except Exception:
                        pass
                if tracking_issue_number is None:
                    return httpx.Response(
                        422,
                        json={"detail": "tracking_issue_number is required when GitHub issue auto-creation is unavailable"},
                        request=request,
                    )
                goal_payload = req_goal.model_dump(mode="json", exclude={"actor"})
                goal_payload["tracking_issue_number"] = tracking_issue_number
                try:
                    res = store.set_tenant_goal(tenant.id, goal_payload)
                except ValueError as exc:
                    return httpx.Response(422, json={"detail": str(exc)}, request=request)
                store.record_security_audit(
                    "goal.updated",
                    tenant_id=tenant.id,
                    actor=req_goal.actor,
                    metadata={
                        "autonomy": req_goal.autonomy,
                        "targets": req_goal.targets,
                        "guardrails": req_goal.guardrails,
                        "tracking_issue_number": tracking_issue_number,
                    },
                )
                return httpx.Response(200, json={"tenant_id": tenant.id, **res}, request=request)

            m_metric = re.fullmatch(r"metrics/([^/]+)(?:/(compile|approve|retire))?", subpath)
            if m_metric:
                key, action = m_metric.group(1), m_metric.group(2)
                if not METRIC_KEY_RE.fullmatch(key):
                    return httpx.Response(422, json={"detail": "Invalid metric key format"}, request=request)
                if method == "PUT" and action is None:
                    row = {
                        "key": key,
                        "intent": body.get("intent", ""),
                        "kind": body.get("kind", "guardrail"),
                        "rationale": body.get("rationale", ""),
                        "owner_login": body.get("actor", "admin"),
                        "status": "draft",
                        "source": None,
                        "comparator": None,
                        "threshold": None,
                        "compiled": None,
                        "digest": None,
                        "dry_run": None,
                    }
                    if hasattr(store, "upsert_metric_definition"):
                        row = store.upsert_metric_definition(tenant.id, row)
                    return httpx.Response(200, json={"status": "draft", "key": key, "metric": row}, request=request)
                if method == "POST" and action == "compile":
                    run_id, _, cred_hash, expires_at = generate_run_credentials()
                    if hasattr(store, "create_run_credential"):
                        store.create_run_credential(
                            run_id, tenant.id, "metric_compiler", cred_hash, expires_at, metric_key=key
                        )
                    return httpx.Response(
                        200,
                        json={"status": "dispatched", "dispatched": True, "role": "metric_compiler", "key": key, "run_id": run_id},
                        request=request,
                    )
                if method == "POST" and action == "approve":
                    digest = str(body.get("digest") or "")
                    actor = str(body.get("actor") or "admin")
                    if hasattr(store, "approve_metric_atomic"):
                        try:
                            store.approve_metric_atomic(
                                tenant.id,
                                key,
                                digest,
                                actor,
                                dict(tenant.manifest or {}),
                                canonical_sha(tenant.manifest or {}),
                            )
                            if hasattr(store, "rebuild_tenant_manifest"):
                                store.rebuild_tenant_manifest(tenant.id)
                        except LookupError:
                            return httpx.Response(404, json={"detail": f"Metric {key} not found"}, request=request)
                        except ValueError as exc:
                            if "digest_mismatch" in str(exc):
                                return httpx.Response(409, json={"detail": "digest_mismatch"}, request=request)
                            return httpx.Response(422, json={"detail": str(exc)}, request=request)
                    return httpx.Response(200, json={"status": "active", "key": key}, request=request)
                if method == "POST" and action == "retire":
                    if hasattr(store, "retire_metric"):
                        try:
                            store.retire_metric(tenant.id, key)
                        except ValueError as exc:
                            return httpx.Response(409, json={"detail": str(exc)}, request=request)
                    if hasattr(store, "rebuild_tenant_manifest"):
                        store.rebuild_tenant_manifest(tenant.id)
                    return httpx.Response(200, json={"status": "retired", "key": key}, request=request)

            if method == "POST" and subpath == "wiki/regenerate":
                actor_name = str(body.get("actor") or request.headers.get("X-Actor-Login") or "admin")
                run_id, run_cred, cred_hash, expires_at = generate_run_credentials()
                claim_res = store.claim_wiki_refresh(tenant.id, tenant.repo, run_id)
                if not claim_res.get("claimed"):
                    active_run_id = str(claim_res.get("active_run_id") or "")
                    existing_onb = store.active_session_for_role(tenant.id, "onboarder")
                    res_q = {
                        "status": "queued",
                        "dispatched": False,
                        "queued": True,
                        "role": "onboarder",
                        "run_id": active_run_id or str((existing_onb or {}).get("run_id") or ""),
                        "instance_id": str((existing_onb or {}).get("instance_id") or ""),
                    }
                    store.record_security_audit(
                        "wiki.regenerate_requested",
                        tenant_id=tenant.id,
                        actor=actor_name,
                        run_id=active_run_id or None,
                        metadata={"status": "queued", "queued": True},
                    )
                    return httpx.Response(200, json=res_q, request=request)

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
                if dispatcher is not None and hasattr(dispatcher, "dispatch"):
                    dispatcher.dispatch(
                        "flow_run",
                        {
                            "role": "onboarder",
                            "target_repo": tenant.repo,
                            "issue_id": 0,
                            "pr": 0,
                            "label": "flow:wiki-refresh",
                            "run_id": run_id,
                            "run_credential": run_cred,
                        },
                    )
                store.record_security_audit(
                    "wiki.regenerate_requested",
                    tenant_id=tenant.id,
                    actor=actor_name,
                    run_id=run_id,
                    metadata={"status": "dispatched", "queued": False},
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "dispatched",
                        "dispatched": True,
                        "queued": False,
                        "role": "onboarder",
                        "run_id": run_id,
                        "instance_id": f"pending-{run_id}",
                    },
                    request=request,
                )

            if method == "POST" and subpath == "wiki/policy":
                try:
                    req_pol = WikiPolicyUpdateRequest.model_validate(body)
                except Exception as exc:
                    return httpx.Response(422, json={"detail": str(exc)}, request=request)
                updated_wiki = store.update_wiki_state(
                    tenant.id,
                    tenant.repo,
                    {"refresh_interval_merges": req_pol.refresh_interval_merges},
                )
                store.record_security_audit(
                    "wiki.policy_updated",
                    tenant_id=tenant.id,
                    actor=req_pol.actor,
                    metadata={"refresh_interval_merges": req_pol.refresh_interval_merges},
                )
                return httpx.Response(
                    200,
                    json={
                        "tenant_id": tenant.id,
                        "refresh_interval_merges": req_pol.refresh_interval_merges,
                        "wiki": updated_wiki,
                    },
                    request=request,
                )

        # 5. Run-scoped routes: /internal/v1/runs/{run_id}/...
        m_run = re.fullmatch(r"/internal/v1/runs/([^/]+)/(.+)", path)
        if m_run:
            run_id, subpath = m_run.group(1), m_run.group(2)
            rec = store.get_run_credential(run_id) if hasattr(store, "get_run_credential") else None
            tenant = _lookup_tenant(store, str((rec or {}).get("tenant_id") or tid_hdr or repo_hdr))
            if tenant is None:
                return httpx.Response(404, json={"detail": f"Run {run_id} not found"}, request=request)

            if method == "POST" and subpath == "heartbeat":
                role = str((rec or {}).get("role") or body.get("role") or "developer")
                halted_reason = body.get("halt_detail") or body.get("halt_reason")
                session_row: dict[str, Any] = {
                    "run_id": run_id,
                    "tenant_id": tenant.id,
                    "role": role,
                    "status": body.get("status", "Running"),
                    "current_todo": body.get("current_todo"),
                    "last_tool": body.get("last_tool"),
                    "cost_estimate": body.get("cost_estimate"),
                    "instance_id": body.get("instance_id", ""),
                    "target_repo": tenant.repo,
                    "issue_id": body.get("issue_id", 0),
                    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "agent_runtime": body.get("agent_runtime", "nanobot"),
                    "iterations": body.get("iterations", 0),
                    "usage": body.get("usage"),
                    "last_activity_at": body.get("last_activity_at"),
                    "head_sha": body.get("head_sha"),
                    "halt_reason": body.get("halt_reason"),
                    "halt_detail": body.get("halt_detail"),
                }
                if halted_reason:
                    session_row["halted_reason"] = f"Halted: {halted_reason}"
                if hasattr(store, "update_session"):
                    store.update_session(f"pending-{run_id}", {"status": "dispatched"})
                if hasattr(store, "upsert_session"):
                    store.upsert_session(session_row)
                elif hasattr(store, "update_session"):
                    store.update_session(
                        str(body.get("instance_id") or ""),
                        {
                            "status": body.get("status"),
                            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                            "usage": body.get("usage"),
                            "agent_runtime": body.get("agent_runtime"),
                            "iterations": body.get("iterations"),
                            "halted_reason": f"Halted: {halted_reason}" if halted_reason else None,
                        },
                    )
                hb_issue = int(body.get("issue_id") or 0)
                if (
                    str(body.get("status") or "").startswith("Halted")
                    and hb_issue > 0
                    and role in ("planner", "developer", "evaluator", "arbitrator", "conductor")
                    and hasattr(store, "loop_rounds_for_pr")
                    and hasattr(store, "record_loop_round")
                ):
                    existing_rounds = store.loop_rounds_for_pr(tenant.id, hb_issue)
                    already_recorded = any(
                        isinstance(r.get("usage_detail"), dict) and r["usage_detail"].get("run_id") == run_id
                        for r in existing_rounds
                    )
                    body_usage = body.get("usage")
                    raw_usage: dict[str, Any] = dict(body_usage) if isinstance(body_usage, dict) else {}
                    tot_raw = raw_usage.get("total")
                    tot: dict[str, Any] = dict(tot_raw) if isinstance(tot_raw, dict) else {}
                    in_tok = int(tot.get("input_tokens") or raw_usage.get("input_tokens") or 0)
                    out_tok = int(tot.get("output_tokens") or raw_usage.get("output_tokens") or 0)
                    tokens_val = int(tot.get("total_tokens") or raw_usage.get("total_tokens") or (in_tok + out_tok))
                    cost_val = float(
                        tot.get("cost_usd") or raw_usage.get("cost_usd") or body.get("cost_estimate") or 0.0
                    )
                    if not already_recorded:
                        next_round = max((int(r.get("round", 0)) for r in existing_rounds), default=-1) + 1
                        raw_usage.setdefault("agent_runtime", str(body.get("agent_runtime") or "nanobot"))
                        raw_usage.setdefault(
                            "total",
                            {
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "total_tokens": tokens_val,
                                "cost_usd": cost_val,
                            },
                        )
                        raw_usage.setdefault("calls", [])
                        raw_usage["run_id"] = run_id
                        if body.get("halt_reason"):
                            raw_usage["halt_reason"] = body["halt_reason"]
                        if halted_reason:
                            raw_usage["halt_detail"] = halted_reason
                        raw_sha = str(body.get("head_sha") or "")
                        valid_sha = (
                            raw_sha.lower()
                            if (len(raw_sha) == 40 and all(c in "0123456789abcdef" for c in raw_sha.lower()))
                            else ("0" * 40)
                        )
                        store.record_loop_round(
                            {
                                "tenant_id": tenant.id,
                                "pr_number": hb_issue,
                                "round": max(0, next_round),
                                "head_sha": valid_sha,
                                "role": role,
                                "tokens": tokens_val,
                                "cost_usd": cost_val,
                                "wall_clock_minutes": 0.01,
                                "envelope_digest": f"halted:{body.get('halt_reason') or 'error'}",
                                "usage_detail": raw_usage,
                            }
                        )
                    goal_spend_key = f"goal-spend:{run_id}"
                    if cost_val > 0.0 and not store.has_processed_event(tenant.id, goal_spend_key):
                        store.record_goal_spend(tenant.id, float(cost_val))
                        store.record_processed_event(
                            tenant.id,
                            goal_spend_key,
                            payload={"run_id": run_id, "cost_usd": cost_val},
                            kind="goal_spend",
                            sender="heartbeat",
                        )
                return httpx.Response(200, json={"status": "ok"}, request=request)

            if method == "POST" and subpath == "events":
                events = list(body.get("events") or [])
                if len(events) > 500:
                    return httpx.Response(413, json={"detail": "Batch size exceeds 500 events limit"}, request=request)
                rows: list[dict[str, Any]] = []
                for evt in events:
                    if len(json.dumps(evt.get("payload") or {}).encode("utf-8")) > 65536:
                        return httpx.Response(
                            422, json={"detail": "Event payload exceeds 64KB limit"}, request=request
                        )
                    r: dict[str, Any] = {
                        "run_id": run_id,
                        "seq": evt["seq"],
                        "kind": evt["kind"],
                        "payload": evt.get("payload") or {},
                    }
                    if evt.get("ts"):
                        r["ts"] = evt["ts"]
                    rows.append(r)
                if hasattr(store, "record_run_events"):
                    store.record_run_events(run_id, rows)
                return httpx.Response(200, json={"status": "ok", "count": len(rows)}, request=request)

            if method == "POST" and subpath == "turn-complete":
                bot_logins = get_bot_logins()
                role = str((rec or {}).get("role") or "developer")
                sender = bot_logins.get(role, "atobar-flow-agent[bot]")
                goal_plan = None
                if role == "planner":
                    try:
                        goal_plan = parse_and_validate_goal_plan(
                            str(body.get("comment_body") or ""),
                            author_login=sender,
                            bots=bot_logins,
                            manifest=tenant.manifest,
                            force_goal_mode=bool((rec or {}).get("goal_mode")),
                        )
                    except ValueError as exc:
                        return httpx.Response(422, json={"detail": str(exc)}, request=request)
                sess = store.get_session_by_run(run_id) if hasattr(store, "get_session_by_run") else None
                goal_tracking_issue = ((tenant.manifest or {}).get("goal") or {}).get("tracking_issue_number")
                issue_number = (
                    body.get("issue_number")
                    or (int(sess["issue_id"]) if sess and sess.get("issue_id") else None)
                    or (int(goal_tracking_issue) if goal_tracking_issue else None)
                )
                if issue_number is None:
                    parsed_env = goal_plan or parse(
                        str(body.get("comment_body") or ""), author_login=sender, bots=bot_logins
                    )
                    if parsed_env is not None:
                        issue_number = parsed_env.pr
                if issue_number is None:
                    return httpx.Response(
                        400, json={"detail": "Unable to determine issue_number for turn-complete"}, request=request
                    )
                instance_id = str(
                    body.get("instance_id")
                    or (sess["instance_id"] if sess and sess.get("instance_id") else f"spot-{run_id}")
                )
                if body.get("usage") and sess and hasattr(store, "upsert_session"):
                    store.upsert_session(
                        {**sess, "usage": body["usage"], "heartbeat_at": datetime.now(timezone.utc).isoformat()}
                    )
                gh_adapter = _LocalGitHubAdapter(
                    dispatcher=dispatcher,
                    store=store,
                    repo=tenant.repo,
                    pr_number=int(issue_number),
                    head_sha=str(body.get("head_sha") or ""),
                    exclude_comment_id=body.get("comment_id"),
                )
                if goal_plan is None:
                    parsed_turn_env = parse(
                        str(body.get("comment_body") or ""),
                        author_login=sender,
                        bots=bot_logins,
                    )
                    if parsed_turn_env is None:
                        if sess:
                            store.upsert_session(
                                {**sess, "status": "Complete", "heartbeat_at": datetime.now(timezone.utc).isoformat()}
                            )
                        return httpx.Response(
                            200,
                            json={"action": "shutdown", "reason": "no_envelope"},
                            request=request,
                        )
                    if parsed_turn_env.run_id != run_id or parsed_turn_env.role != role:
                        return httpx.Response(
                            403,
                            json={"detail": "Envelope run_id or role does not match authenticated run credential"},
                            request=request,
                        )
                if body.get("comment_id") is not None and rec is not None:
                    if not store.claim_run_credential_envelope(run_id, int(body["comment_id"])):
                        return httpx.Response(
                            409,
                            json={"detail": f"Run {run_id} has already claimed a different envelope comment"},
                            request=request,
                        )

                if goal_plan is not None:
                    dedup_key = f"goal-plan:{run_id}"
                    if store.has_processed_event(tenant.id, dedup_key):
                        store.clear_goal_planning(tenant.id, run_id)
                        return httpx.Response(
                            200,
                            json={"action": "terminate", "reason": "goal_planned", "created_issues": [], "deduped": True},
                            request=request,
                        )
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
                    if sess and hasattr(store, "upsert_session"):
                        store.upsert_session(
                            {**sess, "status": "Complete", "heartbeat_at": datetime.now(timezone.utc).isoformat()}
                        )
                    return httpx.Response(
                        200,
                        json={"action": "terminate", "reason": "goal_planned", "created_issues": created_issues},
                        request=request,
                    )
                webhook_event: dict[str, Any] = {
                    "kind": "issue_comment.created",
                    "issue_number": int(issue_number),
                    "comment_body": str(body.get("comment_body") or ""),
                    "comment_id": body.get("comment_id"),
                    "sender": sender,
                }
                event_id = compute_event_id(webhook_event)
                raw_event = dict(webhook_event)
                if body.get("trigger_label"):
                    raw_event["trigger_label"] = body["trigger_label"]
                if body.get("wall_clock_minutes"):
                    raw_event["wall_clock_minutes"] = body["wall_clock_minutes"]

                comment_entry = {
                    "id": body.get("comment_id"),
                    "user": {"login": sender},
                    "body": str(body.get("comment_body") or ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                if hasattr(dispatcher, "comments") and isinstance(getattr(dispatcher, "comments"), dict):
                    c_list = getattr(dispatcher, "comments").setdefault((tenant.repo, int(issue_number)), [])
                    if not any(c.get("id") is not None and c.get("id") == body.get("comment_id") for c in c_list):
                        c_list.append(comment_entry)
                else:
                    turn_comments = getattr(store, "_turn_comments", None)
                    if not isinstance(turn_comments, dict):
                        turn_comments = {}
                        try:
                            setattr(store, "_turn_comments", turn_comments)
                        except Exception:
                            pass
                    turn_comments.setdefault((tenant.repo, int(issue_number)), []).append(comment_entry)

                res = process_flow_event(
                    payload={"tenant": tenant.repo, "event_id": event_id, "event": raw_event},
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
                            {**sess, "status": "Complete", "heartbeat_at": datetime.now(timezone.utc).isoformat()}
                        )
                    return httpx.Response(200, json=handover, request=request)
                reason = str(res.get("new_status") or res.get("reason") or "complete")
                if sess and hasattr(store, "upsert_session"):
                    terminal_status = "Halted: escalated" if reason == "escalated" else "Complete"
                    store.upsert_session(
                        {**sess, "status": terminal_status, "heartbeat_at": datetime.now(timezone.utc).isoformat()}
                    )
                return httpx.Response(200, json={"action": "shutdown", "reason": reason}, request=request)

            if method == "POST" and subpath == "arbitration-decisions":
                manifest_sha = canonical_sha(dict(tenant.manifest or {}))
                if hasattr(store, "insert_arbitration_decision"):
                    store.insert_arbitration_decision(
                        {
                            "tenant_id": tenant.id,
                            "pr_number": body["pr_number"],
                            "head_sha": body["head_sha"],
                            "mode": body["mode"],
                            "decision": body["decision"],
                            "reasons": body.get("reasons") or "",
                            "manifest_sha": manifest_sha,
                        }
                    )
                return httpx.Response(200, json={"status": "ok", "manifest_sha": manifest_sha}, request=request)

            if method == "GET" and subpath == "arbitration-data":
                raw_obs = list(store.recent_observations(tenant.id)) if hasattr(store, "recent_observations") else []
                if hasattr(store, "default_branch_observations"):
                    seen_sigs = {
                        (
                            o.get("key"),
                            o.get("commit_sha"),
                            o.get("pr_number") if o.get("pr_number") is not None else o.get("pr"),
                            o.get("run_id") or o.get("check_run_id"),
                            o.get("run_attempt"),
                        )
                        for o in raw_obs
                    }
                    for b in store.default_branch_observations(tenant.id):
                        sig = (
                            b.get("key"),
                            b.get("commit_sha"),
                            b.get("pr_number") if b.get("pr_number") is not None else b.get("pr"),
                            b.get("run_id") or b.get("check_run_id"),
                            b.get("run_attempt"),
                        )
                        if sig not in seen_sigs:
                            raw_obs.append(b)
                            seen_sigs.add(sig)
                recent: list[dict[str, Any]] = []
                for o in raw_obs:
                    obs_pr = o.get("pr_number") if o.get("pr_number") is not None else o.get("pr")
                    recent.append(
                        {
                            "key": o.get("key", ""),
                            "value": float(o.get("value", 0.0)),
                            "workflow_path": o.get("workflow_path", ""),
                            "commit_sha": o.get("commit_sha", ""),
                            "pr_number": obs_pr,
                            "run_id": o.get("run_id") if o.get("run_id") is not None else o.get("check_run_id"),
                            "run_attempt": o.get("run_attempt"),
                            "observed_at": o.get("observed_at"),
                            "artifact_sha256": o.get("artifact_sha256"),
                            "manifest_sha": o.get("manifest_sha"),
                        }
                    )
                manifest = dict(tenant.manifest or {})
                return httpx.Response(
                    200,
                    json={
                        "tenant_id": tenant.id,
                        "arbitration_mode": tenant.arbitration_mode,
                        "manifest": manifest,
                        "manifest_sha": canonical_sha(manifest),
                        "recent_observations": recent,
                        "pr_observations": [o for o in recent if o.get("pr_number") is not None],
                        "baseline_observations": [o for o in recent if o.get("pr_number") is None],
                    },
                    request=request,
                )

            if method == "GET" and subpath == "wiki":
                wiki = store.wiki_for_tenant(tenant.id) if hasattr(store, "wiki_for_tenant") else None
                if wiki is None:
                    return httpx.Response(404, json={"detail": "wiki not generated yet"}, request=request)
                return httpx.Response(200, json=dict(wiki), request=request)

            if method == "POST" and subpath == "wiki":
                role_val = str((rec or {}).get("role") or "")
                if role_val != "onboarder":
                    return httpx.Response(
                        403,
                        json={"detail": "Only onboarder runs can update repository wiki"},
                        request=request,
                    )
                if hasattr(store, "upsert_wiki"):
                    store.upsert_wiki(tenant.id, tenant.repo, body)
                return httpx.Response(200, json={"ok": True, "repo_name": tenant.repo}, request=request)

            if method == "POST" and subpath == "github-token":
                stored_role = str((rec or {}).get("role") or "").strip().lower()
                if stored_role not in ROLE_GITHUB_PERMISSIONS:
                    return httpx.Response(
                        403,
                        json={"detail": f"Unsupported role for GitHub token minting: {stored_role}"},
                        request=request,
                    )
                if not _GITHUB_TOKEN_MINT_LIMITER.try_acquire(run_id):
                    return httpx.Response(
                        429,
                        json={"detail": "Rate limit exceeded: at most 1 github-token mint per 60 seconds per run"},
                        request=request,
                    )
                if dispatcher is not None and hasattr(dispatcher, "mint_repo_token"):
                    try:
                        minted = dispatcher.mint_repo_token(tenant.repo, stored_role)
                    except PermissionError as exc:
                        return httpx.Response(403, json={"detail": str(exc)}, request=request)
                else:
                    minted = {"token": "ghs_fallback_token", "expires_at": ""}
                return httpx.Response(
                    200,
                    json={"token": str(minted["token"]), "expires_at": str(minted.get("expires_at") or "")},
                    request=request,
                )

            if method == "POST" and subpath == "credential-used":
                inst_id = str(body.get("instance_id") or "").strip()
                if inst_id and hasattr(store, "claim_run_credential_instance"):
                    if not store.claim_run_credential_instance(run_id, inst_id):
                        return httpx.Response(
                            403,
                            json={"detail": "Run credential already claimed by another instance"},
                            request=request,
                        )
                if hasattr(store, "mark_run_credential_used"):
                    store.mark_run_credential_used(run_id)
                return httpx.Response(200, json={"status": "ok"}, request=request)

            if method == "POST" and subpath == "metric-compilation":
                key = str(body.get("key") or "")
                compiled = dict(body.get("compiled") or {})
                run_cmd = str(compiled.get("run") or "").strip()
                raw_comp = str(compiled.get("comparator") or "")
                raw_thr = float(compiled.get("threshold") or 0.0)
                metric_dict: dict[str, Any] = {
                    "key": key,
                    "run": run_cmd,
                    "comparator": raw_comp,
                    "threshold": raw_thr,
                    "baseline_mode": str(compiled.get("baseline_mode") or "absolute"),
                    "min_samples": int(compiled.get("min_samples") or 1),
                }
                Metric.model_validate(metric_dict)
                digest = canonical_sha(metric_dict)
                dry_run = {
                    "value": float(body.get("value") or 0.0),
                    "sha": str(body.get("sha") or ""),
                    "stdout_tail": str(body.get("stdout_tail") or ""),
                }
                if hasattr(store, "upsert_metric_definition"):
                    existing = (
                        store.get_metric_definition(tenant.id, key)
                        if hasattr(store, "get_metric_definition")
                        else {}
                    ) or {}
                    store.upsert_metric_definition(
                        tenant.id,
                        {
                            **existing,
                            "key": key,
                            "status": "compiled",
                            "source": {"kind": "command", "run": run_cmd},
                            "comparator": raw_comp,
                            "threshold": raw_thr,
                            "compiled": metric_dict,
                            "digest": digest,
                            "dry_run": dry_run,
                        },
                    )
                return httpx.Response(
                    200,
                    json={"status": "compiled", "key": key, "digest": digest, "compiled": metric_dict, "dry_run": dry_run},
                    request=request,
                )

        if method == "POST" and path == "/internal/v1/gc/sweep":
            active = store.active_sessions() if hasattr(store, "active_sessions") else []
            reaped_ids: list[str] = []
            for sess in active:
                inst_id = str(sess.get("instance_id") or "")
                if not inst_id:
                    continue
                if hasattr(store, "update_session"):
                    store.update_session(inst_id, {"status": "Halted", "halted_reason": "stale/reaped"})
                r_id = sess.get("run_id")
                if r_id and hasattr(store, "mark_run_credential_used"):
                    store.mark_run_credential_used(str(r_id))
                repo_name = str(sess.get("target_repo") or "")
                issue_num = int(sess.get("issue_id") or 0)
                if repo_name and issue_num > 0 and dispatcher is not None and hasattr(dispatcher, "create_issue_comment"):
                    try:
                        dispatcher.create_issue_comment(
                            repo_name,
                            issue_num,
                            f"🌊 **flow Event Bus:** session `{inst_id}` terminated by GC sweep (stale heartbeat).",
                        )
                    except Exception:
                        pass
                reaped_ids.append(inst_id)
            return httpx.Response(
                200,
                json={"reaped_sessions": reaped_ids, "count": len(reaped_ids)},
                request=request,
            )

        return httpx.Response(404, json={"detail": f"Unknown internal route {method} {path}"}, request=request)

    return httpx.MockTransport(_handler)


def _optional_dispatcher() -> Optional[Dispatcher]:
    try:
        from control_panel.app import get_dispatcher

        return get_dispatcher()
    except Exception:
        return None


def get_flow_engine_client(
    store: Store = Depends(get_store),
) -> FlowEngineClient:
    fe_url = (os.environ.get("FE_URL") or os.environ.get("FLOW_ENGINE_URL") or "").strip()
    from control_panel import app as cp_app
    from control_panel.store import SupabaseStore

    if fe_url or isinstance(store, SupabaseStore) or cp_app.get_store not in cp_app.app.dependency_overrides:
        return FlowEngineClient(
            base_url=fe_url or DEFAULT_FE_URL,
            internal_token=os.environ.get("FE_INTERNAL_TOKEN", ""),
        )
    disp_override = cp_app.app.dependency_overrides.get(cp_app.get_dispatcher)
    dispatcher = disp_override() if disp_override is not None else _optional_dispatcher()
    return FlowEngineClient(
        base_url=DEFAULT_FE_URL,
        internal_token=os.environ.get("FE_INTERNAL_TOKEN", "local-fallback-token"),
        transport=_build_local_fallback_transport(store, dispatcher),
    )
