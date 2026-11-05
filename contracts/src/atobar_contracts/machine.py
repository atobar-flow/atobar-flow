"""Deterministic conductor pure state machine and circuit breaker enforcement."""

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from atobar_contracts.envelopes import Ack, Arbitration, Plan, Verdict
from atobar_contracts.manifest import LoopLimits


class LoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    pr: int
    head_sha: str
    round: int = 0
    head_sha_history: list[str] = Field(default_factory=list)
    findings_history: list[list[str]] = Field(default_factory=list)
    accumulated_cost_usd: float = 0.0
    accumulated_wall_clock_minutes: float = 0.0
    latest_plan: Optional[Plan] = None
    latest_ack: Optional[Ack] = None
    latest_verdict: Optional[Verdict] = None
    latest_arbitration: Optional[Arbitration] = None
    required_checks: list[str] = Field(default_factory=list)
    required_checks_status: dict[str, str] = Field(default_factory=dict)
    status: Literal["idle", "in_plan", "in_dev", "in_eval", "in_arbiter", "converged", "escalated"] = "idle"
    escalation_reason: Optional[str] = None


class PlanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Plan
    trigger_label: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_minutes: float = 0.0


class AckEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ack: Ack
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_minutes: float = 0.0


class VerdictEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_minutes: float = 0.0


class PushEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    head_sha: str


class CheckCompletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_name: str
    conclusion: str
    head_sha: str


class ArbitrationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arbitration: Arbitration
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_minutes: float = 0.0


class HumanLabelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str


class HumanCommentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["issue", "pr"]
    intent: Literal["plan", "approve", "correct", "evaluate"]
    head_sha: str = ""
    comment_id: Optional[int] = None


Event = Union[
    PlanEvent,
    AckEvent,
    VerdictEvent,
    PushEvent,
    CheckCompletedEvent,
    ArbitrationEvent,
    HumanLabelEvent,
    HumanCommentEvent,
]


class DispatchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["planner", "developer", "evaluator", "arbitrator"]
    head_sha: str


class EscalateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    circuit_breaker: str
    tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_minutes: float = 0.0


class SetLabelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str


class RemoveLabelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str


class CommentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str


Action = Union[
    DispatchAction,
    EscalateAction,
    SetLabelAction,
    RemoveLabelAction,
    CommentAction,
]


def _check_convergence(state: LoopState) -> bool:
    checks_green = all(
        state.required_checks_status.get(c) == "success"
        for c in state.required_checks
    )
    verdict_approved = (
        state.latest_verdict is not None
        and state.latest_verdict.verdict == "approved"
        and state.latest_verdict.head_sha == state.head_sha
    )
    ack_clean = (
        state.latest_ack is None
        or (
            state.latest_ack.head_sha == state.head_sha
            and len(state.latest_ack.dispute) == 0
        )
    )
    return checks_green and verdict_approved and ack_clean


