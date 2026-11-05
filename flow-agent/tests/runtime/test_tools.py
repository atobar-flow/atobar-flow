import pytest

from flow_agent.runtime.tools import (
    allowlist_for,
    get_tool_specs_for,
    get_tools_for,
)

# Table of (tool_name, role, expected_allowed)
TOOL_ROLE_TABLE = [
    # developer has all tools
    ("execute_command", "developer", True),
    ("multi_edit", "developer", True),
    ("read_file", "developer", True),
    ("write_file", "developer", True),
    ("search_codebase", "developer", True),
    ("inspect_ui", "developer", True),
    ("git_pr_checks", "developer", True),
    ("git_ci_job_logs", "developer", True),
    ("todo_write", "developer", True),
    ("create_and_verify_pr", "developer", True),

    # evaluator is read-only (no multi_edit, write_file, create_and_verify_pr)
    ("execute_command", "evaluator", True),
    ("read_file", "evaluator", True),
    ("search_codebase", "evaluator", True),
    ("inspect_ui", "evaluator", True),
    ("git_pr_checks", "evaluator", True),
    ("git_ci_job_logs", "evaluator", True),
    ("todo_write", "evaluator", True),
    ("multi_edit", "evaluator", False),
    ("write_file", "evaluator", False),
    ("create_and_verify_pr", "evaluator", False),

    # arbitrator cannot execute commands or write files
    ("read_file", "arbitrator", True),
    ("search_codebase", "arbitrator", True),
    ("git_pr_checks", "arbitrator", True),
    ("git_ci_job_logs", "arbitrator", True),
    ("execute_command", "arbitrator", False),
    ("multi_edit", "arbitrator", False),
    ("write_file", "arbitrator", False),
    ("inspect_ui", "arbitrator", False),
    ("todo_write", "arbitrator", False),
    ("create_and_verify_pr", "arbitrator", False),
]


@pytest.mark.parametrize("tool_name,role,expected_allowed", TOOL_ROLE_TABLE)
def test_ac1_table_test_every_tool_x_role(tool_name, role, expected_allowed):
    allowed = allowlist_for(role)
    assert (tool_name in allowed) == expected_allowed


def test_ac2_registry_exposes_only_allowlisted_declarations():
    # Developer
    dev_specs = get_tool_specs_for("developer")
    dev_names = {s.name for s in dev_specs}
    assert dev_names == allowlist_for("developer")
    assert "multi_edit" in dev_names
    assert "create_and_verify_pr" in dev_names

    # Evaluator
    eval_specs = get_tool_specs_for("evaluator")
    eval_names = {s.name for s in eval_specs}
    assert eval_names == allowlist_for("evaluator")
    assert "multi_edit" not in eval_names
    assert "write_file" not in eval_names
    assert "create_and_verify_pr" not in eval_names
    assert "read_file" in eval_names

    # Arbitrator
    arb_specs = get_tool_specs_for("arbitrator")
    arb_names = {s.name for s in arb_specs}
    assert arb_names == allowlist_for("arbitrator")
    assert "execute_command" not in arb_names
    assert "write_file" not in arb_names
    assert "read_file" in arb_names


def test_get_tools_for_returns_callables():
    eval_tools = get_tools_for("evaluator")
    names = {t.__name__ for t in eval_tools}
    assert names == allowlist_for("evaluator")


def test_unknown_role_raises_error():
    with pytest.raises(ValueError, match="Unknown role"):
        allowlist_for("unknown_role")
