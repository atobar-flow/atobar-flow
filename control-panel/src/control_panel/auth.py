"""Unified run-credential authentication and instance binding guard (CSPM-05)."""

import hashlib
import hmac
import json
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from control_panel.store import Store, Tenant, get_store

ALL_SANDBOX_ROLES: frozenset[str] = frozenset(
    {"planner", "developer", "evaluator", "arbitrator", "onboarder", "metric_compiler"}
)

# Single audited role-allowlist table for every /v1/runs/{run_id}/* route (J42 WP-2 / UJ-10).
RUN_ROUTE_ALLOWED_ROLES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/v1/runs/{run_id}/heartbeat"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/llm-key"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/github-token"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/events"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/screenshots"): ALL_SANDBOX_ROLES,
    ("GET", "/v1/runs/{run_id}/wiki"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/wiki"): frozenset({"onboarder"}),
    ("POST", "/v1/runs/{run_id}/metric-compilation"): frozenset({"metric_compiler"}),
    ("POST", "/v1/runs/{run_id}/turn-complete"): ALL_SANDBOX_ROLES,
    ("GET", "/v1/runs/{run_id}/arbitration-data"): ALL_SANDBOX_ROLES,
    ("POST", "/v1/runs/{run_id}/arbitration-decisions"): ALL_SANDBOX_ROLES,
}


def _resolve_tenant(store: Store, tenant_id: str) -> Optional[Tenant]:
    tenant: Optional[Tenant] = None
    if hasattr(store, "tenant_by_id"):
        tenant = store.tenant_by_id(tenant_id)
    if tenant is None and hasattr(store, "tenants"):
        tenants_map = getattr(store, "tenants")
        if isinstance(tenants_map, dict) and tenant_id in tenants_map:
            tenant = tenants_map[tenant_id]
    return tenant


async def _extract_instance_id(request: Request, x_instance_id: Optional[str]) -> Optional[str]:
    if x_instance_id and x_instance_id.strip():
        return x_instance_id.strip()
    q_inst = request.query_params.get("instance_id")
    if q_inst and q_inst.strip():
        return q_inst.strip()
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            raw = await request.body()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    val = parsed.get("instance_id")
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            pass
    return None


async def _verify_run_credential(
    run_id: str,
    request: Request,
    authorization: Optional[str],
    x_instance_id: Optional[str],
    store: Store,
    roles: Optional[Collection[str]] = None,
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid authorization header")

    raw_cred = authorization.removeprefix("Bearer ").strip()
    if not raw_cred:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid authorization header")

    cred_hash = hashlib.sha256(raw_cred.encode("utf-8")).hexdigest()
    record = store.get_run_credential(run_id)
    stored_hash = str(record.get("cred_hash", "")) if record else ""
    if not record or not stored_hash or not hmac.compare_digest(stored_hash, cred_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credential for run")

    expires_at_raw = record.get("expires_at")
    if not expires_at_raw or not isinstance(expires_at_raw, str):
        raise HTTPException(status.HTTP_410_GONE, "Run credential expired")
    try:
        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status.HTTP_410_GONE, "Run credential expired")
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_410_GONE, "Run credential expired")

    tenant_id = str(record.get("tenant_id", ""))
    tenant = _resolve_tenant(store, tenant_id)
    if tenant is None or getattr(tenant, "status", "") == "offboarded":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant not found or offboarded")

    allowed_roles: Optional[frozenset[str]] = (
        frozenset(str(r).strip().lower() for r in roles) if roles is not None else None
    )
    if allowed_roles is None and request is not None:
        route = request.scope.get("route") if isinstance(getattr(request, "scope", None), dict) else None
        route_path = getattr(route, "path", None)
        if route_path:
            allowed_roles = RUN_ROUTE_ALLOWED_ROLES.get((request.method.upper(), str(route_path)))
    if allowed_roles is not None:
        cred_role = str(record.get("role") or "").strip().lower()
        if cred_role not in allowed_roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Role '{cred_role}' is not permitted on this endpoint (allowed: {sorted(allowed_roles)})",
            )

    instance_id = await _extract_instance_id(request, x_instance_id)
    if instance_id:
        claimed = record.get("claimed_instance_id")
        if claimed:
            if not hmac.compare_digest(str(claimed), instance_id):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Run credential already claimed by another instance",
                )
        else:
            record["claimed_instance_id"] = instance_id
            if hasattr(store, "claim_run_credential_instance"):
                ok = store.claim_run_credential_instance(run_id, instance_id)
                if ok is False:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        "Run credential already claimed by another instance",
                    )
            else:
                from control_panel import app as cp_app
                from control_panel.flow_engine_client import get_flow_engine_client

                fe_override = cp_app.app.dependency_overrides.get(get_flow_engine_client)
                fe = fe_override() if fe_override is not None else get_flow_engine_client(store)
                req_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")
                fe.post_run_credential_used(
                    run_id,
                    instance_id=instance_id,
                    request_id=req_id,
                    tenant_id=tenant.id,
                    tenant_repo=tenant.repo,
                    run_credential=raw_cred,
                )

    result = dict(record)
    result["tenant"] = tenant
    return result


def _default_roles() -> Optional[Collection[str]]:
    return None


async def require_run_credential(
    run_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_instance_id: Optional[str] = Header(None, alias="X-Instance-ID"),
    store: Store = Depends(get_store),
    roles: Optional[Collection[str]] = Depends(_default_roles),
) -> dict[str, Any]:
    return await _verify_run_credential(
        run_id, request, authorization, x_instance_id, store, roles=roles
    )
