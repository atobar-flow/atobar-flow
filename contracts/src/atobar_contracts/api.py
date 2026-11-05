"""Shared Pydantic request/response schemas for Control Panel <-> Flow Engine internal RPC."""

from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

from atobar_contracts.client_templates import resolve_cp_url
from atobar_contracts.goal import TenantGoal


class InternalEventRequest(BaseModel):
    tenant: Optional[str] = None
    tenant_id: Optional[str] = None
    event_type: Optional[str] = None
    delivery_id: Optional[str] = None
    event_id: Optional[str] = None
    event: dict[str, Any] = Field(default_factory=dict)


class InternalMetricsRequest(BaseModel):
    tenant_id: Optional[str] = None
    tenant: Optional[str] = None
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pr_number: Optional[int] = Field(default=None, gt=0)
    run_id: Optional[int] = Field(default=None, gt=0)
    run_attempt: Optional[int] = Field(default=1, ge=1)
    workflow_path: str = Field(min_length=1, max_length=260)
    values: dict[str, float] = Field(default_factory=dict)


class CreateTenantRequest(BaseModel):
    repo: str
    created_by: Optional[str] = None
    issue_number: int = 0
    dispatch_onboard: bool = False


class ArbitrationModeUpdateRequest(BaseModel):
    mode: Literal["shadow", "recommend", "auto_merge"]
    actor: str = "admin"


class AssigneeUpdateRequest(BaseModel):
    owner_github_login: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
    actor: str = "admin"


class GuardrailsUpdateRequest(BaseModel):
    spot_max_instance_minutes: int = Field(default=60, ge=10, le=480)
    max_pr_wall_clock_minutes: int = Field(default=240, ge=10, le=1440)
    max_pr_cost_usd: float = Field(default=25.0, gt=0, le=200)
    max_review_rounds: int = Field(default=4, ge=1, le=10)
    max_repeated_finding: int = Field(default=2, ge=1, le=5)
    actor: str = "admin"


class GoalUpdateRequest(TenantGoal):
    model_config = ConfigDict(extra="forbid")
    actor: str = "admin"

    @model_validator(mode="before")
    @classmethod
    def _normalize_flat_budget_and_stall(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        budget = dict(out.get("budget") or {}) if isinstance(out.get("budget"), dict) else {}
        for k in ("max_goal_cost_usd", "max_goal_prs", "max_goal_days"):
            if k in out:
                budget.setdefault(k, out.pop(k))
        if budget:
            out["budget"] = budget
        stall = dict(out.get("stall") or {}) if isinstance(out.get("stall"), dict) else {}
        for k in ("max_merges_without_progress", "max_open_planned_issues"):
            if k in out:
                stall.setdefault(k, out.pop(k))
        if stall:
            out["stall"] = stall
        return out


class MetricDraftRequest(BaseModel):
    intent: str = Field(min_length=3, max_length=2000)
    kind: Literal["guardrail", "target"] = "guardrail"
    rationale: Optional[str] = Field(default="", max_length=2000)
    actor: str = "admin"


class MetricCompileRequest(BaseModel):
    actor: str = "admin"


class MetricCompilationPayload(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    compiled: dict[str, Any]
    value: float
    sha: str
    stdout_tail: str = ""


class MetricApproveRequest(BaseModel):
    digest: str = Field(min_length=1)
    actor: str = "admin"
    cp_url: str = Field(default_factory=resolve_cp_url)


class MetricRetireRequest(BaseModel):
    actor: str = "admin"
    cp_url: str = Field(default_factory=resolve_cp_url)


class HeartbeatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    current_todo: Optional[str] = None
    last_tool: Optional[str] = None
    cost_estimate: Optional[float] = None
    instance_id: str
    target_repo: Optional[str] = None
    issue_id: int = 0
    agent_runtime: Optional[str] = "nanobot"
    iterations: Optional[int] = 0
    last_activity_at: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    halt_reason: Optional[str] = None
    halt_detail: Optional[str] = None
    head_sha: Optional[str] = None
    tenant_id: Optional[str] = None
    role: Optional[str] = None


class RunEventItem(BaseModel):
    seq: int
    kind: str
    ts: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunEventsBatchPayload(BaseModel):
    events: list[RunEventItem]


class TurnCompletePayload(BaseModel):
    comment_body: str
    comment_id: Optional[int] = None
    head_sha: Optional[str] = None
    issue_number: Optional[int] = None
    instance_id: Optional[str] = None
    trigger_label: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    wall_clock_minutes: float = 0.0


class ArbitrationDecisionPayload(BaseModel):
    pr_number: int
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    mode: Literal["shadow", "recommend", "auto_merge"]
    decision: str
    reasons: Optional[str] = None
    tenant_id: Optional[str] = None


class WikiRegenerateRequest(BaseModel):
    actor: str = "admin"


class WikiPolicyUpdateRequest(BaseModel):
    refresh_interval_merges: int = Field(default=5, ge=0, le=100)
    actor: str = "admin"
