import ast
import pathlib

from flow_engine.conductor.machine import (
    AckEvent,
    ArbitrationEvent,
    CheckCompletedEvent,
    DispatchAction,
    EscalateAction,
    HumanLabelEvent,
    LoopState,
    PlanEvent,
    PushEvent,
    RemoveLabelAction,
    SetLabelAction,
    VerdictEvent,
    step,
)
from atobar_contracts.envelopes import Ack, Arbitration, Finding, Plan, PlannedCriterion, Verdict
from flow_engine.tenancy.manifest import LoopLimits

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"
SHA3 = "3333333333333333333333333333333333333333"
SHA4 = "4444444444444444444444444444444444444444"
SHA5 = "5555555555555555555555555555555555555555"


def test_ac1_happy_path_convergence():
    policy = LoopLimits()
    state = LoopState(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA1,
        required_checks=["ci/test"],
    )

    # 1. Developer sends Ack -> dispatches Evaluator
    ack = Ack(tenant="owner/repo", pr=10, head_sha=SHA1)
    state, actions = step(state, AckEvent(ack=ack), policy)
    assert any(
        isinstance(a, DispatchAction) and a.role == "evaluator" and a.head_sha == SHA1
        for a in actions
    )
    assert state.status == "in_eval"

    # 2. CI completes successfully
    state, actions = step(
        state,
        CheckCompletedEvent(check_name="ci/test", conclusion="success", head_sha=SHA1),
        policy,
    )
    assert state.required_checks_status.get("ci/test") == "success"

    # 3. Evaluator approves -> all conditions met -> dispatches Arbitrator
    verdict = Verdict(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA1,
        round=1,
        verdict="approved",
        findings=[],
    )
    state, actions = step(state, VerdictEvent(verdict=verdict), policy)
    assert state.status == "converged"
    assert any(
        isinstance(a, DispatchAction) and a.role == "arbitrator" and a.head_sha == SHA1
        for a in actions
    )


def test_ac2_cap_max_review_rounds():
    policy = LoopLimits(max_review_rounds=4)
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1)

    # Round 1 on SHA1
    v1 = Verdict(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA1,
        round=1,
        verdict="changes_requested",
        findings=[Finding(id="F1", criterion="AC", evidence="x", message="m1")],
    )
    state, actions = step(state, VerdictEvent(verdict=v1), policy)
    assert state.round == 1
    assert state.status != "escalated"

    # Round 2 on SHA2
    state, _ = step(state, PushEvent(head_sha=SHA2), policy)
    v2 = Verdict(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA2,
        round=2,
        verdict="changes_requested",
        findings=[Finding(id="F1", criterion="AC", evidence="x", message="m2")],
    )
    state, actions = step(state, VerdictEvent(verdict=v2), policy)
    assert state.round == 2
    assert state.status != "escalated"

    # Round 3 on SHA3
    state, _ = step(state, PushEvent(head_sha=SHA3), policy)
    v3 = Verdict(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA3,
        round=3,
        verdict="changes_requested",
        findings=[Finding(id="F1", criterion="AC", evidence="x", message="m3")],
    )
    state, actions = step(state, VerdictEvent(verdict=v3), policy)
    assert state.round == 3
    assert state.status != "escalated"

    # Round 4 on SHA4 -> reaches limit 4 -> trips cap
    state, _ = step(state, PushEvent(head_sha=SHA4), policy)
    v4 = Verdict(
        tenant="owner/repo",
        pr=10,
        head_sha=SHA4,
        round=4,
        verdict="changes_requested",
        findings=[Finding(id="F1", criterion="AC", evidence="x", message="m4")],
    )
    state, actions = step(state, VerdictEvent(verdict=v4), policy)
    assert state.round == 4
    assert state.status == "escalated"
    assert any(
        isinstance(a, EscalateAction) and a.circuit_breaker == "max_review_rounds"
        for a in actions
    )


def test_ac2_cap_max_repeated_finding():
    policy = LoopLimits(max_repeated_finding=2)
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1)

    f = Finding(id="F1", criterion="AC-1", evidence="test:1", message="Broken login")

    # Round 1 has finding f
    v1 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA1, round=1, verdict="changes_requested", findings=[f])
    state, actions = step(state, VerdictEvent(verdict=v1), policy)
    assert state.status != "escalated"

    # Round 2 on new SHA also reports the exact same finding f -> 2 consecutive rounds -> trips cap
    state, _ = step(state, PushEvent(head_sha=SHA2), policy)
    v2 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA2, round=2, verdict="changes_requested", findings=[f])
    state, actions = step(state, VerdictEvent(verdict=v2), policy)
    assert state.status == "escalated"
    assert any(
        isinstance(a, EscalateAction) and a.circuit_breaker == "max_repeated_finding"
        for a in actions
    )


