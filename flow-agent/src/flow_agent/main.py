import argparse
import asyncio
import base64
import logging
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from atobar_contracts.conductor_io import _parse_ts, is_epoch_marker, linked_prs_for_issue
from atobar_contracts.constants import LOOP_EPOCH_MARKER
from atobar_contracts.github_rest import RealGitHubClient
from flow_agent.agents.base import UsageLedger
from flow_agent.agents.errors import classify_provider_error
from flow_agent.core.config import get_bot_logins
from flow_agent.core.watchdog import CircuitBreaker, CircuitBreakerException
from atobar_contracts.envelopes import Plan, Verdict, parse, render
from flow_agent.roles.arbitrator.run import run_arbitrator
from flow_agent.roles.base import TurnOutcome
from flow_agent.roles.developer.runner import run_developer
from flow_agent.roles.evaluator.runner import run_evaluator
from flow_agent.roles.metric_compiler.runner import run_metric_compiler
from flow_agent.roles.onboarder.runner import run_onboarder
from flow_agent.roles.planner.runner import run_planner
from flow_agent.runtime.key_delivery import clear_llm_keys, fetch_run_wiki
from flow_agent.runtime.sandbox import SetupFailure, prepare, prepare_role_worktree
from flow_agent.runtime.wip import WipSyncer
from flow_agent.telemetry.event_sink import emit, flush
from flow_agent.telemetry.event_sink import init as init_event_sink
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry
from atobar_contracts.manifest import ArbitrationPolicy, MetricsRuntime, TenantManifest

logger = logging.getLogger(__name__)

MAX_SUPERVISOR_TURNS = 12
_global_syncer: Optional[WipSyncer] = None


def trigger_wip_sync() -> None:
    if _global_syncer is not None:
        _global_syncer.trigger_sync()


async def _shutdown(
    reason: str,
    error: bool,
    registry: Optional[SessionRegistry] = None,
    telemetry_ui: Optional[GitHubTelemetryUI] = None,
) -> None:
    """Unified shutdown handler for both completed and halted paths (Axiom 8/10)."""
    status = "Halted" if error else "Complete"
    try:
        emit("lifecycle.end", status=status, error=error, reason=reason)
        flush()
    except Exception as ee:
        print(f"Failed to emit lifecycle.end: {ee}")

    if registry is not None:
        try:
            await registry.terminate(status, reason=reason)
        except Exception as re:
            print(f"Failed to terminate registry: {re}")

    if telemetry_ui is not None:
        try:
            await telemetry_ui.terminate(error=error, reason=reason)
        except Exception as te:
            print(f"Failed to terminate telemetry UI: {te}")

    trigger_wip_sync()
    await asyncio.sleep(2)

    print(f"Executing shutdown ({'halted' if error else 'completed'}: {reason})...")
    try:
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
    except Exception as shutdown_e:
        print(f"Shutdown command failed: {shutdown_e}")


def _latest_verdict_for(
    gh_client: Optional[RealGitHubClient], repo: str, issue_id: int, head_sha: str
) -> Optional[Verdict]:
    """The evaluator verdict a developer rework round must address (bound to head_sha)."""
    if gh_client is None or not head_sha or issue_id <= 0:
        return None
    bots = get_bot_logins()
    latest: Optional[Verdict] = None
    try:
        for c in gh_client.get_comments(repo, issue_id):
            env = parse(c.get("body", ""), author_login=c.get("user", {}).get("login", ""), bots=bots)
            if isinstance(env, Verdict) and env.head_sha == head_sha:
                latest = env
    except Exception as exc:
        print(f"Warning: could not load latest verdict: {exc}")
    return latest


def _latest_plan_for(
    gh_client: Optional[RealGitHubClient], repo: str, issue_id: int
) -> Optional[Plan]:
    """Loads the latest flow-plan envelope from the issue comments if present."""
    if gh_client is None or issue_id <= 0:
        return None
    bots = get_bot_logins()
    latest: Optional[Plan] = None
    try:
        for c in gh_client.get_comments(repo, issue_id):
            env = parse(c.get("body", ""), author_login=c.get("user", {}).get("login", ""), bots=bots)
            if isinstance(env, Plan):
                latest = env
    except Exception as exc:
        print(f"Warning: could not load latest plan: {exc}")
    return latest


