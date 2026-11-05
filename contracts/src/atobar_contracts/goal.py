"""TenantGoal schema and pure outer-loop goal_step() state machine."""

from datetime import datetime, timezone
import hashlib
import json
from typing import TYPE_CHECKING, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from atobar_contracts.manifest import TenantManifest


class GoalBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_goal_cost_usd: float = Field(default=100.0, gt=0, le=5000.0)
    max_goal_prs: int = Field(default=10, ge=1, le=100)
    max_goal_days: int = Field(default=14, ge=1, le=90)


class GoalStallPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_merges_without_progress: int = Field(default=3, ge=1, le=20)
    max_open_planned_issues: int = Field(default=2, ge=1, le=5)


class TenantGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=4000)
    targets: list[str] = Field(min_length=1)
    guardrails: list[str] = Field(default_factory=list)
    autonomy: Literal["approve", "auto"] = "approve"
    tracking_issue_number: Optional[int] = Field(default=None, gt=0)
    budget: GoalBudget = Field(default_factory=GoalBudget)
    stall: GoalStallPolicy = Field(default_factory=GoalStallPolicy)

    @field_validator("targets", "guardrails")
    @classmethod
    def _unique_metric_keys(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("goal metric keys must be unique")
        return v

    def sha256(self) -> str:
        data = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class GoalState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_sha: str = ""
    status: Literal["open", "met", "stopped"] = "open"
    stop_reason: Optional[str] = None
    spent_usd: float = 0.0
    prs_merged: int = 0
    merges_without_progress: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_progress_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GoalMetAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["met"] = "met"


class GoalStopAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["stop"] = "stop"
    reason: Literal["budget_cost", "budget_prs", "budget_days", "stalled"]


class GoalWaitAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["wait"] = "wait"


class GoalPlanAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["plan"] = "plan"
    count: int = Field(ge=1)


GoalAction = Union[GoalMetAction, GoalStopAction, GoalWaitAction, GoalPlanAction]


def _target_meets_threshold(val: float, threshold: float, comparator: str) -> bool:
    if comparator == ">=":
        return val >= threshold
    if comparator == "<=":
        return val <= threshold
    if comparator == ">":
        return val > threshold
    if comparator == "<":
        return val < threshold
    if comparator == "==":
        return abs(val - threshold) <= 1e-9
    return False


def goal_step(
    manifest: "TenantManifest",
    goal_state: GoalState,
    latest_default_obs: dict[str, float],
    open_planned_issues: int,
    now: datetime,
) -> GoalAction:
    """Pure deterministic outer-loop state machine for repository goal progression."""
    goal = manifest.goal
    if goal is None or goal_state.status != "open":
        return GoalWaitAction()

    metrics_by_key = {m.key: m for m in manifest.metrics}

    # 1. Check if all manifest.goal.targets meet their Metric.comparator + Metric.threshold on latest_default_obs
    all_met = True
    for target_key in goal.targets:
        m = metrics_by_key.get(target_key)
        if m is None or target_key not in latest_default_obs:
            all_met = False
            break
        if not _target_meets_threshold(float(latest_default_obs[target_key]), float(m.threshold), str(m.comparator)):
            all_met = False
            break
    if all_met:
        return GoalMetAction()

    # 2. Check budgets & stall policy
    if goal_state.spent_usd >= goal.budget.max_goal_cost_usd:
        return GoalStopAction(reason="budget_cost")
    if goal_state.prs_merged >= goal.budget.max_goal_prs:
        return GoalStopAction(reason="budget_prs")

    started_dt = (
        goal_state.started_at
        if goal_state.started_at.tzinfo is not None
        else goal_state.started_at.replace(tzinfo=timezone.utc)
    )
    now_dt = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if (now_dt - started_dt).total_seconds() / 86400.0 >= goal.budget.max_goal_days:
        return GoalStopAction(reason="budget_days")

    if goal_state.merges_without_progress >= goal.stall.max_merges_without_progress:
        return GoalStopAction(reason="stalled")

    # 3. Check WIP limit
    active_wip = max(0, int(open_planned_issues))
    if active_wip >= goal.stall.max_open_planned_issues:
        return GoalWaitAction()
    return GoalPlanAction(count=goal.stall.max_open_planned_issues - active_wip)


__all__ = [
    "GoalAction",
    "GoalBudget",
    "GoalMetAction",
    "GoalPlanAction",
    "GoalStallPolicy",
    "GoalState",
    "GoalStopAction",
    "GoalWaitAction",
    "TenantGoal",
    "goal_step",
]
