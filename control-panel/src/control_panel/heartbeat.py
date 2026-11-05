from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from control_panel.auth import require_run_credential
from control_panel.flow_engine_client import FlowEngineClient, get_flow_engine_client

router = APIRouter()


class HeartbeatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    current_todo: Optional[str] = None
    last_tool: Optional[str] = None
    cost_estimate: Optional[float] = None
    instance_id: str
    target_repo: Optional[str] = None
    issue_id: int
    agent_runtime: Optional[str] = "nanobot"
    iterations: Optional[int] = 0
    last_activity_at: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    halt_reason: Optional[str] = None
    halt_detail: Optional[str] = None
    head_sha: Optional[str] = None


def _extract_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return None


@router.post("/v1/runs/{run_id}/heartbeat")
def handle_heartbeat(
    run_id: str,
    payload: HeartbeatPayload,
    request: Request,
    record: dict[str, Any] = Depends(require_run_credential),
    fe: FlowEngineClient = Depends(get_flow_engine_client),
) -> dict[str, Any]:
    tenant = record["tenant"]
    request.state.run_id = run_id
    request.state.tenant = tenant.repo
    req_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")
    raw_cred = _extract_bearer_token(request)

    body = payload.model_dump(exclude_none=True)
    body["tenant_id"] = tenant.id
    body["role"] = str(record.get("role", "developer"))
    return fe.post_run_heartbeat(
        run_id,
        body,
        request_id=req_id,
        tenant_id=tenant.id,
        tenant_repo=tenant.repo,
        run_credential=raw_cred,
    )