def test_ac2_cap_max_pr_cost_and_wall_clock():
    policy = LoopLimits(max_pr_cost_usd=25.0, max_pr_wall_clock_minutes=240.0)

    # Cost cap
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1)
    v1 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA1, round=1, verdict="changes_requested", findings=[])
    state, actions = step(state, VerdictEvent(verdict=v1, cost_usd=26.0), policy)
    assert state.status == "escalated"
    assert any(isinstance(a, EscalateAction) and a.circuit_breaker == "max_pr_cost_usd" for a in actions)

    # Wall clock cap
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1)
    state, actions = step(state, VerdictEvent(verdict=v1, wall_clock_minutes=245.0), policy)
    assert state.status == "escalated"
    assert any(isinstance(a, EscalateAction) and a.circuit_breaker == "max_pr_wall_clock_minutes" for a in actions)


def test_post_convergence_arbitration_and_check_events_do_not_trip_caps():
    """#354: Once approved/converged, ArbitrationEvent or CheckCompletedEvent must not escalate on wall-clock/cost caps."""
    policy = LoopLimits(max_pr_cost_usd=25.0, max_pr_wall_clock_minutes=30.0)
    ack = Ack(tenant="owner/repo", pr=53, head_sha=SHA1)
    approved = Verdict(tenant="owner/repo", pr=53, head_sha=SHA1, round=1, verdict="approved", findings=[])

    # 1. Converged state receiving ArbitrationEvent echo when accumulated_wall_clock_minutes >= 30
    state = LoopState(
        tenant="owner/repo",
        pr=53,
        head_sha=SHA1,
        round=1,
        accumulated_cost_usd=0.32,
        accumulated_wall_clock_minutes=33.61,
        latest_ack=ack,
        latest_verdict=approved,
        status="in_eval",
    )
    arb = Arbitration(
        tenant="owner/repo",
        pr=53,
        head_sha=SHA1,
        decision="pass",
        mode="recommend",
        action_taken="posted_recommendation",
        summary="pass: All metrics passed",
    )
    s_after_arb, arb_actions = step(state, ArbitrationEvent(arbitration=arb), policy)
    assert s_after_arb.status == "converged"
    assert s_after_arb.latest_arbitration == arb
    assert arb_actions == []

    # 2. Approved verdict waiting on required CI check when over wall-clock cap -> check completes -> converges
    state_waiting_ci = LoopState(
        tenant="owner/repo",
        pr=53,
        head_sha=SHA1,
        required_checks=["ci/test"],
        accumulated_wall_clock_minutes=25.3,
        latest_ack=ack,
        status="in_eval",
    )
    s_after_verdict, v_actions = step(
        state_waiting_ci,
        VerdictEvent(verdict=approved, wall_clock_minutes=8.31),
        policy,
    )
    assert s_after_verdict.status != "escalated"
    assert v_actions == []

    s_after_ci, ci_actions = step(
        s_after_verdict,
        CheckCompletedEvent(check_name="ci/test", conclusion="success", head_sha=SHA1),
        policy,
    )
    assert s_after_ci.status == "converged"
    assert ci_actions == [DispatchAction(role="arbitrator", head_sha=SHA1)]

    # Subsequent check completion on already-converged state does not re-dispatch arbitrator or escalate
    s_after_dup_ci, dup_actions = step(
        s_after_ci,
        CheckCompletedEvent(check_name="ci/test", conclusion="success", head_sha=SHA1),
        policy,
    )
    assert s_after_dup_ci.status == "converged"
    assert dup_actions == []


def test_ac3_rereview_of_same_head_sha_does_not_advance_round():
    policy = LoopLimits()
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1)

    v1 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA1, round=1, verdict="changes_requested", findings=[])
    state, _ = step(state, VerdictEvent(verdict=v1), policy)
    assert state.round == 1

    # Second verdict on the exact same SHA
    v2 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA1, round=1, verdict="changes_requested", findings=[])
    state, _ = step(state, VerdictEvent(verdict=v2), policy)
    assert state.round == 1


def test_ac4_stale_envelope_is_ignored():
    policy = LoopLimits()
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA2)

    # Event for old SHA1
    stale_ack = Ack(tenant="owner/repo", pr=10, head_sha=SHA1)
    new_state, actions = step(state, AckEvent(ack=stale_ack), policy)
    assert new_state == state
    assert len(actions) == 0


