"""FastAPI surface. Tenant identity comes only from the bearer token, never from the request body."""

import base64
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import time
import uuid
from functools import lru_cache
from typing import Annotated, Any, Literal, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from atobar_contracts.api import (
    ArbitrationDecisionPayload,
    CreateTenantRequest,
    InternalEventRequest,
    InternalMetricsRequest,
    MetricCompilationPayload,
    RunEventsBatchPayload,
    TurnCompletePayload,
)
from atobar_contracts.providers import canonical_provider
from control_panel import llm_keys, web
from control_panel.auth import require_run_credential
from control_panel.client_templates import render_onboard_script, render_trigger
from control_panel.flow_engine_client import FlowEngineClient, get_flow_engine_client
from control_panel.github import Dispatcher, GitHubAppDispatcher, get_dispatcher
from control_panel.heartbeat import router as heartbeat_router
from control_panel.store import Store, Tenant, get_store
from control_panel.tokens import (
    TOKEN_PREFIX,
    decrypt_screenshot_bytes,
    digest_matches,
    encrypt_screenshot_bytes,
    hash_token,
    mint_token,
    sign_screenshot_url,
    verify_screenshot_signature,
)
from control_panel.web import router as web_router

logger = logging.getLogger("control_panel.app")

REPO_RE = re.compile(r"(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+")

_DOCS = os.environ.get("CP_ENABLE_DOCS") == "1"
app = FastAPI(
    title="atobar-flow Control Panel",
    version="0.2.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
app.include_router(web_router)
app.include_router(heartbeat_router)


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


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    req_id = getattr(request.state, "request_id", None) or (request.headers.get("X-Request-ID") or "").strip()
    headers = dict(exc.headers or {})
    if req_id:
        headers["X-Request-ID"] = req_id
    if (
        exc.status_code == status.HTTP_502_BAD_GATEWAY
        and isinstance(exc.detail, dict)
        and exc.detail.get("error") == "flow_engine_unavailable"
    ):
        err_req_id = str(exc.detail.get("request_id") or req_id or "")
        payload = {
            "error": "flow_engine_unavailable",
            "request_id": err_req_id,
            "detail": {"error": "flow_engine_unavailable", "request_id": err_req_id},
        }
        return JSONResponse(status_code=exc.status_code, content=payload, headers=headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)


def _req_id(request: Request) -> str:
    val = getattr(request.state, "request_id", None) or (request.headers.get("X-Request-ID") or "").strip()
    return str(val) if val else str(uuid.uuid4())


def _raw_bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return None


def get_admin_digest() -> str:
    return os.environ.get("CP_ADMIN_TOKEN_SHA256", "")


def get_jwk_client() -> Any:
    return None


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return authorization.removeprefix("Bearer ").strip()


def current_tenant(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: Store = Depends(get_store),
    jwk_client: Any = Depends(get_jwk_client),
) -> Tenant:
    token = _bearer(authorization)
    tenant: Optional[Tenant] = None

    if token.startswith(TOKEN_PREFIX):
        tenant = store.tenant_for_token_hash(hash_token(token))
    else:
        try:
            from control_panel.oidc import verify_github_oidc_token

            claims = verify_github_oidc_token(token, jwk_client=jwk_client)
            repo_id = str(claims.get("repository_id", ""))
            if repo_id:
                tenant = store.tenant_for_repository_id(repo_id)
            if tenant is None:
                repo = str(claims.get("repository", ""))
                if repo:
                    tenant = store.tenant_by_repo(repo)
        except Exception:
            tenant = None

    if tenant is None or tenant.status == "offboarded":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid tenant token")
    request.state.tenant = tenant.repo
    return tenant


def require_admin(authorization: Optional[str] = Header(default=None), digest: str = Depends(get_admin_digest)) -> None:
    if not digest_matches(_bearer(authorization), digest):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/v1/admin/status")
def admin_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    info: dict[str, Any] = {"status": "ok"}
    deploy_path = pathlib.Path(os.environ.get("DEPLOY_JSON_PATH", "/etc/atobar/deploy.json"))
    if deploy_path.exists():
        try:
            info.update(json.loads(deploy_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return info


@app.get("/onboard.sh")
def onboard_script() -> Response:
    from control_panel.client_templates import resolve_cp_url

    cp_url = resolve_cp_url()
    app_slug = os.environ.get("CP_GITHUB_APP_SLUG", "atobar-flow-agent")
    return Response(content=render_onboard_script(cp_url=cp_url, app_slug=app_slug), media_type="text/x-shellscript")


@app.get("/onboard/flow-trigger.yml", response_class=PlainTextResponse)
def onboard_trigger(repo: str = Query(..., max_length=140)) -> str:
    from control_panel.client_templates import resolve_cp_url

    if not REPO_RE.fullmatch(repo):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "repo must be owner/name")
    return render_trigger(repo.replace("/", "__"), resolve_cp_url())


@app.get("/v1/tenant")
def read_tenant(tenant: Tenant = Depends(current_tenant)) -> dict[str, Any]:
    return {
        "repo": tenant.repo,
        "status": tenant.status,
        "arbitration_mode": tenant.arbitration_mode,
        "manifest": tenant.manifest,
    }


@app.get("/v1/tenant/wiki")
def read_wiki(tenant: Tenant = Depends(current_tenant), store: Store = Depends(get_store)) -> dict[str, Any]:
    wiki = store.wiki_for_tenant(tenant.id)
    if wiki is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "wiki not generated yet")
    return wiki


class IssueEvent(BaseModel):
    kind: Literal["issues.labeled", "issues.edited"]
    issue_number: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=100)
    sender: Optional[str] = None
    issue_body: Optional[str] = None


