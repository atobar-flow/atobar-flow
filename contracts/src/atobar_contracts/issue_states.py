"""Issue states, transitions, and status card renderer for atobar-flow."""

from collections.abc import Iterable
from typing import Any, Optional

STATUS_MARKER = "<!-- atobar:status -->"
ONBOARDING_HOLD_MARKER = "<!-- atobar:onboarding-hold -->"

COMMAND_LABELS: frozenset[str] = frozenset({
    "flow:approved",
    "flow:resume",
})

FLOW_LABEL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "name": "flow:feature",
        "color": "0e8a16",
        "role": "human",
        "description": "You apply: build a new feature or capability",
    },
    {
        "name": "flow:fix",
        "color": "006b75",
        "role": "human",
        "description": "You apply: fix a bug or regression",
    },
    {
        "name": "flow:plan",
        "color": "5319e7",
        "role": "human",
        "description": "You apply: draft an implementation plan first and pause for your approval",
    },
    {
        "name": "flow:approved",
        "color": "2ea44f",
        "role": "human",
        "description": "You apply: approve the proposed plan and start coding (auto-removed once consumed)",
    },
    {
        "name": "flow:resume",
        "color": "1b7c83",
        "role": "human",
        "description": "You apply: reset circuit breaker budget and resume the agent (auto-removed once consumed)",
    },
    {
        "name": "flow:in-progress",
        "color": "fbca04",
        "role": "system",
        "description": "Set by Atobar: agent is actively planning or coding",
    },
    {
        "name": "flow:in-review",
        "color": "1d76db",
        "role": "system",
        "description": "Set by Atobar: Evaluator and Arbitrator are verifying the PR",
    },
    {
        "name": "flow:needs-approval",
        "color": "d93f0b",
        "role": "system",
        "description": "Set by Atobar: waiting on you — review the plan and apply flow:approved (or close to cancel)",
    },
    {
        "name": "flow:escalated",
        "color": "b60205",
        "role": "system",
        "description": "Set by Atobar: waiting on you — circuit breaker paused run; comment & apply flow:resume",
    },
    {
        "name": "flow:shipped",
        "color": "6f42c1",
        "role": "system",
        "description": "Set by Atobar: PR passed verification and merged",
    },
    {
        "name": "flow:onboard",
        "color": "c5def5",
        "role": "control",
        "description": "Set by Atobar: repository onboarding specification issue",
    },
    {
        "name": "flow:metrics",
        "color": "bfd4f2",
        "role": "control",
        "description": "Set by Atobar: repository metric proposal issue",
    },
)

STATE_LABELS: frozenset[str] = frozenset({
    "flow:onboard",
    "flow:feature",
    "flow:fix",
    "flow:plan",
    "flow:needs-approval",
    "flow:approved",
    "flow:in-progress",
    "flow:in-review",
    "flow:shipped",
    "flow:escalated",
})

TRANSITIONS: dict[tuple[str, str], str] = {
    # From entry states:
    ("flow:onboard", "spec_ready"): "flow:needs-approval",
    ("flow:feature", "spec_ready"): "flow:needs-approval",
    ("flow:feature", "start"): "flow:in-progress",
    ("flow:fix", "spec_ready"): "flow:needs-approval",
    ("flow:fix", "start"): "flow:in-progress",
    ("flow:plan", "spec_ready"): "flow:needs-approval",
    # From needs-approval:
    ("flow:needs-approval", "approve"): "flow:approved",
    # From approved:
    ("flow:approved", "start"): "flow:in-progress",
    # From in-progress:
    ("flow:in-progress", "submit_review"): "flow:in-review",
    ("flow:in-progress", "escalate"): "flow:escalated",
    # From in-review:
    ("flow:in-review", "ship"): "flow:shipped",
    ("flow:in-review", "changes_requested"): "flow:in-progress",
    ("flow:in-review", "escalate"): "flow:escalated",
    # From escalated:
    ("flow:escalated", "resume"): "flow:in-progress",
}

NEXT_ACTIONS: dict[str, str] = {
    "flow:onboard": "Describe your business objective and evaluation metrics in the issue table.",
    "flow:feature": "Provide acceptance criteria and problem description for implementation.",
    "flow:fix": "Provide bug reproduction steps and expected behaviour.",
    "flow:plan": "Wait for the PLANNER agent to post a `flow-plan` proposal for your review.",
    "flow:needs-approval": (
        "Review the proposed `flow-plan` above and apply `flow:approved` to start coding, "
        "or comment with adjustments and re-apply `flow:plan`. Not happy with this plan? Close this issue to reject."
    ),
    "flow:approved": "Wait for the DEVELOPER agent to start implementation and open a pull request.",
    "flow:in-progress": "Wait for the DEVELOPER agent to push commits and submit a pull request.",
    "flow:in-review": "Wait for the EVALUATOR review verdict and external CI checks to finish.",
    "flow:shipped": "No action needed; this work has been verified and shipped.",
    "flow:escalated": (
        "Inspect the circuit breaker reason below, leave a comment clarifying requirements or scope if needed, "
        "and apply `flow:resume` to continue."
    ),
}