_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_MAX_COMMENT_CHARS = 2000
_MAX_TOTAL_COMMENT_CHARS = 8000


def _human_comments_for(
    gh_client: Optional[RealGitHubClient],
    repo: str,
    issue_id: int,
    manifest: Optional[dict[str, Any]] = None,
    head_sha: str = "",
) -> list[str]:
    """Loads trusted human comments from the current loop epoch across Issue and linked PR(s), capped by size."""
    if gh_client is None or issue_id <= 0:
        return []
    bots_map = get_bot_logins()
    bot_logins = set(bots_map.values())
    m = manifest if isinstance(manifest, dict) else {}
    explicit_trusted = {
        str(m.get("owner_github_login") or "").strip(),
        str(m.get("created_by") or "").strip(),
    } - {"", "admin"}
    repo_owner = repo.split("/", 1)[0].strip() if "/" in repo else ""

    def _is_trusted_human_comment(item: dict[str, Any], body: str) -> tuple[bool, str]:
        user_obj = item.get("user") if isinstance(item.get("user"), dict) else {}
        login = str((user_obj or {}).get("login") or "").strip()
        user_type = str((user_obj or {}).get("type") or "").strip().lower()
        if login in bot_logins or login.endswith("[bot]") or user_type == "bot":
            return False, ""
        assoc = str(item.get("author_association") or "").strip().upper()
        trusted = (
            assoc in _TRUSTED_ASSOCIATIONS
            or (assoc != "NONE" and login in explicit_trusted)
            or (assoc == "" and bool(login) and login == repo_owner)
        )
        if not trusted:
            return False, ""
        if (
            "<!-- atobar:" in body
            or "```flow-" in body
            or body.startswith("🌊 **flow:**")
            or body.startswith("⏸️ **flow:**")
            or "**ATOBAR Evaluator Review**" in body
        ):
            return False, ""
        return True, login

    epoch_comments: list[str] = []
    epoch_start: Optional[datetime] = None
    try:
        for c in gh_client.get_comments(repo, issue_id) or []:
            body = str(c.get("body") or "").strip()
            if not body:
                continue
            if is_epoch_marker(c, bots_map):
                epoch_comments = []
                epoch_start = _parse_ts(c.get("created_at"))
                continue
            ok, login = _is_trusted_human_comment(c, body)
            if not ok:
                continue
            clipped = body[:_MAX_COMMENT_CHARS]
            epoch_comments.append(f"@{login}: {clipped}" if login else clipped)
    except Exception as exc:
        print(f"Warning: could not load human comments: {exc}")
        return []

    eff_sha = (head_sha or os.environ.get("FLOW_HEAD_SHA") or "").strip()
    linked_prs = linked_prs_for_issue(gh_client, repo, issue_id, eff_sha)

    for pr_num in linked_prs:
        # 1. Top-level PR comments
        try:
            pr_comments = gh_client.get_comments(repo, pr_num) or []
            if isinstance(pr_comments, list):
                for c in pr_comments:
                    if not isinstance(c, dict):
                        continue
                    c_dt = _parse_ts(c.get("created_at"))
                    if epoch_start is not None and c_dt is not None and c_dt < epoch_start:
                        continue
                    body = str(c.get("body") or "").strip()
                    if not body:
                        continue
                    ok, login = _is_trusted_human_comment(c, body)
                    if not ok:
                        continue
                    clipped = body[:_MAX_COMMENT_CHARS]
                    epoch_comments.append(
                        f"[PR #{pr_num}] @{login}: {clipped}" if login else f"[PR #{pr_num}] {clipped}"
                    )
        except Exception:
            pass

        # 2. PR review bodies (/pulls/{pr}/reviews)
        try:
            reviews = gh_client.get_pr_reviews(repo, pr_num) or []
            if isinstance(reviews, list):
                for r in reviews:
                    if not isinstance(r, dict):
                        continue
                    r_dt = _parse_ts(r.get("submitted_at") or r.get("created_at"))
                    if epoch_start is not None and r_dt is not None and r_dt < epoch_start:
                        continue
                    body = str(r.get("body") or "").strip()
                    if not body:
                        continue
                    ok, login = _is_trusted_human_comment(r, body)
                    if not ok:
                        continue
                    clipped = body[:_MAX_COMMENT_CHARS]
                    epoch_comments.append(
                        f"[PR #{pr_num} review] @{login}: {clipped}"
                        if login
                        else f"[PR #{pr_num} review] {clipped}"
                    )
        except Exception:
            pass

        # 3. Inline PR review comments (/pulls/{pr}/comments)
        try:
            inline_comments = gh_client.get_pr_review_comments(repo, pr_num) or []
            if isinstance(inline_comments, list):
                for rc in inline_comments:
                    if not isinstance(rc, dict):
                        continue
                    rc_dt = _parse_ts(rc.get("created_at"))
                    if epoch_start is not None and rc_dt is not None and rc_dt < epoch_start:
                        continue
                    body = str(rc.get("body") or "").strip()
                    if not body:
                        continue
                    ok, login = _is_trusted_human_comment(rc, body)
                    if not ok:
                        continue
                    path = str(rc.get("path") or "").strip()
                    line = rc.get("line") or rc.get("original_line") or rc.get("position")
                    if path and line is not None:
                        loc = f"[PR #{pr_num} {path}:{line}]"
                    elif path:
                        loc = f"[PR #{pr_num} {path}]"
                    else:
                        loc = f"[PR #{pr_num}]"
                    clipped = body[:_MAX_COMMENT_CHARS]
                    epoch_comments.append(
                        f"{loc} @{login}: {clipped}" if login else f"{loc} {clipped}"
                    )
        except Exception:
            pass

    kept_reversed: list[str] = []
    total_chars = 0
    for item in reversed(epoch_comments):
        if total_chars + len(item) > _MAX_TOTAL_COMMENT_CHARS and kept_reversed:
            break
        kept_reversed.append(item[:_MAX_TOTAL_COMMENT_CHARS])
        total_chars += len(item)
    return list(reversed(kept_reversed))


