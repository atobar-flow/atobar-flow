import ast
import pathlib

from flow_engine.conductor.sufficiency import SufficiencyDecision, assess_issue_sufficiency


def test_ac1_fast_track_well_specified_roadmap_issue():
    issue_body = """
## Goal
Add timeout support to the HTTP client.

## Scope
- `src/client/http.py`
- `tests/test_http.py`

## Out of scope
Retry policies.

## Acceptance criteria (tests first)
- [ ] AC-1: `fetch()` raises `TimeoutError` after `timeout_sec`.
- [ ] `pytest tests/test_http.py -q` passes.
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:feature")
    assert isinstance(decision, SufficiencyDecision)
    assert decision.route == "developer"
    assert decision.reason


def test_ac2_fast_track_bug_report_with_backticked_path_and_repro():
    issue_body = """
Clicking export in `src/ui/export_modal.tsx` crashes when the table is empty.

## Steps to reproduce
1. Open the dashboard with 0 rows.
2. Click Export CSV.

## Expected
The button is disabled or exports an empty header without throwing TypeError.
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:fix")
    assert decision.route == "developer"


def test_ac3_plan_first_when_label_is_flow_plan():
    issue_body = """
## Scope
- `src/client/http.py`

## Acceptance criteria
- [ ] AC-1: Works.
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:plan")
    assert decision.route == "planner"
    assert "flow:plan" in decision.reason


def test_ac4_plan_first_when_underspecified_missing_scope():
    issue_body = """
Fix dashboard overflow on mobile screens.

## Acceptance criteria
- [ ] Dashboard does not overflow horizontally.
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:fix")
    assert decision.route == "planner"
    assert "scope" in decision.reason.lower() or "file" in decision.reason.lower()


def test_ac5_plan_first_when_underspecified_missing_acceptance_criteria():
    issue_body = """
We should refactor `src/flow_engine/main.py` to make it cleaner and faster.
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:feature")
    assert decision.route == "planner"
    assert "acceptance" in decision.reason.lower() or "criteria" in decision.reason.lower()


def test_ac6_plan_first_on_unfilled_template_placeholders():
    issue_body = """
## Goal
<one outcome, one or two sentences>

## Scope
- <exact files to add or change>

## Acceptance criteria (tests first)
- [ ] AC-1:
"""
    decision = assess_issue_sufficiency(issue_body, label="flow:feature")
    assert decision.route == "planner"


def test_ac7_no_llm_or_network_imports_in_sufficiency():
    path = pathlib.Path(__file__).resolve().parents[2] / "src" / "flow_engine" / "conductor" / "sufficiency.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = [
        "google.genai",
        "google.generativeai",
        "openai",
        "anthropic",
        "litellm",
        "langchain",
        "requests",
        "urllib",
        "httpx",
        "socket",
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for f in forbidden:
                    assert f not in alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for f in forbidden:
                    assert f not in node.module