def transition(state: str, event: str) -> Optional[str]:
    """Pure transition function returning next state or None if transition is forbidden."""
    return TRANSITIONS.get((state, event))


def set_state_label(current_labels: Iterable[str], new_state: str) -> list[str]:
    """Setting a state removes all other flow:* state labels while preserving other labels."""
    preserved = [lbl for lbl in current_labels if lbl not in STATE_LABELS]
    if new_state not in preserved:
        preserved.append(new_state)
    return preserved


def render_onboarding_blocked_comment(
    repo: str,
    label: str,
    cp_url: str = "",
    missing_items: Optional[list[str]] = None,
) -> str:
    """Renders the deduplicated comment posted when a work label is applied before tenant activation."""
    from atobar_contracts.client_templates import resolve_cp_url

    base = resolve_cp_url(cp_url)
    tenant_url = f"{base}/tenants/{repo}"
    checklist = ""
    if missing_items:
        checklist = "\n" + "\n".join(f"- {item}" for item in missing_items) + "\n"
    return (
        f"{ONBOARDING_HOLD_MARKER}\n"
        f"⏸️ **flow:** `{label}` was removed because `{repo}` is still in `onboarding` status.\n"
        f"{checklist}\n"
        f"Complete setup and metric calibration in the [Control Panel — {repo}]({tenant_url}) "
        f"(first agent runs start once your LLM key, role models, and active metric calibration samples on `main` are ready), "
        f"then re-apply `{label}` to start.\n"
    )


def render_status_card(
    state: str,
    links: Optional[dict[str, str]] = None,
    spend: Optional[float] = None,
    caps: Optional[dict[str, Any]] = None,
    reason: Optional[str] = None,
    round_num: Optional[int] = None,
    entry_label: Optional[str] = None,
) -> str:
    """Renders the markdown status card with hidden marker for in-place editing."""
    if state == "flow:needs-approval" and entry_label in ("flow:onboard", "flow:metrics"):
        next_action = (
            "Review the generated onboarding/metric specification PR and apply `flow:approved` to proceed "
            "(or close this issue to reject)."
        )
    else:
        next_action = NEXT_ACTIONS.get(state, "Review current issue state and progress.")
    parts = [
        STATUS_MARKER,
        f"## atobar-flow Status: `{state}`",
        "",
        f"**Next Action:** {next_action}",
    ]

    if reason:
        parts.append("")
        parts.append(f"**Reason:** {reason}")

    if links:
        parts.append("")
        parts.append("**Links:**")
        for name, url in sorted(links.items()):
            parts.append(f"- [{name}]({url})")

    if spend is not None or caps or round_num is not None:
        parts.append("")
        parts.append("**Resource Usage:**")
        max_rounds = caps.get("max_review_rounds") if caps else None
        if round_num is not None and max_rounds is not None:
            parts.append(f"- Round: {round_num} / {max_rounds}")
        elif round_num is not None:
            parts.append(f"- Round: {round_num}")
        elif max_rounds is not None and state != "flow:escalated":
            parts.append(f"- Review rounds cap: {max_rounds}")
        if spend is not None:
            max_goal = caps.get("max_goal_cost_usd") if caps else None
            max_cost = caps.get("max_pr_cost_usd") if caps else None
            if max_goal is not None:
                parts.append(f"- Goal Spend: ${spend:.2f} / ${float(max_goal):.2f}")
            elif max_cost is not None:
                parts.append(f"- Spend: ${spend:.2f} / ${float(max_cost):.2f}")
            else:
                parts.append(f"- Spend: ${spend:.2f}")

    if state == "flow:escalated":
        cap_cost = (
            f"${float(caps['max_pr_cost_usd']):.2f}"
            if caps and "max_pr_cost_usd" in caps
            else "the configured cost cap"
        )
        cap_rounds = (
            f"{caps['max_review_rounds']} rounds"
            if caps and "max_review_rounds" in caps
            else "the configured round limit"
        )
        parts.append("")
        parts.append(
            f"**Resume Budget:** Applying `flow:resume` starts a fresh loop epoch "
            f"(allowing up to another {cap_cost} / {cap_rounds})."
        )

    if state != "flow:shipped":
        parts.append("")
        parts.append(
            "**Cancel:** Close this issue to cancel and stop compute "
            "(if a pull request is already open, close the PR as well)."
        )

    return "\n".join(parts) + "\n"
