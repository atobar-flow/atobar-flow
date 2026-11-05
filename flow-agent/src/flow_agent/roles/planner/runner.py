"""Execution entrypoint for the PLANNER role."""

import base64
import hashlib
import os
import re
from typing import Any, Optional

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.registry import get_runtime
from flow_agent.core.config import get_bot_logins
from flow_agent.core.watchdog import CircuitBreaker
from atobar_contracts.envelopes import (
    Plan,
    parse,
    parse_and_validate_goal_plan,
    render,
    render_hidden,
    validate_goal_plan,
)
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.roles.base import TurnOutcome
from flow_agent.roles.planner.prompt import PLANNER_SYSTEM_PROMPT
from flow_agent.roles.planner.spec import build_planner_context
from flow_agent.runtime.key_delivery import (
    export_llm_keys,
    fetch_run_llm_key,
    require_model,
)
from flow_agent.runtime.tools import allowlist_for
from flow_agent.telemetry.event_sink import emit
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry
from flow_agent.tools.git_tools import workspace_dir

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _extract_comment_id(res: Any) -> Optional[int]:
    if isinstance(res, dict) and isinstance(res.get("id"), int):
        return int(res["id"])
    if isinstance(res, int):
        return res
    return None


def _decode_goal_objective(raw_b64: str) -> str:
    if not raw_b64:
        return ""
    try:
        return base64.b64decode(raw_b64.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def format_plan_summary(plan: Plan) -> str:
    """Formats a human-readable Markdown summary of a Plan envelope."""
    lines = [f"### 🗺️ Implementation Plan (`{plan.complexity}`)", "", f"**Goal:** {plan.goal}"]
    if plan.scope_files:
        lines.append("\n**Scope Files:** " + ", ".join(f"`{f}`" for f in plan.scope_files))
    if plan.acceptance_criteria:
        lines.append("\n**Acceptance Criteria:**")
        for ac in plan.acceptance_criteria:
            lines.append(f"- **{ac.id}:** {ac.description}")
    if plan.implementation_steps:
        lines.append("\n**Implementation Steps:**")
        for idx, step_text in enumerate(plan.implementation_steps, 1):
            lines.append(f"{idx}. {step_text}")
    return "\n".join(lines)


def _publish_plan(
    tenant: str,
    pr: int,
    head_sha: str,
    envelope_text: str,
    gh_client: Optional[Any],
    manifest: Optional[dict[str, Any]] = None,
    *,
    force_goal_mode: bool = False,
) -> tuple[str, Optional[int], str]:
    bots = get_bot_logins()
    goal_plan = parse_and_validate_goal_plan(
        envelope_text,
        author_login=bots["planner"],
        bots=bots,
        manifest=manifest,
        force_goal_mode=force_goal_mode,
    )
    model_plan = goal_plan if goal_plan is not None else parse(envelope_text, author_login=bots["planner"], bots=bots)
    if not isinstance(model_plan, Plan):
        raise ValueError(f"Expected Plan envelope, got {type(model_plan)}")

    validate_goal_plan(model_plan, manifest=manifest, require_proposed=force_goal_mode)

    run_id = os.environ.get("FLOW_RUN_ID", "")
    effective_sha = head_sha if _SHA_RE.fullmatch(head_sha or "") else (
        model_plan.head_sha if _SHA_RE.fullmatch(model_plan.head_sha or "") else "0" * 40
    )

    plan = Plan(
        tenant=tenant,
        pr=pr,
        head_sha=effective_sha,
        run_id=run_id or model_plan.run_id,
        complexity=model_plan.complexity,
        goal=model_plan.goal,
        scope_files=model_plan.scope_files,
        out_of_scope=model_plan.out_of_scope,
        acceptance_criteria=model_plan.acceptance_criteria,
        implementation_steps=model_plan.implementation_steps,
        proposed_issues=model_plan.proposed_issues,
    )
    rendered = render(plan)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    comment_body = f"{format_plan_summary(plan)}\n\n{render_hidden(plan)}"

    comment_id: Optional[int] = None
    if gh_client is not None and not plan.proposed_issues:
        res = gh_client.post_pr_comment(repo=tenant, pr_number=pr, body=comment_body)
        comment_id = _extract_comment_id(res)
    try:
        emit(
            "envelope",
            envelope="plan",
            digest=digest,
            posted=gh_client is not None and not plan.proposed_issues,
            deduped=False,
            head_sha=effective_sha,
        )
    except Exception:
        pass
    return comment_body, comment_id, effective_sha


async def run_planner(
    tenant: str,
    pr: int,
    task_prompt: str,
    head_sha: str = "",
    wiki: Optional[dict[str, Any]] = None,
    manifest: Optional[dict[str, Any]] = None,
    registry: Optional[SessionRegistry] = None,
    telemetry_ui: Optional[GitHubTelemetryUI] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    gh_client: Optional[Any] = None,
    *,
    goal_mode: Optional[bool] = None,
    plan_count: Optional[int] = None,
    goal_objective: Optional[str] = None,
    human_comments: Optional[list[str]] = None,
) -> TurnOutcome:
    """Runs the PLANNER role loop until a flow-plan envelope is emitted and posted."""
    cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL")
    run_id = os.environ.get("FLOW_RUN_ID")
    cred = os.environ.get("FLOW_RUN_CREDENTIAL")

    runtime_name = os.environ.get("FLOW_AGENT_RUNTIME", "nanobot")
    effective_goal_mode = (
        goal_mode
        if goal_mode is not None
        else os.environ.get("FLOW_GOAL_MODE", "").strip().lower() in ("1", "true", "yes")
    )
    raw_count = plan_count if plan_count is not None else int(os.environ.get("FLOW_PLAN_COUNT", "0") or 0)
    effective_plan_count = max(1, min(3, raw_count)) if raw_count > 0 else 2
    effective_objective = (
        goal_objective
        if goal_objective is not None
        else _decode_goal_objective(os.environ.get("FLOW_GOAL_OBJECTIVE_B64", ""))
    )
    if not effective_objective and isinstance(manifest, dict):
        raw_g = manifest.get("goal")
        if isinstance(raw_g, dict):
            effective_objective = str(raw_g.get("objective") or "")

    if not (cp_url and run_id and cred):
        raise ValueError("Failed to fetch tenant LLM key: no valid run_id/credential provided.")

    tenant_key = fetch_run_llm_key(control_panel_url=cp_url, run_id=run_id, credential=cred)
    export_llm_keys(tenant_key)
    if "GEMINI_API_KEY" in os.environ:
        del os.environ["GEMINI_API_KEY"]

    role_model = require_model(tenant_key)
    runtime = get_runtime(runtime_name)

    effective_sha = head_sha if _SHA_RE.fullmatch(head_sha or "") else "0" * 40
    context = build_planner_context(
        tenant=tenant,
        pr=pr,
        head_sha=effective_sha,
        wiki=wiki,
        manifest=manifest,
        human_comments=human_comments,
    )

    ws_dir = workspace_dir()
    if effective_goal_mode:
        targets_list = []
        if isinstance(manifest, dict) and isinstance(manifest.get("goal"), dict):
            targets_list = list(manifest["goal"].get("targets") or [])
        wrapped_obj = wrap_untrusted(effective_objective or task_prompt, source="tenant_goal_objective")
        wrapped_task_prompt = (
            f"OUTER-LOOP GOAL PLANNING MODE (propose 1 to {effective_plan_count} concrete issue drafts):\n"
            f"- Allowed target_metric_key values: {targets_list}\n"
            f"- Emit a `flow-plan` envelope with `complexity: \"multi_issue_epic\"` and `proposed_issues` "
            f"(1 to {effective_plan_count} items).\n"
            f"- Repository Goal Objective:\n{wrapped_obj}"
        )
    else:
        wrapped_task_prompt = wrap_untrusted(task_prompt, source="issue_specification")
    task = RoleTask(
        role="planner",
        system_prompt=PLANNER_SYSTEM_PROMPT,
        context=context,
        task_prompt=wrapped_task_prompt,
        workspace_dir=ws_dir,
        allowed_tools=allowlist_for("planner"),
        model_name=role_model,
        done_envelope="flow-plan",
        llm=None,
    )

    hooks = RuntimeHooks(
        registry=registry,
        telemetry_ui=telemetry_ui,
        circuit_breaker=circuit_breaker,
    )

    result = await runtime.run(task, hooks=hooks)

    usage_dict: Optional[dict[str, Any]] = None
    if result and result.usage:
        usage_dict = result.usage.to_dict()
        if registry is not None:
            registry.usage = usage_dict
            await registry.heartbeat()
        if telemetry_ui is not None:
            state_update: dict[str, Any] = {
                "total_tokens": (result.usage.total.input_tokens or 0) + (result.usage.total.output_tokens or 0),
                "by_tool": {k: v.to_dict() for k, v in result.usage.by_tool.items()},
            }
            telemetry_ui.update_state(state_update)

    envelope_text = (result.envelope_text or "") if result else ""
    comment_id: Optional[int] = None
    comment_body = envelope_text
    if result and result.envelope_text:
        comment_body, comment_id, effective_sha = _publish_plan(
            tenant=tenant,
            pr=pr,
            head_sha=effective_sha,
            envelope_text=result.envelope_text,
            gh_client=gh_client,
            manifest=manifest,
            force_goal_mode=effective_goal_mode,
        )
        envelope_text = comment_body

    return TurnOutcome(
        envelope_text=envelope_text,
        comment_id=comment_id,
        head_sha=effective_sha,
        usage=usage_dict,
        comment_body=comment_body,
        halt=getattr(result, "halt", None) if result else None,
        halt_detail=getattr(result, "halt_detail", None) if result else None,
    )