def step(
    state: LoopState,
    event: Event,
    policy: LoopLimits,
) -> tuple[LoopState, list[Action]]:
    """Pure transition function evaluating PR events against state and caps."""
    s = state.model_copy(deep=True)
    actions: list[Action] = []

    # Handle escalation recovery
    if s.status == "escalated":
        if isinstance(event, HumanLabelEvent) and event.label == "flow:resume":
            s.status = "in_dev"
            s.escalation_reason = None
            actions.append(RemoveLabelAction(label="flow:escalated"))
            actions.append(RemoveLabelAction(label="flow:resume"))
            actions.append(SetLabelAction(label="flow:in-progress"))
            return s, actions
        return s, []

    if isinstance(event, HumanLabelEvent) and event.label == "flow:approved":
        if s.latest_plan is not None and s.status in ("idle", "in_plan"):
            s.status = "in_dev"
            actions.append(RemoveLabelAction(label="flow:needs-approval"))
            actions.append(RemoveLabelAction(label="flow:approved"))
            actions.append(RemoveLabelAction(label="flow:plan"))
            actions.append(DispatchAction(role="developer", head_sha=s.head_sha))
            actions.append(SetLabelAction(label="flow:in-progress"))
        return s, actions

    # Stale event check: ignore events bound to older head_sha
    event_sha: Optional[str] = None
    if isinstance(event, AckEvent):
        event_sha = event.ack.head_sha
    elif isinstance(event, VerdictEvent):
        event_sha = event.verdict.head_sha
    elif isinstance(event, CheckCompletedEvent):
        event_sha = event.head_sha

    if event_sha is not None:
        if not s.head_sha or s.head_sha == "0" * 40:
            # Issue-based loop: adopt the SHA from the first envelope (Ack from developer)
            s.head_sha = event_sha
        elif event_sha != s.head_sha:
            # Stale event check: ignore events bound to older head_sha
            return s, []

    # Accumulate generic event costs if available
    if hasattr(event, "cost_usd"):
        s.accumulated_cost_usd += getattr(event, "cost_usd", 0.0)
    if hasattr(event, "wall_clock_minutes"):
        s.accumulated_wall_clock_minutes += getattr(event, "wall_clock_minutes", 0.0)

    def _check_cost_limits(state_to_check: LoopState, policy_to_check: LoopLimits, actions_list: list[Action]) -> bool:
        """Returns True if escalated"""
        if state_to_check.accumulated_cost_usd >= policy_to_check.max_pr_cost_usd:
            state_to_check.status = "escalated"
            state_to_check.escalation_reason = (
                f"max_pr_cost_usd reached (${state_to_check.accumulated_cost_usd:.2f} >= "
                f"${policy_to_check.max_pr_cost_usd:.2f})"
            )
            actions_list.append(
                EscalateAction(
                    reason=state_to_check.escalation_reason,
                    circuit_breaker="max_pr_cost_usd",
                )
            )
            actions_list.append(SetLabelAction(label="flow:escalated"))
            return True

        if state_to_check.accumulated_wall_clock_minutes >= policy_to_check.max_pr_wall_clock_minutes:
            state_to_check.status = "escalated"
            state_to_check.escalation_reason = (
                f"max_pr_wall_clock_minutes reached "
                f"({state_to_check.accumulated_wall_clock_minutes} >= {policy_to_check.max_pr_wall_clock_minutes})"
            )
            actions_list.append(
                EscalateAction(
                    reason=state_to_check.escalation_reason,
                    circuit_breaker="max_pr_wall_clock_minutes",
                )
            )
            actions_list.append(SetLabelAction(label="flow:escalated"))
            return True
        return False

    if isinstance(event, ArbitrationEvent):
        s.latest_arbitration = event.arbitration
        s.status = "converged"
        return s, []

    if isinstance(event, PushEvent):
        s.head_sha = event.head_sha
        s.status = "in_dev"
        s.latest_ack = None
        s.latest_verdict = None
        s.latest_arbitration = None
        s.required_checks_status.clear()
        return s, []

    if isinstance(event, CheckCompletedEvent):
        s.required_checks_status[event.check_name] = event.conclusion
        if s.status != "converged" and s.latest_arbitration is None and _check_convergence(s):
            s.status = "converged"
            actions.append(DispatchAction(role="arbitrator", head_sha=s.head_sha))
        return s, actions

    already_converged = (
        s.status == "converged"
        or s.latest_arbitration is not None
        or _check_convergence(s)
    )
    is_converging_approval = isinstance(event, VerdictEvent) and event.verdict.verdict == "approved"
    if (
        not is_converging_approval
        and (isinstance(event, HumanCommentEvent) or not already_converged)
        and _check_cost_limits(s, policy, actions)
    ):
        return s, actions

    if isinstance(event, HumanCommentEvent):
        if s.round >= policy.max_review_rounds:
            s.status = "escalated"
            s.escalation_reason = (
                f"max_review_rounds reached ({s.round} >= {policy.max_review_rounds})"
            )
            actions.append(
                EscalateAction(
                    reason=s.escalation_reason,
                    circuit_breaker="max_review_rounds",
                )
            )
            actions.append(SetLabelAction(label="flow:escalated"))
            return s, actions

        eff_sha = event.head_sha or s.head_sha
        if event.head_sha:
            s.head_sha = event.head_sha
        s.escalation_reason = None
        s.latest_arbitration = None
        if event.target == "issue":
            if event.intent == "approve":
                s.status = "in_dev"
                actions.append(RemoveLabelAction(label="flow:needs-approval"))
                actions.append(RemoveLabelAction(label="flow:plan"))
                actions.append(SetLabelAction(label="flow:in-progress"))
                actions.append(DispatchAction(role="developer", head_sha=eff_sha))
            else:
                s.status = "in_plan"
                actions.append(RemoveLabelAction(label="flow:needs-approval"))
                actions.append(SetLabelAction(label="flow:in-progress"))
                actions.append(DispatchAction(role="planner", head_sha=eff_sha))
        else:
            if event.intent == "evaluate":
                s.status = "in_eval"
                actions.append(SetLabelAction(label="flow:in-review"))
                actions.append(DispatchAction(role="evaluator", head_sha=eff_sha))
            else:
                s.status = "in_dev"
                s.latest_verdict = None
                s.latest_ack = None
                actions.append(RemoveLabelAction(label="flow:in-review"))
                actions.append(SetLabelAction(label="flow:in-progress"))
                actions.append(DispatchAction(role="developer", head_sha=eff_sha))
        return s, actions

    if isinstance(event, PlanEvent):
        s.latest_plan = event.plan
        if event.plan.complexity == "multi_issue_epic" or event.trigger_label == "flow:plan":
            s.status = "idle"
            actions.append(SetLabelAction(label="flow:needs-approval"))
        else:
            s.status = "in_dev"
            actions.append(DispatchAction(role="developer", head_sha=s.head_sha))
            actions.append(SetLabelAction(label="flow:in-progress"))
        return s, actions

    if isinstance(event, AckEvent):
        s.latest_ack = event.ack
        if not event.ack.dispute:
            s.status = "in_eval"
            actions.append(DispatchAction(role="evaluator", head_sha=s.head_sha))
            actions.append(SetLabelAction(label="flow:in-review"))
        return s, actions

    if isinstance(event, VerdictEvent):
        v = event.verdict
        s.latest_verdict = v

        if s.head_sha not in s.head_sha_history:
            s.round += 1
            s.head_sha_history.append(s.head_sha)
            current_fps = [f.fingerprint for f in v.findings if f.fingerprint]
            s.findings_history.append(current_fps)

        # Circuit Breakers
        # 1. max_review_rounds
        if s.round >= policy.max_review_rounds and v.verdict != "approved":
            s.status = "escalated"
            s.escalation_reason = (
                f"max_review_rounds reached ({s.round} >= {policy.max_review_rounds})"
            )
            actions.append(
                EscalateAction(
                    reason=s.escalation_reason,
                    circuit_breaker="max_review_rounds",
                )
            )
            actions.append(SetLabelAction(label="flow:escalated"))
            return s, actions

        # 2. max_repeated_finding
        if len(s.findings_history) >= policy.max_repeated_finding:
            last_n = [
                set(fps) for fps in s.findings_history[-policy.max_repeated_finding:]
            ]
            common = set.intersection(*last_n) if last_n else set()
            if common:
                tripped_fp = sorted(common)[0]
                s.status = "escalated"
                s.escalation_reason = (
                    f"max_repeated_finding reached for {tripped_fp}"
                )
                actions.append(
                    EscalateAction(
                        reason=s.escalation_reason,
                        circuit_breaker="max_repeated_finding",
                    )
                )
                actions.append(SetLabelAction(label="flow:escalated"))
                return s, actions

        # If not escalated: check convergence or rework
        if _check_convergence(s):
            s.status = "converged"
            actions.append(DispatchAction(role="arbitrator", head_sha=s.head_sha))
        elif v.verdict == "changes_requested":
            if _check_cost_limits(s, policy, actions):
                return s, actions
            s.status = "in_dev"
            actions.append(DispatchAction(role="developer", head_sha=s.head_sha))
            actions.append(SetLabelAction(label="flow:in-progress"))

        return s, actions

    return s, actions