def test_human_resume_clears_escalation():
    policy = LoopLimits(max_pr_cost_usd=2.0)
    state = LoopState(tenant="owner/repo", pr=10, head_sha=SHA1, status="escalated")

    # While escalated, events yield no dispatches
    ack = Ack(tenant="owner/repo", pr=10, head_sha=SHA1)
    state, actions = step(state, AckEvent(ack=ack), policy)
    assert state.status == "escalated"
    assert len(actions) == 0

    # First flow:resume clears escalation and removes both flow:escalated and flow:resume
    state, actions = step(state, HumanLabelEvent(label="flow:resume"), policy)
    assert state.status == "in_dev"
    assert RemoveLabelAction(label="flow:escalated") in actions
    assert RemoveLabelAction(label="flow:resume") in actions
    assert SetLabelAction(label="flow:in-progress") in actions

    # Second escalation on the same issue followed by a second flow:resume
    v1 = Verdict(tenant="owner/repo", pr=10, head_sha=SHA1, round=1, verdict="changes_requested", findings=[])
    state, actions = step(state, VerdictEvent(verdict=v1, cost_usd=2.5), policy)
    assert state.status == "escalated"

    state, actions = step(state, HumanLabelEvent(label="flow:resume"), policy)
    assert state.status == "in_dev"
    assert RemoveLabelAction(label="flow:escalated") in actions
    assert RemoveLabelAction(label="flow:resume") in actions
    assert SetLabelAction(label="flow:in-progress") in actions


def test_plan_event_single_pr_transitions_to_developer():
    policy = LoopLimits()
    sentinel = "0" * 40
    state = LoopState(tenant="owner/repo", pr=33, head_sha=sentinel, status="in_plan")
    plan = Plan(
        tenant="owner/repo",
        pr=33,
        head_sha=sentinel,
        complexity="single_pr",
        goal="Fix layout",
        scope_files=["src/app.py"],
        acceptance_criteria=[PlannedCriterion(id="AC-1", description="No overflow")],
        implementation_steps=["Update src/app.py"],
    )
    state, actions = step(state, PlanEvent(plan=plan), policy)
    assert state.status == "in_dev"
    assert state.latest_plan == plan
    assert DispatchAction(role="developer", head_sha=sentinel) in actions
    assert SetLabelAction(label="flow:in-progress") in actions

    # Subsequent Ack from developer with real commit SHA adopts SHA1 cleanly
    ack = Ack(tenant="owner/repo", pr=33, head_sha=SHA1)
    state, actions = step(state, AckEvent(ack=ack), policy)
    assert state.head_sha == SHA1
    assert state.status == "in_eval"
    assert state.latest_plan == plan
    assert DispatchAction(role="evaluator", head_sha=SHA1) in actions


def test_plan_event_multi_issue_epic_or_flow_plan_needs_approval():
    policy = LoopLimits()
    sentinel = "0" * 40
    state = LoopState(tenant="owner/repo", pr=33, head_sha=sentinel, status="in_plan")

    epic_plan = Plan(
        tenant="owner/repo",
        pr=33,
        head_sha=sentinel,
        complexity="multi_issue_epic",
        goal="Decompose into 3 sub-issues",
    )
    s1, actions1 = step(state, PlanEvent(plan=epic_plan), policy)
    assert s1.status == "idle"
    assert s1.latest_plan == epic_plan
    assert actions1 == [SetLabelAction(label="flow:needs-approval")]

    single_plan = Plan(
        tenant="owner/repo",
        pr=33,
        head_sha=sentinel,
        complexity="single_pr",
        goal="Plan requested explicitly via flow:plan",
    )
    s2, actions2 = step(state, PlanEvent(plan=single_plan, trigger_label="flow:plan"), policy)
    assert s2.status == "idle"
    assert s2.latest_plan == single_plan
    assert actions2 == [SetLabelAction(label="flow:needs-approval")]

    # Applying flow:approved consumes command labels and dispatches developer
    s3, actions3 = step(s2, HumanLabelEvent(label="flow:approved"), policy)
    assert s3.status == "in_dev"
    assert RemoveLabelAction(label="flow:needs-approval") in actions3
    assert RemoveLabelAction(label="flow:approved") in actions3
    assert RemoveLabelAction(label="flow:plan") in actions3
    assert DispatchAction(role="developer", head_sha=sentinel) in actions3
    assert SetLabelAction(label="flow:in-progress") in actions3



def test_ac5_no_io_or_llm_imports():
    machine_path = pathlib.Path(__file__).resolve().parents[2] / "src" / "flow_engine" / "conductor" / "machine.py"
    tree = ast.parse(machine_path.read_text(), filename=str(machine_path))

    forbidden = ["google.genai", "openai", "anthropic", "litellm", "requests", "urllib", "httpx", "socket"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for f in forbidden:
                    assert f not in alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for f in forbidden:
                    assert f not in node.module
