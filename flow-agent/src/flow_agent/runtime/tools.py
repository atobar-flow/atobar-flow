"""Per-role tool allowlists, registry, and dispatch."""

import inspect
from typing import Any, Optional

from flow_agent.llm.protocol import ToolSpec
from flow_agent.tools.ci_tools import git_ci_job_logs, git_pr_checks
from flow_agent.tools.exec_tool import execute_command
from flow_agent.tools.fs_tools import multi_edit, read_file, write_file
from flow_agent.tools.git_tools import create_and_verify_pr
from flow_agent.tools.search_tools import search_codebase
from flow_agent.tools.state_tools import todo_write
from flow_agent.tools.vision_tools import capture_evaluator_screenshot, inspect_ui

FLOW_TOOLS: list[Any] = [
    execute_command,
    multi_edit,
    read_file,
    write_file,
    search_codebase,
    inspect_ui,
    capture_evaluator_screenshot,
    git_pr_checks,
    git_ci_job_logs,
    todo_write,
    create_and_verify_pr,
]

_TOOL_MAP: dict[str, Any] = {tool.__name__: tool for tool in FLOW_TOOLS}

# Role allowlists per ADR-11 and Axiom 1:
# - DEVELOPER: all tools (SWE) except evaluator-only visual evidence tools
# - EVALUATOR: read/search/exec-read-only/vision/ci (no multi_edit, write_file, push)
# - ARBITRATOR: read/search/ci only (no execute_command, no writes)
_DEVELOPER_TOOLS: frozenset[str] = frozenset(
    t.__name__ for t in FLOW_TOOLS if t.__name__ != "capture_evaluator_screenshot"
)

_EVALUATOR_TOOLS: frozenset[str] = frozenset({
    "execute_command",
    "read_file",
    "search_codebase",
    "inspect_ui",
    "capture_evaluator_screenshot",
    "git_pr_checks",
    "git_ci_job_logs",
    "todo_write",
})

_ARBITRATOR_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "search_codebase",
    "git_pr_checks",
    "git_ci_job_logs",
})

_PLANNER_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "search_codebase",
    "git_pr_checks",
    "git_ci_job_logs",
    "todo_write",
})

ROLE_ALLOWLISTS: dict[str, frozenset[str]] = {
    "planner": _PLANNER_TOOLS,
    "developer": _DEVELOPER_TOOLS,
    "evaluator": _EVALUATOR_TOOLS,
    "arbitrator": _ARBITRATOR_TOOLS,
}


def allowlist_for(role: str) -> frozenset[str]:
    """Returns the set of tool names permitted for the given role."""
    r = role.strip().lower()
    if r not in ROLE_ALLOWLISTS:
        raise ValueError(
            f"Unknown role: {role!r}. Supported roles: {sorted(ROLE_ALLOWLISTS.keys())}"
        )
    return ROLE_ALLOWLISTS[r]


def get_tools_for(role: str) -> list[Any]:
    """Returns the list of callable tool functions permitted for the given role."""
    allowed = allowlist_for(role)
    return [t for t in FLOW_TOOLS if t.__name__ in allowed]


def get_tool_specs_for(role: str) -> list[ToolSpec]:
    """Returns the list of ToolSpec declarations for the given role."""
    allowed = allowlist_for(role)
    specs = []
    for tool_func in FLOW_TOOLS:
        name = tool_func.__name__
        if name in allowed:
            doc = inspect.getdoc(tool_func) or f"Tool {name}"
            specs.append(
                ToolSpec(
                    name=name,
                    description=doc.split("\n\n")[0],
                    parameters={"type": "object", "properties": {}},
                )
            )
    return specs


async def execute_tool(
    tool_name: str,
    args: dict[str, Any],
    role: Optional[str] = None,
) -> Any:
    """Dynamically executes a tool by name, handling both sync and async functions."""
    if role is not None:
        allowed = allowlist_for(role)
        if tool_name not in allowed:
            raise ValueError(
                f"Tool '{tool_name}' is not in the allowlist for role '{role}'"
            )

    if tool_name not in _TOOL_MAP:
        return f"Error: Tool '{tool_name}' not found."

    tool = _TOOL_MAP[tool_name]
    try:
        if inspect.iscoroutinefunction(tool):
            result = await tool(**args)
        else:
            result = tool(**args)
        return result
    except Exception as e:
        return f"Error executing {tool_name}: {str(e)}"
