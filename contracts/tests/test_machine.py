"""Unit tests for HumanCommentEvent routing in the deterministic state machine (J49 #365 WP-2)."""

from atobar_contracts.machine import (
    DispatchAction,
    EscalateAction,
    HumanCommentEvent,
    LoopState,
    RemoveLabelAction,
    SetLabelAction,
    step,
)
from atobar_contracts.manifest import LoopLimits

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"


def test_human_comment_on_issue_plan_and_approve() -> None:
    policy = LoopLimits()
    state = LoopState(tenant="arsenyspb/ollama-monitor", pr=53, head_sha=SHA1, status="idle")

    # 1. Comment on Issue with intent='plan' -> dispatches planner
    s_plan, actions_plan = step(
        state,
        HumanCommentEvent(target="issue", intent="plan", comment_id=101),
        policy,
    )
    assert s_plan.status == "in_plan"
    assert DispatchAction(role="planner", head_sha=SHA1) in actions_plan
    assert SetLabelAction(label="flow:in-progress") in actions_plan
    assert RemoveLabelAction(label="flow:needs-approval") in actions_plan

    # 2. Comment on Issue with intent='approve' -> dispatches developer
    s_app, actions_app = step(
        s_plan,
        HumanCommentEvent(target="issue", intent="approve", comment_id=102),
        policy,
    )
    assert s_app.status == "in_dev"
    assert DispatchAction(role="developer", head_sha=SHA1) in actions_app
    assert SetLabelAction(label="flow:in-progress") in actions_app
    assert RemoveLabelAction(label="flow:needs-approval") in actions_app
    assert RemoveLabelAction(label="flow:plan") in actions_app


def test_human_comment_on_pr_correct_and_evaluate_and_cap_enforcement() -> None:
    policy = LoopLimits(max_review_rounds=4, max_pr_cost_usd=5.0)
    state = LoopState(
        tenant="arsenyspb/ollama-monitor",
        pr=53,
        head_sha=SHA1,
        status="in_eval",
    )

    # 1. Comment on PR with intent='correct' on SHA2 -> dispatches developer on SHA2
    s_dev, actions_dev = step(
        state,
        HumanCommentEvent(target="pr", intent="correct", head_sha=SHA2, comment_id=201),
        policy,
    )
    assert s_dev.status == "in_dev"
    assert s_dev.head_sha == SHA2
    assert DispatchAction(role="developer", head_sha=SHA2) in actions_dev
    assert RemoveLabelAction(label="flow:in-review") in actions_dev
    assert SetLabelAction(label="flow:in-progress") in actions_dev

    # 2. Comment on PR with intent='evaluate' -> dispatches evaluator on SHA2
    s_eval, actions_eval = step(
        s_dev,
        HumanCommentEvent(target="pr", intent="evaluate", head_sha=SHA2, comment_id=202),
        policy,
    )
    assert s_eval.status == "in_eval"
    assert DispatchAction(role="evaluator", head_sha=SHA2) in actions_eval
    assert SetLabelAction(label="flow:in-review") in actions_eval

    # 3. Escalated state requires flow:resume (HumanCommentEvent alone cannot bypass escalation)
    esc_state = state.model_copy(update={"status": "escalated", "escalation_reason": "max_pr_cost_usd reached"})
    s_esc, actions_esc = step(
        esc_state,
        HumanCommentEvent(target="pr", intent="correct", head_sha=SHA2, comment_id=203),
        policy,
    )
    assert s_esc.status == "escalated"
    assert actions_esc == []

    # 4. HumanCommentEvent enforces max_review_rounds and cost caps
    capped_round = state.model_copy(update={"round": 4})
    s_cap, actions_cap = step(
        capped_round,
        HumanCommentEvent(target="pr", intent="correct", head_sha=SHA2, comment_id=204),
        policy,
    )
    assert s_cap.status == "escalated"
    assert any(isinstance(a, EscalateAction) and a.circuit_breaker == "max_review_rounds" for a in actions_cap)

    capped_cost = state.model_copy(update={"status": "converged", "accumulated_cost_usd": 5.5})
    s_cost, actions_cost = step(
        capped_cost,
        HumanCommentEvent(target="pr", intent="correct", head_sha=SHA2, comment_id=205),
        policy,
    )
    assert s_cost.status == "escalated"
    assert any(isinstance(a, EscalateAction) and a.circuit_breaker == "max_pr_cost_usd" for a in actions_cost)