class PullRequestEvent(BaseModel):
    kind: Literal["pull_request.opened", "pull_request.synchronize", "pull_request.closed"]
    pr_number: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_sha: Optional[str] = None
    sender: Optional[str] = None
    merged: Optional[bool] = False


class IssueCommentEvent(BaseModel):
    kind: Literal[
        "issue_comment.created",
        "pull_request_review.submitted",
        "pull_request_review_comment.created",
    ]
    issue_number: int = Field(gt=0)
    pr_number: Optional[int] = Field(default=None, gt=0)
    head_sha: Optional[str] = None
    comment_body: str
    comment_id: Optional[int] = None
    sender: Optional[str] = None
    author_association: Optional[str] = None
    is_pull_request: Optional[bool] = None


class CheckRunEvent(BaseModel):
    kind: Literal["check_run.completed"]
    check_name: str
    conclusion: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: Optional[int] = None


ClientEvent = Annotated[
    Union[IssueEvent, PullRequestEvent, IssueCommentEvent, CheckRunEvent],
    Field(discriminator="kind"),
]

IssueLabeledEvent = IssueEvent


@app.post("/v1/events", status_code=status.HTTP_202_ACCEPTED)
def ingest_event(
    event: ClientEvent,
    request: Request,
    tenant: Tenant = Depends(current_tenant),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    request.state.tenant = tenant.repo
    if tenant.status not in ("active", "onboarding"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"tenant is {tenant.status}")

    if isinstance(event, IssueEvent) and event.label == "flow:onboard":
        if hasattr(store, "active_tokens") and hasattr(store, "add_token"):
            if not store.active_tokens(tenant.id):
                tok = mint_token()
                store.add_token(tenant.id, hash_token(tok), tenant.repo.split("/", 1)[0], "ci")

    delivery_id = (
        request.headers.get("X-GitHub-Delivery")
        or request.headers.get("X-Event-ID")
        or None
    )
    req_payload = InternalEventRequest(
        tenant=tenant.repo,
        tenant_id=tenant.id,
        delivery_id=delivery_id,
        event=event.model_dump(exclude_none=True),
    )
    return fe.post_events(
        req_payload,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
    )


class MetricReport(BaseModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pr_number: Optional[int] = Field(default=None, gt=0)
    run_id: Optional[int] = Field(default=None, gt=0)
    run_attempt: Optional[int] = Field(default=1, ge=1)
    workflow_path: str = Field(min_length=1, max_length=260)
    values: dict[str, float] = Field(default_factory=dict)


@app.post("/v1/metrics", status_code=status.HTTP_201_CREATED)
def ingest_metrics(
    report: MetricReport,
    request: Request,
    tenant: Tenant = Depends(current_tenant),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    request.state.tenant = tenant.repo
    if tenant.status not in ("active", "onboarding"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"tenant is {tenant.status}")
    if not all(math.isfinite(v) for v in report.values.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "metric values must be finite")

    req_payload = InternalMetricsRequest(
        tenant_id=tenant.id,
        tenant=tenant.repo,
        commit_sha=report.commit_sha,
        pr_number=report.pr_number,
        run_id=report.run_id,
        run_attempt=report.run_attempt or 1,
        workflow_path=report.workflow_path,
        values=report.values,
    )
    return fe.post_metrics(
        req_payload,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
    )


class TokenRequest(BaseModel):
    owner_github_login: str = Field(min_length=1, max_length=39)
    label: str = Field(default="ci", max_length=64)


def _tenant_or_404(owner: str, repo: str, store: Store) -> Tenant:
    full = f"{owner}/{repo}"
    if not REPO_RE.fullmatch(full):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid repo")
    tenant = store.tenant_by_repo(full)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not registered")
    return tenant


@app.post("/v1/admin/gc/sweep")
def admin_gc_sweep(
    request: Request,
    _: None = Depends(require_admin),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    return fe.post_gc_sweep(request_id=_req_id(request))


@app.post("/v1/admin/tenants/{owner}/{repo}/onboard", status_code=status.HTTP_201_CREATED)
def admin_onboard_tenant(
    owner: str,
    repo: str,
    request: Request,
    issue_number: int = 0,
    _: None = Depends(require_admin),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    full = f"{owner}/{repo}"
    if not REPO_RE.fullmatch(full):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid repo")
    request.state.tenant = full

    fe_res = fe.create_tenant(
        CreateTenantRequest(
            repo=full,
            created_by=owner,
            issue_number=issue_number,
            dispatch_onboard=True,
        ),
        request_id=_req_id(request),
        actor_login=owner,
    )
    tenant_id = str(fe_res.get("tenant_id") or fe_res.get("id") or "")
    minted_token: Optional[str] = None
    if tenant_id and hasattr(store, "add_token"):
        active = store.active_tokens(tenant_id) if hasattr(store, "active_tokens") else []
        if not active:
            minted_token = mint_token()
            store.add_token(tenant_id, hash_token(minted_token), owner, "ci")

    res: dict[str, Any] = {
        "repo": full,
        "tenant_id": tenant_id,
        "run_id": str(fe_res.get("run_id") or ""),
        "dispatched": bool(fe_res.get("dispatched", True)),
    }
    if minted_token:
        res["token"] = minted_token
    return res


@app.post("/v1/admin/tenants/{owner}/{repo}/tokens", status_code=status.HTTP_201_CREATED)
def issue_token(
    owner: str,
    repo: str,
    body: TokenRequest,
    _: None = Depends(require_admin),
    store: Store = Depends(get_store),
) -> dict[str, str]:
    tenant = _tenant_or_404(owner, repo, store)
    token = mint_token()
    store.add_token(tenant.id, hash_token(token), body.owner_github_login, body.label)
    if hasattr(store, "record_security_audit"):
        store.record_security_audit(
            "tenant_token.issued",
            tenant_id=tenant.id,
            actor=body.owner_github_login,
            metadata={"label": body.label},
        )
    return {"repo": tenant.repo, "token": token}


@app.delete("/v1/admin/tenants/{owner}/{repo}/tokens")
def revoke_tokens(
    owner: str, repo: str, _: None = Depends(require_admin), store: Store = Depends(get_store)
) -> dict[str, Any]:
    tenant = _tenant_or_404(owner, repo, store)
    return {"repo": tenant.repo, "revoked": store.revoke_tokens(tenant.id)}


@app.post("/v1/runs/{run_id}/llm-key")
def deliver_run_llm_key(
    run_id: str,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
    kek: Optional[bytes] = Depends(llm_keys.get_kek),
    kms_client: Optional[Any] = Depends(llm_keys.get_kms_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo

    # Record first delivery timestamp via Flow Engine (sole writer of run_credentials)
    if record.get("used_at") is None:
        fe.post_run_credential_used(
            run_id,
            request_id=_req_id(request),
            tenant_id=tenant.id,
            tenant_repo=tenant.repo,
            run_credential=_raw_bearer(request),
        )
        if hasattr(store, "mark_run_credential_used"):
            store.mark_run_credential_used(run_id)

    role = str(record.get("role", "developer"))
    manifest = tenant.manifest or {}
    prefs = (store.role_llm_prefs(tenant.id) if hasattr(store, "role_llm_prefs") else None) or {}
    pref = prefs.get(role) or {}
    if not (pref.get("provider") and pref.get("model")) and role in ("planner", "onboarder", "metric_compiler"):
        pref = prefs.get("developer") or {}
    if not (pref.get("provider") and pref.get("model")):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No model configured for the {role} role of {tenant.repo}; set it in the Control Panel",
        )
    try:
        provider = canonical_provider(pref["provider"])
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid llm provider in role settings")
    role_llm = {"model": pref["model"], "reasoning_effort": pref.get("reasoning_effort") or "none"}

    key_row: Optional[dict[str, Any]] = None
    if hasattr(store, "get_tenant_llm_key_record"):
        key_row = store.get_tenant_llm_key_record(tenant.id, provider)
    if key_row is None and hasattr(store, "get_tenant_llm_ciphertext"):
        ct = store.get_tenant_llm_ciphertext(tenant.id, provider)
        if ct:
            key_row = {"ciphertext": ct, "kek_version": 1}

    if key_row and key_row.get("ciphertext"):
        try:
            api_key = llm_keys.open_llm_key(
                key_row,
                tenant_id=tenant.id,
                provider=provider,
                kek=kek,
                kms_client=kms_client,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
        if hasattr(store, "record_security_audit"):
            store.record_security_audit(
                "llm_key.delivered",
                tenant_id=tenant.id,
                actor=role,
                run_id=run_id,
                metadata={"provider": provider, "role": role},
            )
        return {
            "provider": provider,
            "api_key": api_key,
            **role_llm,
        }

    # Fallback to factory key only if opted in
    fallback_allowed = bool(
        manifest.get("fallback_to_factory_key", False)
        or manifest.get("llm", {}).get("fallback_to_factory_key", False)
    )
    if fallback_allowed:
        factory_key = os.environ.get(f"{provider.upper()}_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
        if factory_key:
            if hasattr(store, "record_security_audit"):
                store.record_security_audit(
                    "llm_key.delivered",
                    tenant_id=tenant.id,
                    actor=role,
                    run_id=run_id,
                    metadata={"provider": provider, "role": role, "fallback": True},
                )
            return {"provider": provider, "api_key": factory_key, **role_llm}
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factory key fallback allowed but no factory key available")

    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        f"No LLM key configured for tenant {tenant.repo} and factory fallback disabled",
    )


@app.post("/v1/runs/{run_id}/github-token")
def mint_run_github_token(
    run_id: str,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, str]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo

    minted = fe.post_run_github_token(
        run_id,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )
    if hasattr(store, "record_security_audit"):
        store.record_security_audit(
            "github_token.minted",
            tenant_id=tenant.id,
            actor=str(record.get("role", "developer")),
            run_id=run_id,
            metadata={"repo": tenant.repo},
        )
    out: dict[str, str] = {
        "token": str(minted["token"]),
        "expires_at": str(minted.get("expires_at") or ""),
    }
    if minted.get("engine_token"):
        out["engine_token"] = str(minted["engine_token"])
    return out


@app.get("/v1/runs/{run_id}/arbitration-data")
def get_arbitration_data(
    run_id: str,
    request: Request,
    pr_number: Optional[int] = Query(default=None),
    commit_sha: Optional[str] = Query(default=None),
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    return fe.get_run_arbitration_data(
        run_id,
        pr_number=pr_number,
        commit_sha=commit_sha,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.get("/v1/runs/{run_id}/wiki")
def get_run_wiki(
    run_id: str,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    return fe.get_run_wiki(
        run_id,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.post("/v1/runs/{run_id}/wiki")
def post_run_wiki(
    run_id: str,
    body: dict[str, Any],
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    return fe.post_run_wiki(
        run_id,
        body,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.post("/v1/runs/{run_id}/turn-complete")
def post_turn_complete(
    run_id: str,
    body: TurnCompletePayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    return fe.post_run_turn_complete(
        run_id,
        body,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.post("/v1/runs/{run_id}/arbitration-decisions")
def post_arbitration_decision(
    run_id: str,
    payload: ArbitrationDecisionPayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    body = payload.model_dump(exclude_none=True)
    body["tenant_id"] = tenant.id
    return fe.post_run_arbitration_decision(
        run_id,
        body,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.post("/v1/runs/{run_id}/events")
def post_run_events(
    run_id: str,
    batch: RunEventsBatchPayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    if len(batch.events) > 500:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Batch size exceeds 500 events limit")
    for evt in batch.events:
        payload_bytes = len(json.dumps(evt.payload).encode("utf-8"))
        if payload_bytes > 65536:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Event payload exceeds 64KB limit")
    return fe.post_run_events(
        run_id,
        batch,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


@app.post("/v1/runs/{run_id}/metric-compilation")
def post_run_metric_compilation(
    run_id: str,
    payload: MetricCompilationPayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    return fe.post_run_metric_compilation(
        run_id,
        payload,
        request_id=_req_id(request),
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=_raw_bearer(request),
    )


_SCREENSHOT_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.png$")
_SCREENSHOT_TTL_SECONDS = 7 * 86400


def _screenshot_cache_dir() -> pathlib.Path:
    base = pathlib.Path(os.environ.get("CP_SCREENSHOT_DIR", "/tmp/atobar-screenshots"))
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _purge_expired_screenshots(base_dir: pathlib.Path) -> None:
    cutoff = time.time() - _SCREENSHOT_TTL_SECONDS
    try:
        for run_dir in base_dir.iterdir():
            if not run_dir.is_dir():
                continue
            for f in run_dir.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


class RunScreenshotUploadPayload(BaseModel):
    filename: str = Field(default="screenshot.png", max_length=120)
    data_base64: str
    caption: Optional[str] = Field(default="Screenshot", max_length=300)
    metadata: Optional[dict[str, Any]] = None


_CMD_SECRET_RE = re.compile(
    r"(?i)(?:--?(?:api[-_]?key|token|secret|password|passwd|auth|bearer|credential)[=\s]+\S+"
    r"|\b(?:sk|ghp|ghs|gho|ghu|github_pat|atb_run|xox[baprs]|AIza)[A-Za-z0-9_.-]+"
    r"|[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:@]+:[^\s@]+@[^\s]+"
    r"|\b[A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|DATABASE_URL|SUPABASE)[A-Z0-9_]*=\S+)"
)


def _redact_command_secrets(text: str) -> str:
    return _CMD_SECRET_RE.sub("[REDACTED]", text or "")


def _sanitize_screenshot_metadata(raw_meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw_meta, dict):
        return {}
    raw_mode = str(raw_meta.get("data_mode") or "undeclared").strip().lower()
    data_mode = raw_mode if raw_mode in {"live", "synthetic", "hybrid", "undeclared"} else "undeclared"
    x11_windows_raw = raw_meta.get("x11_windows")
    x11_windows = (
        [str(w)[:500] for w in x11_windows_raw[:10] if w is not None]
        if isinstance(x11_windows_raw, list)
        else []
    )
    active_procs_raw = raw_meta.get("active_processes")
    active_processes = (
        [_redact_command_secrets(str(p))[:500] for p in active_procs_raw[:10] if p is not None]
        if isinstance(active_procs_raw, list)
        else []
    )
    has_win_raw = raw_meta.get("has_active_window")
    has_active_window = bool(has_win_raw) if has_win_raw is not None else True
    return {
        "data_mode": data_mode,
        "data_source": str(raw_meta.get("data_source") or "")[:500],
        "constraints": str(raw_meta.get("constraints") or "")[:500],
        "scenario": str(raw_meta.get("scenario") or "")[:500],
        "resolution": str(raw_meta.get("resolution") or "")[:64],
        "x11_windows": x11_windows,
        "active_processes": active_processes,
        "has_active_window": has_active_window,
        "captured_at": str(raw_meta.get("captured_at") or "")[:64],
    }


@app.post("/v1/runs/{run_id}/screenshots", status_code=status.HTTP_201_CREATED)
def post_run_screenshot(
    run_id: str,
    payload: RunScreenshotUploadPayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    store: Store = Depends(get_store),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant: Tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo

    fname = os.path.basename(payload.filename.strip() or "screenshot.png")
    if not fname.endswith(".png"):
        fname += ".png"
    if not _SCREENSHOT_FILENAME_RE.fullmatch(fname) or ".." in fname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid screenshot filename")

    try:
        png_bytes = base64.b64decode(payload.data_base64, validate=True)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid base64 PNG data")

    if len(png_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Screenshot exceeds 10MB limit")

    tenant_id = str(record["tenant_id"])
    try:
        encrypted_blob = encrypt_screenshot_bytes(tenant_id, run_id, fname, png_bytes)
        exp = int(time.time()) + _SCREENSHOT_TTL_SECONDS
        sig = sign_screenshot_url(tenant_id, run_id, fname, exp)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    sha256_hex = hashlib.sha256(png_bytes).hexdigest()

    base_dir = _screenshot_cache_dir()
    _purge_expired_screenshots(base_dir)
    run_dir = base_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    target_file: Optional[pathlib.Path] = None
    tmp_file: Optional[pathlib.Path] = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        target_file = run_dir / fname
        tmp_file = run_dir / f".{fname}.{uuid.uuid4().hex}.tmp"
        tmp_file.write_bytes(encrypted_blob)
        os.chmod(tmp_file, 0o600)
    except OSError as exc:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Screenshot storage unavailable") from exc

    evt_payload: dict[str, Any] = {
        "filename": fname,
        "caption": payload.caption or "Screenshot",
        "sha256": sha256_hex,
        "size_bytes": len(png_bytes),
        **_sanitize_screenshot_metadata(payload.metadata),
    }
    try:
        fe.post_run_events(
            run_id,
            {"events": [{"seq": time.time_ns(), "kind": "screenshot", "payload": evt_payload}]},
            request_id=_req_id(request),
            tenant_id=tenant.id,
            tenant_repo=tenant.repo,
            run_credential=_raw_bearer(request),
        )
    except Exception:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    try:
        os.replace(tmp_file, target_file)
        os.chmod(target_file, 0o600)
    except OSError as exc:
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Screenshot storage unavailable") from exc

    public_base = (os.environ.get("CP_PUBLIC_URL") or str(request.base_url)).rstrip("/")
    url = f"{public_base}/v1/runs/{run_id}/screenshots/{fname}?exp={exp}&sig={sig}"
    alt = (payload.caption or "Screenshot").replace("[", "").replace("]", "")
    return {
        "filename": fname,
        "url": url,
        "markdown": f"![{alt}]({url})",
    }


@app.get("/v1/runs/{run_id}/screenshots/{filename}")
def get_run_screenshot(
    run_id: str,
    filename: str,
    request: Request,
    exp: Optional[int] = Query(default=None),
    sig: Optional[str] = Query(default=None),
    session: Optional[web.Session] = Depends(web.require_user),
    store: Store = Depends(get_store),
) -> Response:
    request.state.run_id = run_id
    fname = os.path.basename(filename)
    if not _SCREENSHOT_FILENAME_RE.fullmatch(fname) or ".." in fname:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found")

    record = store.get_run_credential(run_id)
    tenant_id = str(record.get("tenant_id", "")) if record else ""
    tenant: Optional[Tenant] = None
    if tenant_id:
        if hasattr(store, "tenant_by_id"):
            tenant = store.tenant_by_id(tenant_id)
        if tenant is None and hasattr(store, "tenants"):
            tenants_map = getattr(store, "tenants")
            if isinstance(tenants_map, dict):
                tenant = tenants_map.get(tenant_id)
    if tenant is not None:
        request.state.tenant = tenant.repo

    if exp is not None or sig is not None:
        if exp is None or sig is None or not tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid screenshot signature")
        if int(time.time()) > int(exp):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Screenshot URL has expired")
        try:
            valid_sig = verify_screenshot_signature(tenant_id, run_id, fname, int(exp), sig)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        if not valid_sig:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid screenshot signature")
    else:
        if session is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
        if tenant is None or not session.can_see(tenant.repo):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Repository access required")

    base_dir = _screenshot_cache_dir()
    run_dir = base_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    cached_file = run_dir / fname
    if cached_file.is_file():
        try:
            raw_png = decrypt_screenshot_bytes(tenant_id, run_id, fname, cached_file.read_bytes())
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except Exception:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found")
        return Response(
            content=raw_png,
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'",
            },
        )

    raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found")
