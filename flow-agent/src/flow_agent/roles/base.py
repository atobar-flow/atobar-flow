"""Base classes and specifications for swarm roles."""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.llm.protocol import LLMClient, Reply
from atobar_contracts.envelopes import Plan


@dataclass
class RoleSpec:
    """Specification configuring a swarm role for execution in llm_loop."""

    name: str
    system_prompt: str
    tools: frozenset[str]
    llm: LLMClient
    done: Callable[[Reply], bool]
    model_name: Optional[str] = None


@dataclass
class TurnOutcome:
    """Structured outcome of a single role turn execution for warm Spot supervisor handover."""

    envelope_text: str
    comment_id: Optional[int] = None
    head_sha: str = ""
    usage: Optional[dict[str, Any]] = None
    comment_body: str = ""
    wall_clock_minutes: float = 0.0
    halt: Optional[str] = None
    halt_detail: Optional[str] = None


def format_wiki_section(wiki: dict[str, Any]) -> str:
    """Formats a SotaWiki dictionary into a deterministic prompt section wrapped as untrusted tenant content."""
    body_lines: list[str] = []
    if wiki.get("purpose"):
        body_lines.append(f"- Purpose: {wiki['purpose']}")
    techs = wiki.get("technologies") if wiki.get("technologies") is not None else wiki.get("key_technologies")
    if techs:
        if isinstance(techs, list):
            body_lines.append("- Technologies:")
            for item in techs:
                body_lines.append(f"  * {item}")
        else:
            body_lines.append(f"- Technologies: {techs}")
    structure = wiki.get("structure") if wiki.get("structure") is not None else wiki.get("top_level_structure")
    if structure:
        body_lines.append(f"- Structure: {structure}")
    concepts = wiki.get("concepts") if wiki.get("concepts") is not None else wiki.get("key_concepts")
    if concepts:
        if isinstance(concepts, list):
            body_lines.append("- Key Concepts:")
            for item in concepts:
                body_lines.append(f"  * {item}")
        else:
            body_lines.append(f"- Key Concepts: {concepts}")
    mermaid = str(wiki.get("architecture_mermaid") or "").strip().replace("```", "")
    if mermaid:
        body_lines.append(f"- Architecture Diagram:\n```mermaid\n{mermaid}\n```")
    return "TENANT KNOWLEDGE / WIKI:\n" + wrap_untrusted("\n".join(body_lines), source="tenant_wiki")


def format_plan_section(plan: Plan) -> str:
    """Formats a flow-plan Task Ledger into a deterministic prompt section."""
    lines = [
        "APPROVED IMPLEMENTATION PLAN (flow-plan TASK LEDGER):",
        f"- Complexity: {plan.complexity}",
        f"- Goal: {plan.goal}",
    ]
    if plan.scope_files:
        lines.append(f"- Target Scope Files: {', '.join(plan.scope_files)}")
    if plan.out_of_scope:
        lines.append(f"- Out of Scope: {', '.join(plan.out_of_scope)}")
    if plan.acceptance_criteria:
        lines.append("- Planned Acceptance Criteria:")
        for ac in plan.acceptance_criteria:
            cmd_suffix = f" (verify: `{ac.verification_cmd}`)" if ac.verification_cmd else ""
            lines.append(f"  * [{ac.id}] {ac.description}{cmd_suffix}")
    if plan.implementation_steps:
        lines.append("- Implementation Steps:")
        for idx, step_text in enumerate(plan.implementation_steps, start=1):
            lines.append(f"  {idx}. {step_text}")
    return "\n".join(lines)

