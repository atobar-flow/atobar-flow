"""Execution entrypoint for the EVALUATOR role."""

import logging
import os
from typing import Any, Optional

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.registry import get_runtime
from flow_agent.core.config import get_bot_logins
from flow_agent.core.watchdog import CircuitBreaker
from atobar_contracts.envelopes import Plan, Verdict, fingerprint, parse
from flow_agent.roles.base import TurnOutcome
from flow_agent.roles.evaluator.prompt import EVALUATOR_SYSTEM_PROMPT
from flow_agent.roles.evaluator.spec import (
    StaleShaError,
    build_evaluator_context,
    post_evaluator_verdict,
)
from flow_agent.runtime.key_delivery import (
    export_llm_keys,
    fetch_run_llm_key,
    require_model,
)
from flow_agent.runtime.tools import allowlist_for
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry
from flow_agent.tools.git_tools import workspace_dir
from flow_agent.tools.vision_tools import load_recorded_screenshots

logger = logging.getLogger(__name__)


async def run_evaluator(
    tenant: str,
    pr: int,
    head_sha: str,
    task_prompt: str,
    pr_description: str = "",
    checks_summary: str = "",
    wiki: Optional[dict[str, Any]] = None,
    latest_plan: Optional[Plan] = None,
    gh_client: Optional[Any] = None,
    registry: Optional[SessionRegistry] = None,
    telemetry_ui: Optional[GitHubTelemetryUI] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    human_comments: Optional[list[str]] = None,
) -> TurnOutcome:
    """Runs the EVALUATOR role loop until a flow-verdict is emitted, then registers the flow/eval check."""
    cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL")
    run_id = os.environ.get("FLOW_RUN_ID")
    cred = os.environ.get("FLOW_RUN_CREDENTIAL")

    runtime_name = os.environ.get("FLOW_AGENT_RUNTIME", "nanobot")

    if not (cp_url and run_id and cred):
        raise ValueError("Failed to fetch tenant LLM key: no valid run_id/credential provided.")

    tenant_key = fetch_run_llm_key(control_panel_url=cp_url, run_id=run_id, credential=cred)
    export_llm_keys(tenant_key)
    if "GEMINI_API_KEY" in os.environ:
        del os.environ["GEMINI_API_KEY"]
    eval_model = require_model(tenant_key)

    runtime = get_runtime(runtime_name)

    context = build_evaluator_context(
        tenant=tenant,
        pr=pr,
        head_sha=head_sha,
        issue_text=task_prompt,
        pr_description=pr_description,
        checks_summary=checks_summary,
        wiki=wiki,
        latest_plan=latest_plan,
        human_comments=human_comments,
    )

    ws_dir = workspace_dir()
    task = RoleTask(
        role="evaluator",
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        context=context,
        task_prompt=task_prompt,
        workspace_dir=ws_dir,
        allowed_tools=allowlist_for("evaluator"),
        model_name=eval_model,
        done_envelope="flow-verdict",
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
            screens = load_recorded_screenshots(ws_dir)
            if screens:
                state_update["screenshots"] = screens
            telemetry_ui.update_state(state_update)

    envelope_text = (result.envelope_text or "") if result else ""
    comment_id: Optional[int] = None
    comment_body = envelope_text
    if gh_client is not None and result and result.envelope_text:
        bots = get_bot_logins()
        verdict = parse(result.envelope_text, author_login=bots["evaluator"], bots=bots)
        if isinstance(verdict, Verdict):
            for f in verdict.findings:
                f.fingerprint = fingerprint(f)
            findings_fps = [
                getattr(f, "fingerprint", str(f))
                for f in getattr(verdict, "findings", [])
            ]
            try:
                from flow_agent.telemetry.event_sink import emit
                emit(
                    "verdict",
                    round=verdict.round,
                    decision=verdict.verdict,
                    findings=findings_fps,
                    head_sha=verdict.head_sha,
                )
            except Exception:
                pass
            try:
                post_evaluator_verdict(
                    gh=gh_client,
                    tenant=tenant,
                    pr=pr,
                    verdict=verdict,
                    current_pr_head_sha=head_sha,
                )
                raw_cid = getattr(gh_client, "_last_verdict_comment_id", None)
                if isinstance(raw_cid, int):
                    comment_id = raw_cid
                raw_cbody = getattr(gh_client, "_last_verdict_comment_body", None)
                if isinstance(raw_cbody, str) and raw_cbody:
                    comment_body = raw_cbody
                    envelope_text = raw_cbody
            except StaleShaError as e:
                logger.warning("Stale SHA: %s", e)
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit("verdict", decision="stale_sha")
                except Exception:
                    pass
                gh_client.post_pr_comment(
                    repo=tenant,
                    pr_number=pr,
                    body=(
                        f"⚠️ **Stale SHA Detected:** Evaluator completed round on `{verdict.head_sha}`, "
                        f"but current PR head is `{head_sha}`. Evaluation skipped."
                    ),
                )

    return TurnOutcome(
        envelope_text=envelope_text,
        comment_id=comment_id,
        head_sha=head_sha,
        usage=usage_dict,
        comment_body=comment_body,
        halt=getattr(result, "halt", None) if result else None,
        halt_detail=getattr(result, "halt_detail", None) if result else None,
    )
