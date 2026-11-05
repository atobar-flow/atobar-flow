"""RoleSpec, done predicate, and context builder for the DEVELOPER role."""

from typing import Any, Optional

from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.llm.protocol import LLMClient, Reply
from atobar_contracts.envelopes import Ack, Plan, Verdict
from flow_agent.roles.base import RoleSpec, format_plan_section, format_wiki_section
from flow_agent.roles.developer.prompt import DEVELOPER_SYSTEM_PROMPT
from flow_agent.runtime.tools import allowlist_for


def developer_done_predicate(reply: Reply) -> bool:
    """Returns True if the developer has posted or emitted a valid flow-ack envelope."""
    text = reply.text or ""
    if "<!-- atobar:flow-ack" in text or "```flow-ack" in text:
        return True
    return False


def create_developer_ack(
    tenant: str,
    pr: int,
    pushed_sha: str,
    addressed_findings: Optional[list[str]] = None,
    dispute: Optional[list[Any]] = None,
) -> Ack:
    """Builds a validated Ack envelope bound to the pushed SHA."""
    return Ack(
        tenant=tenant,
        pr=pr,
        head_sha=pushed_sha,
        addressed_findings=addressed_findings or [],
        dispute=dispute or [],
    )


def build_developer_context(
    tenant: str,
    wiki: Optional[dict[str, Any]] = None,
    latest_verdict: Optional[Verdict] = None,
    latest_plan: Optional[Plan] = None,
    human_comments: Optional[list[str]] = None,
) -> str:
    """Assembles initial context for the developer role, incorporating wiki, plan, and latest review verdict."""
    sections: list[str] = [f"TENANT: {tenant}"]

    if wiki:
        sections.append(format_wiki_section(wiki))

    if latest_plan:
        sections.append(format_plan_section(latest_plan))
        if latest_plan.scope_files:
            sections.append(
                "The PLANNER already localized the repository for this plan: read only the Target Scope "
                "Files you need, then write the failing test. Do not re-explore unrelated files."
            )

    if latest_verdict:
        verdict_lines = [
            "LATEST REVIEW VERDICT (RE-DISPATCH INPUT):",
            f"- Status: {latest_verdict.verdict}",
            f"- Evaluated Head SHA: {latest_verdict.head_sha}",
            f"- Review Round: {latest_verdict.round}",
        ]
        if latest_verdict.findings:
            finding_lines = [
                f"  * [{f.severity}] {f.criterion}: {f.evidence} (id: {f.id})"
                + (f" — {f.message}" if getattr(f, "message", None) else "")
                for f in latest_verdict.findings
            ]
            verdict_lines.append(
                "- Review Findings:\n" + wrap_untrusted("\n".join(finding_lines), source="evaluator_findings")
            )
        sections.append("\n".join(verdict_lines))

    if human_comments:
        joined_comments = "\n\n---\n\n".join(c.strip() for c in human_comments if c and c.strip())
        if joined_comments:
            sections.append(
                "HUMAN CLARIFICATIONS & COMMENTS ON ISSUE:\n"
                + wrap_untrusted(joined_comments, source="issue_comments")
            )

    return "\n\n".join(sections)


def get_developer_spec(
    llm: LLMClient,
    model_name: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> RoleSpec:
    """Returns the RoleSpec configuring the DEVELOPER role."""
    return RoleSpec(
        name="developer",
        system_prompt=custom_prompt or DEVELOPER_SYSTEM_PROMPT,
        tools=allowlist_for("developer"),
        llm=llm,
        done=developer_done_predicate,
        model_name=model_name,
    )