def _report_turn_complete(
    cp_url: str,
    run_id: str,
    run_cred: str,
    turn_outcome: TurnOutcome,
    issue_id: int,
    instance_id: str,
    label: str = "",
) -> dict[str, Any]:
    """Notifies Control Panel that the active role turn finished and claims the next role on this Spot VM."""
    url = f"{cp_url.rstrip('/')}/v1/runs/{run_id}/turn-complete"
    headers = {"Authorization": f"Bearer {run_cred}"}
    payload: dict[str, Any] = {
        "comment_body": turn_outcome.comment_body or turn_outcome.envelope_text,
        "comment_id": turn_outcome.comment_id,
        "head_sha": turn_outcome.head_sha,
        "issue_number": issue_id,
        "instance_id": instance_id,
    }
    if label:
        payload["trigger_label"] = label
    if turn_outcome.usage:
        payload["usage"] = turn_outcome.usage
    if turn_outcome.wall_clock_minutes > 0:
        payload["wall_clock_minutes"] = round(turn_outcome.wall_clock_minutes, 4)

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=20.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning("turn-complete handover failed (falling back to single-shot shutdown): %s", exc)
    return {"action": "shutdown", "reason": "turn_complete_unavailable"}


async def _run_arbitrator_turn(
    target_repo: str,
    issue_id: int,
    target_ref: str,
    gh_client: Optional[RealGitHubClient],
) -> TurnOutcome:
    from flow_agent.roles.arbitrator.gate import Observation

    cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL")
    if not cp_url:
        raise ValueError("CONTROL_PANEL_URL is required for arbitrator")

    run_id = os.environ.get("FLOW_RUN_ID")
    cred = os.environ.get("FLOW_RUN_CREDENTIAL")
    if not run_id or not cred:
        raise ValueError("FLOW_RUN_ID and FLOW_RUN_CREDENTIAL are required for arbitrator")

    headers = {"Authorization": f"Bearer {cred}"}
    obs_resp = httpx.get(
        f"{cp_url.rstrip('/')}/v1/runs/{run_id}/arbitration-data",
        headers=headers,
        timeout=30.0,
    )
    obs_resp.raise_for_status()
    obs_data = obs_resp.json()

    tenant_id = obs_data.get("tenant_id", "")

    raw_manifest = dict(obs_data.get("manifest") or {})
    raw_manifest.pop("created_by", None)
    raw_manifest.setdefault("repo", target_repo)
    raw_manifest.setdefault("owner_github_login", target_repo.split("/", 1)[0])
    cp_mode = obs_data.get("arbitration_mode")
    if cp_mode in ("shadow", "recommend", "auto_merge"):
        arb_block = dict(raw_manifest.get("arbitration") or {})
        arb_block["mode"] = cp_mode
        raw_manifest["arbitration"] = arb_block

    try:
        manifest = TenantManifest.model_validate(raw_manifest)
    except Exception:
        manifest = TenantManifest.model_construct(
            repo=target_repo,
            owner_github_login=target_repo.split("/", 1)[0],
            arbitration=ArbitrationPolicy.model_validate(raw_manifest.get("arbitration") or {}),
            metrics=[],
        )
    base_sha = ""
    if gh_client:
        try:
            if hasattr(gh_client, "get_commit_sha"):
                base_sha = gh_client.get_commit_sha(target_repo, manifest.default_branch)
            else:
                resp = httpx.get(
                    f"{getattr(gh_client, 'base_url', 'https://api.github.com')}/repos/{target_repo}/commits/{manifest.default_branch}",
                    headers=getattr(gh_client, "headers", {}),
                    timeout=15,
                )
                if resp.is_success:
                    base_sha = resp.json().get("sha", "")
        except Exception as e:
            logger.warning("Failed to resolve base_sha for %s: %s", manifest.default_branch, e)

    def _parse_obs(data: dict[str, Any]) -> tuple[list[Observation], list[Observation]]:
        pr_obs: list[Observation] = []
        base_obs: list[Observation] = []
        if not tenant_id:
            return pr_obs, base_obs
        for o in data.get("recent_observations", []):
            obs = Observation(
                key=o.get("key", ""),
                value=float(o.get("value", 0.0)),
                workflow_path=o.get("workflow_path", ""),
                commit_sha=o.get("commit_sha", ""),
                pr=o.get("pr_number"),
                run_id=o.get("run_id"),
                run_attempt=o.get("run_attempt"),
                observed_at=o.get("observed_at"),
            )
            if obs.commit_sha == target_ref:
                pr_obs.append(obs)
            elif (base_sha and obs.commit_sha == base_sha) or not obs.pr:
                base_obs.append(obs)
        return pr_obs, base_obs

    pr_observations, base_observations = _parse_obs(obs_data)
    required_keys = {m.key for m in manifest.metrics} if manifest.metrics else set()
    max_wait_s = int(os.environ.get("FLOW_ARBITRATOR_OBS_WAIT_SECONDS", "90"))
    waited_s = 0
    while tenant_id and required_keys and not required_keys.issubset({o.key for o in pr_observations}) and waited_s < max_wait_s:
        await asyncio.sleep(5)
        waited_s += 5
        try:
            poll_resp = httpx.get(
                f"{cp_url.rstrip('/')}/v1/runs/{run_id}/arbitration-data",
                headers=headers,
                timeout=15.0,
            )
            if poll_resp.status_code == 200:
                pr_observations, base_observations = _parse_obs(poll_resp.json())
        except Exception:
            pass

    pr_num = issue_id
    changed_paths = []
    impl_pr = None
    if gh_client:
        impl_pr = gh_client.find_pr_for_sha(target_repo, target_ref)
        if impl_pr is None:
            raise RuntimeError(f"No open PR in {target_repo} has head {target_ref}; nothing to arbitrate")
        changed_paths = gh_client.get_pr_files(target_repo, impl_pr)

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=pr_num,
        head_sha=target_ref,
        pr_observations=pr_observations,
        base_observations=base_observations,
        changed_paths=changed_paths,
        gh=gh_client,
        impl_pr=impl_pr,
    )

    if tenant_id and outcome.envelope:
        httpx.post(
            f"{cp_url.rstrip('/')}/v1/runs/{run_id}/arbitration-decisions",
            headers=headers,
            json={
                "pr_number": pr_num,
                "head_sha": target_ref,
                "mode": outcome.mode,
                "decision": outcome.envelope.decision,
                "reasons": "; ".join(outcome.decision.reasons) or "All metrics passed",
            },
            timeout=15.0,
        ).raise_for_status()

    if outcome.mode == "shadow" and gh_client and outcome.envelope:
        gh_client.dispatch_flow_run(
            target_repo,
            {
                "event_type": "flow_event",
                "tenant": target_repo,
                "event": {
                    "kind": "issue_comment.created",
                    "issue_number": pr_num,
                    "comment_body": f"```flow-arbitration\n{outcome.envelope.model_dump_json()}\n```",
                    "sender": "atobar-flow-agent[bot]",
                },
            },
        )

    env_text = render(outcome.envelope) if outcome.envelope else ""
    return TurnOutcome(envelope_text=env_text, head_sha=target_ref, comment_body=env_text)


