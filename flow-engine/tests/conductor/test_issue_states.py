import ast
import pathlib

import pytest

from flow_engine.conductor.issue_states import (
    COMMAND_LABELS,
    FLOW_LABEL_CATALOG,
    STATE_LABELS,
    render_status_card,
    set_state_label,
    transition,
)

ALLOWED_TRANSITIONS = [
    ("flow:onboard", "spec_ready", "flow:needs-approval"),
    ("flow:feature", "spec_ready", "flow:needs-approval"),
    ("flow:feature", "start", "flow:in-progress"),
    ("flow:fix", "spec_ready", "flow:needs-approval"),
    ("flow:fix", "start", "flow:in-progress"),
    ("flow:plan", "spec_ready", "flow:needs-approval"),
    ("flow:needs-approval", "approve", "flow:approved"),
    ("flow:approved", "start", "flow:in-progress"),
    ("flow:in-progress", "submit_review", "flow:in-review"),
    ("flow:in-progress", "escalate", "flow:escalated"),
    ("flow:in-review", "ship", "flow:shipped"),
    ("flow:in-review", "changes_requested", "flow:in-progress"),
    ("flow:in-review", "escalate", "flow:escalated"),
    ("flow:escalated", "resume", "flow:in-progress"),
]

FORBIDDEN_TRANSITIONS = [
    ("flow:shipped", "start"),
    ("flow:shipped", "ship"),
    ("flow:onboard", "ship"),
    ("flow:onboard", "start"),
    ("flow:needs-approval", "start"),
    ("flow:needs-approval", "ship"),
    ("flow:approved", "ship"),
    ("flow:approved", "submit_review"),
    ("flow:in-progress", "ship"),
    ("flow:in-review", "approve"),
    ("flow:in-review", "start"),
    ("flow:triaged", "spec_ready"),
    ("flow:rejected", "ship"),
    ("unknown:state", "start"),
    ("flow:onboard", "unknown_event"),
]


@pytest.mark.parametrize("initial_state,event,expected_state", ALLOWED_TRANSITIONS)
def test_ac1_allowed_transitions(initial_state, event, expected_state):
    assert transition(initial_state, event) == expected_state


@pytest.mark.parametrize("initial_state,event", FORBIDDEN_TRANSITIONS)
def test_ac1_forbidden_transitions(initial_state, event):
    assert transition(initial_state, event) is None


@pytest.mark.parametrize("state", list(STATE_LABELS))
def test_ac2_card_always_names_exactly_one_next_human_action(state):
    card = render_status_card(state)
    assert "<!-- atobar:status -->" in card
    assert state in card
    assert "**Next Action:**" in card

    # Exactly one Next Action section/line
    lines = [line for line in card.splitlines() if line.startswith("**Next Action:**")]
    assert len(lines) == 1
    # Ensure there is descriptive action text after prefix
    action_text = lines[0].replace("**Next Action:**", "").strip()
    assert len(action_text) > 10


def test_ac2_card_renders_links_and_spend():
    links = {"Manifest PR": "https://github.com/atobar-flow/atobar-flow/pull/42"}
    caps = {"max_pr_cost_usd": 25.0, "max_review_rounds": 4}
    card = render_status_card("flow:in-review", links=links, spend=12.50, caps=caps, round_num=2)

    assert "https://github.com/atobar-flow/atobar-flow/pull/42" in card
    assert "Manifest PR" in card
    assert "$12.50" in card
    assert "$25.00" in card
    assert "Round: 2 / 4" in card
    assert "Close this issue to cancel and stop compute" in card


def test_escalated_card_renders_reason_budget_reset_and_cancel():
    caps = {"max_pr_cost_usd": 2.0, "max_review_rounds": 4}
    card = render_status_card(
        "flow:escalated",
        spend=2.05,
        caps=caps,
        reason="max_pr_cost_usd reached ($2.05 >= $2.00)",
        round_num=2,
    )
    assert "**Reason:** max_pr_cost_usd reached ($2.05 >= $2.00)" in card
    assert "Round: 2 / 4" in card
    assert "Spend: $2.05 / $2.00" in card
    assert "Review rounds cap:" not in card
    assert "Applying `flow:resume` starts a fresh loop epoch (allowing up to another $2.00 / 4 rounds)." in card
    assert "**Cancel:** Close this issue to cancel and stop compute" in card


def test_needs_approval_card_distinguishes_onboard_vs_plan():
    plan_card = render_status_card("flow:needs-approval", entry_label="flow:plan")
    assert "flow-plan" in plan_card
    assert "Close this issue to reject." in plan_card

    onb_card = render_status_card("flow:needs-approval", entry_label="flow:onboard")
    assert "onboarding/metric specification PR" in onb_card
    assert "flow-plan" not in onb_card


def test_label_catalog_and_command_labels_diet():
    from atobar_contracts.client_templates import CONTROL_LABELS, render_trigger
    from atobar_contracts.sufficiency import FAST_TRACK_LABELS

    assert "flow:triaged" not in STATE_LABELS
    assert "flow:rejected" not in STATE_LABELS
    assert "flow:docs" not in FAST_TRACK_LABELS
    assert COMMAND_LABELS == frozenset({"flow:approved", "flow:resume"})

    catalog_names = {entry["name"] for entry in FLOW_LABEL_CATALOG}
    colors = [entry["color"] for entry in FLOW_LABEL_CATALOG]
    assert len(colors) == len(set(colors)), "Every label in FLOW_LABEL_CATALOG must have a unique hex color"

    human_labels = [entry["name"] for entry in FLOW_LABEL_CATALOG if entry["role"] == "human"]
    system_labels = [entry["name"] for entry in FLOW_LABEL_CATALOG if entry["role"] == "system"]
    assert human_labels == ["flow:feature", "flow:fix", "flow:plan", "flow:approved", "flow:resume"]
    assert system_labels == [
        "flow:in-progress",
        "flow:in-review",
        "flow:needs-approval",
        "flow:escalated",
        "flow:shipped",
    ]
    assert STATE_LABELS.issubset(catalog_names)
    assert FAST_TRACK_LABELS.issubset(catalog_names)
    assert COMMAND_LABELS.issubset(catalog_names)
    assert CONTROL_LABELS.issubset(catalog_names)

    trigger_yml = render_trigger("o__r", "https://cp.example.invalid")
    assert "flow:docs" not in trigger_yml
    for h_label in human_labels:
        assert f"github.event.label.name == '{h_label}'" in trigger_yml


def test_ac3_setting_state_removes_other_flow_state_labels():
    # Only the target state is kept; other flow:* state labels removed
    res = set_state_label(["flow:onboard"], "flow:needs-approval")
    assert res == ["flow:needs-approval"]

    # Preserves non-state labels
    res = set_state_label(["flow:onboard", "bug", "area:ui"], "flow:needs-approval")
    assert sorted(res) == ["area:ui", "bug", "flow:needs-approval"]

    # Removes multiple conflicting state labels
    res = set_state_label(
        ["flow:onboard", "flow:plan", "flow:needs-approval", "documentation"],
        "flow:approved",
    )
    assert sorted(res) == ["documentation", "flow:approved"]


def test_no_llm_imports_in_conductor():
    conductor_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "flow_engine" / "conductor"
    llm_keywords = ["google.genai", "google.generativeai", "openai", "anthropic", "litellm", "langchain"]

    for py_file in conductor_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in llm_keywords:
                        assert forbidden not in alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for forbidden in llm_keywords:
                        assert forbidden not in node.module
