"""Execution entrypoint for the DEVELOPER role."""

import hashlib
import os
import subprocess
from typing import Any, Optional

from flow_agent.agents.base import RoleTask, RuntimeHooks
from flow_agent.agents.registry import get_runtime
from atobar_contracts.github_rest import RealGitHubClient
from flow_agent.core.config import get_bot_logins
from flow_agent.core.watchdog import CircuitBreaker
from atobar_contracts.envelopes import Plan, Verdict
from atobar_contracts.envelopes import Ack, parse, render, render_hidden
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.roles.base import TurnOutcome
from flow_agent.roles.developer.prompt import DEVELOPER_SYSTEM_PROMPT
from flow_agent.roles.developer.spec import build_developer_context
from flow_agent.runtime.key_delivery import (
    export_llm_keys,
    fetch_run_llm_key,
    require_model,
)
from flow_agent.runtime.tools import allowlist_for
from flow_agent.telemetry.event_sink import emit
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry
from flow_agent.tools.git_tools import (
    consume_workflow_handoff_note,
    create_and_verify_pr,
    workspace_dir,
)
from flow_agent.tools.vision_tools import load_recorded_screenshots, render_screenshot_with_provenance


def get_current_git_sha(cwd: Optional[str] = None) -> str:
    """Returns the current Git HEAD SHA."""
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _publish_ack(
    tenant: str,
    pr: int,
    envelope_text: str,
    gh_client: RealGitHubClient,
) -> tuple[str, Optional[int], str]:
    bots = get_bot_logins()
    model_ack = parse(envelope_text, author_login=bots["developer"], bots=bots)
    if not isinstance(model_ack, Ack):
        raise ValueError(f"Expected Ack envelope, got {type(model_ack)}")

    run_id = os.environ.get("FLOW_RUN_ID", "")

    # a. Push and open the PR. An ack without a PR sends the evaluator to review nothing,
    # except when every changed file was under .github/workflows/** (J42 WP-2).
    pr_result = create_and_verify_pr(
        title=f"flow: implementation for issue #{pr}",
        body=f"Automated implementation by atobar-flow developer (run {run_id}).\n\nCloses #{pr}",
        wait_ci=False,
    )
    workflow_note = consume_workflow_handoff_note()
    is_workflow_only = isinstance(pr_result, str) and pr_result.startswith("Workflow-only changes skipped push")
    if not pr_result.startswith("PR Creation Output") and not is_workflow_only:
        raise RuntimeError(f"Developer produced no PR; not posting ack: {pr_result}")

    # b. Rebind envelope to real pushed SHA
    pushed_sha = get_current_git_sha(cwd=workspace_dir())

    if is_workflow_only:
        note_body = workflow_note or pr_result
        res = gh_client.post_pr_comment(repo=tenant, pr_number=pr, body=note_body)
        comment_id = res.get("id") if isinstance(res, dict) and isinstance(res.get("id"), int) else (
            res if isinstance(res, int) else None
        )
        return "", comment_id, pushed_sha

    ack = Ack(
        tenant=tenant,
        pr=pr,
        head_sha=pushed_sha,
        run_id=run_id,
        addressed_findings=model_ack.addressed_findings,
        dispute=model_ack.dispute,
    )
    rendered = render(ack)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    # c. Dedup per pushed SHA and run_id (restarted runs on the same SHA must post their own run_id)
    comments = gh_client.get_comments(tenant, pr)
    for c in comments:
        body = c.get("body", "")
        same_run = not run_id or f'"run_id": "{run_id}"' in body or f'"run_id":"{run_id}"' in body
        if "```flow-ack" in body and pushed_sha in body and same_run:
            try:
                emit("envelope", envelope="ack", digest=digest, posted=False, deduped=True, head_sha=pushed_sha)
            except Exception:
                pass
            existing_id = c.get("id") if isinstance(c.get("id"), int) else None
            return body, existing_id, pushed_sha

    # d. Post (attaching any screenshots captured during the developer run + workflow handoff note)
    parts: list[str] = []
    if workflow_note:
        parts.append(workflow_note)
    parts.append(f"✅ **Developer** pushed `{pushed_sha[:7]}` ready for evaluation.")
    try:
        screens = load_recorded_screenshots(workspace_dir())
        md_lines = []
        for s in screens:
            url = (s.get("url") or "").strip()
            if url:
                md_lines.append(render_screenshot_with_provenance(s))
        if md_lines:
            parts.append("### 📸 Developer UI Screenshots\n\n" + "\n\n".join(md_lines))
    except Exception:
        pass
    parts.append(render_hidden(ack))
    comment_body = "\n\n".join(parts)
    res = gh_client.post_pr_comment(repo=tenant, pr_number=pr, body=comment_body)
    comment_id = res.get("id") if isinstance(res, dict) and isinstance(res.get("id"), int) else (
        res if isinstance(res, int) else None
    )
    try:
        emit("envelope", envelope="ack", digest=digest, posted=True, deduped=False, head_sha=pushed_sha)
    except Exception:
        pass
    return comment_body, comment_id, pushed_sha


async def run_developer(
    tenant: str,
    pr: int,
    task_prompt: str,
    wiki: Optional[dict[str, Any]] = None,
    latest_verdict: Optional[Verdict] = None,
    latest_plan: Optional[Plan] = None,
    registry: Optional[SessionRegistry] = None,
    telemetry_ui: Optional[GitHubTelemetryUI] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    gh_client: Optional[RealGitHubClient] = None,
    human_comments: Optional[list[str]] = None,
) -> TurnOutcome:
    """Runs the DEVELOPER role loop until an ack envelope is emitted."""
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

    role_model = require_model(tenant_key)
    runtime = get_runtime(runtime_name)

    context = build_developer_context(
        tenant=tenant,
        wiki=wiki,
        latest_verdict=latest_verdict,
        latest_plan=latest_plan,
        human_comments=human_comments,
    )

    ws_dir = workspace_dir()
    wrapped_task_prompt = wrap_untrusted(task_prompt, source="issue_specification")
    task = RoleTask(
        role="developer",
        system_prompt=DEVELOPER_SYSTEM_PROMPT,
        context=context,
        task_prompt=wrapped_task_prompt,
        workspace_dir=ws_dir,
        allowed_tools=allowlist_for("developer"),
        model_name=role_model,
        done_envelope="flow-ack",
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
    pushed_sha = os.environ.get("FLOW_HEAD_SHA", "")
    comment_body = envelope_text
    if result and result.envelope_text and gh_client is not None:
        comment_body, comment_id, pushed_sha = _publish_ack(tenant, pr, result.envelope_text, gh_client)
        envelope_text = comment_body

    return TurnOutcome(
        envelope_text=envelope_text,
        comment_id=comment_id,
        head_sha=pushed_sha,
        usage=usage_dict,
        comment_body=comment_body,
        halt=getattr(result, "halt", None) if result else None,
        halt_detail=getattr(result, "halt_detail", None) if result else None,
    )