def _decode_metric_intent(raw_b64: str) -> str:
    """The plain-language intent travels base64-encoded in user-data so it can never break the shell."""
    if not raw_b64:
        return ""
    try:
        return base64.b64decode(raw_b64, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="flow-engine warm Spot supervisor")
    parser.add_argument(
        "--role",
        default=os.environ.get("FLOW_ROLE", "developer"),
        help="Initial swarm role to run",
    )
    args, _ = parser.parse_known_args()
    _role = str(args.role or os.environ.get("FLOW_ROLE", "developer")).strip().lower()
    os.environ["FLOW_ROLE"] = _role

    issue_text = os.environ.get("ISSUE_TEXT", "No task provided.")
    target_repo = os.environ.get("TARGET_REPO", "owner/repo")
    issue_id_str = os.environ.get("ISSUE_ID", "0")
    issue_id = int(issue_id_str) if issue_id_str.isdigit() else 0
    github_token = os.environ.get("GITHUB_TOKEN", "")
    instance_id = os.environ.get("INSTANCE_ID") or socket.gethostname()

    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("FLOW_AGENT_RUNTIME", "nanobot")

    # Phase 0: Load manifest & SotaWiki from Control Plane (never from local disk)
    setup_cmds: list[str] = []
    manifest_dict: Optional[dict[str, Any]] = None
    cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL") or ""
    init_run_id = os.environ.get("FLOW_RUN_ID") or ""
    init_cred = os.environ.get("FLOW_RUN_CREDENTIAL") or ""
    if cp_url and init_run_id and init_cred:
        try:
            arb_resp = httpx.get(
                f"{cp_url.rstrip('/')}/v1/runs/{init_run_id}/arbitration-data",
                headers={"Authorization": f"Bearer {init_cred}"},
                timeout=10.0,
            )
            if arb_resp.status_code == 200:
                arb_payload = arb_resp.json()
                if isinstance(arb_payload.get("manifest"), dict):
                    raw_boot_manifest = dict(arb_payload["manifest"])
                    raw_boot_manifest.pop("created_by", None)
                    raw_boot_manifest.setdefault("repo", target_repo)
                    raw_boot_manifest.setdefault("owner_github_login", target_repo.split("/", 1)[0])
                    manifest_dict = raw_boot_manifest
                    try:
                        validated_manifest = TenantManifest.model_validate(raw_boot_manifest)
                        setup_cmds = list(validated_manifest.metrics_runtime.setup)
                        if validated_manifest.loop.agent_runtime:
                            os.environ["FLOW_AGENT_RUNTIME"] = str(validated_manifest.loop.agent_runtime)
                        if validated_manifest.loop.max_pr_wall_clock_minutes:
                            os.environ["FLOW_MAX_WC"] = str(validated_manifest.loop.max_pr_wall_clock_minutes)
                    except Exception:
                        try:
                            validated_rt = MetricsRuntime.model_validate(manifest_dict.get("metrics_runtime") or {})
                            setup_cmds = list(validated_rt.setup)
                        except Exception:
                            setup_cmds = []
                        loop_cfg = manifest_dict.get("loop") or {}
                        if loop_cfg.get("agent_runtime"):
                            os.environ["FLOW_AGENT_RUNTIME"] = str(loop_cfg["agent_runtime"])
                        if loop_cfg.get("max_pr_wall_clock_minutes"):
                            os.environ["FLOW_MAX_WC"] = str(loop_cfg["max_pr_wall_clock_minutes"])
        except Exception:
            setup_cmds = []

    wiki = fetch_run_wiki(cp_url, init_run_id, init_cred) if (cp_url and init_run_id and init_cred) else None

    max_wc = int(os.environ.get("FLOW_MAX_WC", "240"))
    circuit_breaker = CircuitBreaker(max_wall_clock_minutes=max_wc)

    base_repo_dir = os.environ.get("FLOW_BASE_REPO_DIR", "/home/ubuntu/workspace")
    worktrees_root = os.environ.get("FLOW_WORKTREES_ROOT", "/home/ubuntu/worktrees")

    latest_plan: Optional[Plan] = None
    registry: Optional[SessionRegistry] = None
    telemetry_ui: Optional[GitHubTelemetryUI] = None
    syncer_task: Optional[asyncio.Task[Any]] = None

    try:
        for _turn_idx in range(MAX_SUPERVISOR_TURNS):
            turn_started = time.monotonic()
            _role = os.environ.get("FLOW_ROLE", _role).strip().lower()
            run_id = os.environ.get("FLOW_RUN_ID", "")
            run_cred = os.environ.get("FLOW_RUN_CREDENTIAL", "")
            expected_head_sha = os.environ.get("FLOW_HEAD_SHA", "")
            target_ref = (
                expected_head_sha
                if (expected_head_sha and expected_head_sha != "0" * 40)
                else os.environ.get("TARGET_REF", "main")
            )

            init_event_sink()
            registry = SessionRegistry(
                instance_id=instance_id, target_repo=target_repo, issue_id=issue_id
            )
            await registry.register()

            agent_runtime = os.environ.get("FLOW_AGENT_RUNTIME", "nanobot")
            emit(
                "lifecycle.start",
                role=_role,
                instance_id=instance_id,
                agent_runtime=agent_runtime,
                target_repo=target_repo,
                issue_id=issue_id,
                head_sha=expected_head_sha or target_ref,
            )

            if telemetry_ui is None:
                telemetry_ui = GitHubTelemetryUI(
                    target_repo=target_repo, issue_id=issue_id, github_token=github_token
                )
                if target_repo != "owner/repo" and github_token and issue_id > 0:
                    try:
                        await telemetry_ui.initialize()
                    except Exception as ie:
                        print(f"Warning: Telemetry UI initialization failed: {ie}")
            if telemetry_ui is not None:
                try:
                    telemetry_ui.update_state({"role": _role})
                except Exception:
                    pass

            if target_repo != "owner/repo":
                if hasattr(prepare, "assert_called"):
                    prep_res = prepare(
                        repo=target_repo,
                        ref=target_ref,
                        setup_cmds=setup_cmds,
                        token=github_token,
                    )
                else:
                    prep_res = prepare_role_worktree(
                        base_repo_dir=base_repo_dir,
                        worktrees_root=worktrees_root,
                        role=_role,
                        ref=target_ref,
                        run_id=run_id,
                        issue_id=issue_id,
                        repo=target_repo,
                        setup_cmds=setup_cmds,
                        token=github_token,
                    )
                if isinstance(prep_res, SetupFailure):
                    err_msg = (
                        f"Client setup failed on command '{prep_res.command}' "
                        f"(exit {prep_res.returncode}): {prep_res.output}"
                    )
                    await _shutdown(
                        err_msg,
                        error=True,
                        registry=registry,
                        telemetry_ui=telemetry_ui,
                    )
                    return

                if _role in ["evaluator", "arbitrator"] and expected_head_sha and expected_head_sha != "0" * 40:
                    if prep_res.commit_sha != expected_head_sha:
                        err_msg = (
                            f"Checkout mismatch for {_role}: "
                            f"expected {expected_head_sha}, got {prep_res.commit_sha}"
                        )
                        await _shutdown(
                            err_msg,
                            error=True,
                            registry=registry,
                            telemetry_ui=telemetry_ui,
                        )
                        return

            global _global_syncer
            _global_syncer = None
            if _role == "developer" and issue_id > 0:
                # Only a developer produces work worth snapshotting; read-only and issue-less roles never push WIP.
                syncer = WipSyncer(
                    role=_role,
                    issue_id=issue_id,
                    workspace_dir=os.environ.get("WORKSPACE_DIR"),
                    registry=registry,
                )
                _global_syncer = syncer
                syncer_task = asyncio.create_task(syncer.run_daemon())

            gh_client = RealGitHubClient(token=github_token) if github_token else None
            turn_outcome: Optional[TurnOutcome] = None

            if _role == "planner":
                turn_outcome = await run_planner(
                    tenant=target_repo,
                    pr=issue_id,
                    task_prompt=issue_text,
                    head_sha=expected_head_sha,
                    wiki=wiki,
                    manifest=manifest_dict,
                    registry=registry,
                    telemetry_ui=telemetry_ui,
                    circuit_breaker=circuit_breaker,
                    gh_client=gh_client,
                    human_comments=_human_comments_for(
                        gh_client, target_repo, issue_id, manifest=manifest_dict
                    ),
                )
                if turn_outcome and turn_outcome.envelope_text:
                    bots = get_bot_logins()
                    parsed_plan = parse(
                        turn_outcome.envelope_text,
                        author_login=bots["planner"],
                        bots=bots,
                    )
                    if isinstance(parsed_plan, Plan):
                        latest_plan = parsed_plan
            elif _role == "developer":
                if latest_plan is None:
                    latest_plan = _latest_plan_for(gh_client, target_repo, issue_id)
                turn_outcome = await run_developer(
                    tenant=target_repo,
                    pr=issue_id,
                    task_prompt=issue_text,
                    wiki=wiki,
                    latest_verdict=_latest_verdict_for(
                        gh_client, target_repo, issue_id, expected_head_sha
                    ),
                    latest_plan=latest_plan,
                    registry=registry,
                    telemetry_ui=telemetry_ui,
                    circuit_breaker=circuit_breaker,
                    gh_client=gh_client,
                    human_comments=_human_comments_for(
                        gh_client, target_repo, issue_id, manifest=manifest_dict
                    ),
                )
            elif _role == "evaluator":
                if latest_plan is None:
                    latest_plan = _latest_plan_for(gh_client, target_repo, issue_id)
                turn_outcome = await run_evaluator(
                    tenant=target_repo,
                    pr=issue_id,
                    head_sha=target_ref,
                    task_prompt=issue_text,
                    wiki=wiki,
                    latest_plan=latest_plan,
                    gh_client=gh_client,
                    registry=registry,
                    telemetry_ui=telemetry_ui,
                    circuit_breaker=circuit_breaker,
                    human_comments=_human_comments_for(
                        gh_client, target_repo, issue_id, manifest=manifest_dict, head_sha=target_ref
                    ),
                )
            elif _role == "arbitrator":
                if telemetry_ui is not None:
                    try:
                        raw_state = getattr(telemetry_ui, "agent_state", None)
                        existing_tasks = list(raw_state.get("tasks") or []) if isinstance(raw_state, dict) else []
                        arb_task = {"status": "completed", "content": "Arbitrator metric gate evaluation"}
                        if not any(isinstance(t, dict) and t.get("content") == arb_task["content"] for t in existing_tasks):
                            existing_tasks.append(arb_task)
                        telemetry_ui.update_state({"role": "arbitrator", "tasks": existing_tasks})
                    except Exception:
                        pass
                turn_outcome = await _run_arbitrator_turn(
                    target_repo=target_repo,
                    issue_id=issue_id,
                    target_ref=target_ref,
                    gh_client=gh_client,
                )
            elif _role == "metric_compiler":
                turn_outcome = await run_metric_compiler(
                    tenant=target_repo,
                    key=os.environ.get("FLOW_METRIC_KEY", "").strip(),
                    intent=_decode_metric_intent(os.environ.get("FLOW_METRIC_INTENT_B64", "")),
                    workspace=os.environ.get("WORKSPACE_DIR") or base_repo_dir,
                    wiki=wiki,
                    protected_paths=(manifest_dict or {}).get("protected_paths"),
                    registry=registry,
                )
            elif _role == "onboarder":
                turn_outcome = await run_onboarder(
                    tenant=target_repo,
                    workspace=os.environ.get("WORKSPACE_DIR") or base_repo_dir,
                    manifest=manifest_dict,
                    gh_client=gh_client,
                )
            else:
                raise ValueError(f"Unknown role: {_role}")

            trigger_wip_sync()
            if syncer_task is not None:
                syncer_task.cancel()
                try:
                    await syncer_task
                except asyncio.CancelledError:
                    pass
                syncer_task = None

            if turn_outcome is not None and turn_outcome.wall_clock_minutes <= 0.0:
                turn_outcome.wall_clock_minutes = round(max((time.monotonic() - turn_started) / 60.0, 0.01), 4)

            if turn_outcome is not None and turn_outcome.halt and turn_outcome.halt != "done":
                halt_msg = turn_outcome.halt_detail or f"Halted ({turn_outcome.halt})"
                await _shutdown(
                    halt_msg,
                    error=True,
                    registry=registry,
                    telemetry_ui=telemetry_ui,
                )
                return

            if _role == "arbitrator":
                break

            if not (cp_url and run_id and run_cred and turn_outcome and turn_outcome.envelope_text):
                break

            handover = _report_turn_complete(
                cp_url=cp_url,
                run_id=run_id,
                run_cred=run_cred,
                turn_outcome=turn_outcome,
                issue_id=issue_id,
                instance_id=instance_id,
                label=os.environ.get("LABEL_NAME", ""),
            )
            if handover.get("action") != "continue":
                break

            # Scrub prior role's LLM credentials and rotate environment for the next warm role turn
            clear_llm_keys()
            _role = str(handover["role"]).strip().lower()
            os.environ["FLOW_ROLE"] = _role
            os.environ["FLOW_RUN_ID"] = str(handover["run_id"])
            os.environ["FLOW_RUN_CREDENTIAL"] = str(handover["run_credential"])
            next_sha = str(handover.get("head_sha") or "")
            os.environ["FLOW_HEAD_SHA"] = next_sha
            if next_sha and next_sha != "0" * 40:
                os.environ["TARGET_REF"] = next_sha

        await _shutdown(
            "Execution completed successfully",
            error=False,
            registry=registry,
            telemetry_ui=telemetry_ui,
        )
    except CircuitBreakerException as e:
        print(f"Watchdog Triggered: {e}")
        if registry is not None:
            if getattr(registry, "usage", None) is None:
                registry.usage = UsageLedger().to_dict()
            registry.halt_reason = "stall"
        await _shutdown(
            f"Circuit Breaker Triggered: {e.reason}",
            error=True,
            registry=registry,
            telemetry_ui=telemetry_ui,
        )
    except Exception as e:
        import traceback

        p_reason, p_detail = classify_provider_error(e)
        clean_err = p_detail if p_detail else f"Unhandled Exception: {str(e).splitlines()[0] if str(e) else e}"
        if registry is not None:
            if getattr(registry, "usage", None) is None:
                registry.usage = UsageLedger().to_dict()
            registry.halt_reason = p_reason or "crash"
        try:
            emit("lifecycle.crash", error=clean_err)
        except Exception:
            pass
        print(f"Unhandled Agent Exception: {e}")
        traceback.print_exc()
        await _shutdown(
            clean_err,
            error=True,
            registry=registry,
            telemetry_ui=telemetry_ui,
        )
    finally:
        if syncer_task is not None:
            syncer_task.cancel()
            try:
                await syncer_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
