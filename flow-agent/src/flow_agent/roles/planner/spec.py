"""RoleSpec, done predicate, and context builder for the PLANNER role."""

from typing import Any, Optional

from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.llm.protocol import LLMClient, Reply
from flow_agent.roles.base import RoleSpec, format_wiki_section
from flow_agent.roles.planner.prompt import PLANNER_SYSTEM_PROMPT
from flow_agent.runtime.tools import allowlist_for


def planner_done_predicate(reply: Reply) -> bool:
    """Returns True if the planner has emitted a flow-plan envelope."""
    text = reply.text or ""
    return "<!-- atobar:flow-plan" in text or "```flow-plan" in text


def build_planner_context(
    tenant: str,
    pr: int,
    head_sha: str = "",
    wiki: Optional[dict[str, Any]] = None,
    manifest: Optional[dict[str, Any]] = None,
    human_comments: Optional[list[str]] = None,
) -> str:
    """Builds the static context prefix for the PLANNER role."""
    effective_sha = head_sha if (head_sha and len(head_sha) == 40) else "0" * 40
    sections: list[str] = [
        f"TENANT: {tenant}\nISSUE / PR: #{pr}\nHEAD SHA: {effective_sha}"
    ]

    if wiki:
        sections.append(format_wiki_section(wiki))

    if manifest:
        manifest_lines = ["TENANT MANIFEST SUMMARY:"]
        if "default_branch" in manifest:
            manifest_lines.append(f"- Default Branch: {manifest['default_branch']}")
        if "required_checks" in manifest:
            manifest_lines.append(f"- Required Checks: {manifest['required_checks']}")
        if "metrics_runtime" in manifest:
            manifest_lines.append(f"- Metrics Runtime: {manifest['metrics_runtime']}")
        goal_cfg = manifest.get("goal")
        if isinstance(goal_cfg, dict) and goal_cfg.get("objective"):
            manifest_lines.append(f"- Goal Targets: {goal_cfg.get('targets', [])}")
            manifest_lines.append(f"- Goal Guardrails: {goal_cfg.get('guardrails', [])}")
            manifest_lines.append(f"- Goal Autonomy: {goal_cfg.get('autonomy', 'approve')}")
            manifest_lines.append(
                "- Goal Objective:\n" + wrap_untrusted(str(goal_cfg["objective"]), source="tenant_goal_objective")
            )
        sections.append("\n".join(manifest_lines))

    if human_comments:
        joined_comments = "\n\n---\n\n".join(c.strip() for c in human_comments if c and c.strip())
        if joined_comments:
            sections.append(
                "HUMAN CLARIFICATIONS & COMMENTS ON ISSUE:\n"
                + wrap_untrusted(joined_comments, source="issue_comments")
            )

    return "\n\n".join(sections)


def get_planner_spec(
    llm: LLMClient,
    model_name: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> RoleSpec:
    """Returns the RoleSpec configuring the PLANNER role."""
    return RoleSpec(
        name="planner",
        system_prompt=custom_prompt or PLANNER_SYSTEM_PROMPT,
        tools=allowlist_for("planner"),
        llm=llm,
        done=planner_done_predicate,
        model_name=model_name,
    )
