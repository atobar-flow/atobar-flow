"""Deterministic Conductor event processing over Store and GitHubClient protocols."""

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import httpx

from atobar_contracts.arbitration import Observation, run_arbitrator
from atobar_contracts.constants import LOOP_EPOCH_MARKER, get_bot_logins
from atobar_contracts.envelopes import (
    MODEL_BY_ROLE,
    Ack,
    Arbitration,
    BaseEnvelope,
    Plan,
    Verdict,
    compute_event_id,
    parse,
)
from atobar_contracts.github_rest import GitHubClient
from atobar_contracts.issue_states import STATUS_MARKER, render_status_card
from atobar_contracts.machine import (
    AckEvent,
    Action,
    ArbitrationEvent,
    CheckCompletedEvent,
    CommentAction,
    DispatchAction,
    EscalateAction,
    Event,
    HumanCommentEvent,
    HumanLabelEvent,
    LoopState,
    PlanEvent,
    PushEvent,
    RemoveLabelAction,
    SetLabelAction,
    VerdictEvent,
    step,
)
from atobar_contracts.manifest import ArbitrationPolicy, LoopLimits, TenantManifest
from atobar_contracts.run_credentials import generate_run_credentials
from atobar_contracts.store import Store

__all__ = [
    "WARM_HANDOVER_FORBIDDEN_ROLES",
    "compute_event_id",
    "is_epoch_marker",
    "linked_prs_for_issue",
    "rebuild_loop_state",
    "apply_actions",
    "process_flow_event",
    "is_issue_awaiting_plan_approval",
    "resolve_delegated_issue_number",
    "parse_human_comment_directive",
    "handle_merged_pr_event",
    "upsert_issue_comment_by_marker",
    "secrets",
]

logger = logging.getLogger(__name__)

WARM_HANDOVER_FORBIDDEN_ROLES: frozenset[str] = frozenset({"evaluator", "arbitrator"})
LEGACY_UNCLAIMED_CREDENTIAL_CUTOFF = datetime(2026, 10, 22, 0, 0, 0, tzinfo=timezone.utc)
CREDENTIAL_EXPIRY_GRACE = timedelta(minutes=30)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


_EPOCH_ID_RE = re.compile(r"<!--\s*flow:epoch-id:([0-9a-zA-Z_-]+)\s*-->")


def is_epoch_marker(
    comment: dict[str, Any],
    bot_logins: dict[str, str],
    *,
    store: Optional[Store] = None,
    tenant_id: str = "",
) -> bool:
    """True only when `comment` was authored by a configured bot login, starts with LOOP_EPOCH_MARKER,
    and (when `store` is provided) carries a one-time server-minted `role="epoch"` credential id."""
    author = str((comment.get("user") or {}).get("login") or "").strip().lower()
    if not author:
        return False
    allowed = {str(v).strip().lower() for v in (bot_logins or {}).values() if v}
    if author not in allowed:
        return False
    body = str(comment.get("body") or "").lstrip("\ufeff \t\r\n")
    if not body.startswith(LOOP_EPOCH_MARKER) or "```flow-" in body:
        return False
    first_line = body.splitlines()[0] if body else ""
    m = _EPOCH_ID_RE.search(first_line)
    if store is None:
        return True
    if not m:
        return False
    epoch_id = m.group(1)
    rec = store.get_run_credential(epoch_id)
    if not rec or rec.get("role") != "epoch":
        return False
    if tenant_id and rec.get("tenant_id") != tenant_id:
        return False
    cid = comment.get("id")
    if cid is not None and not store.claim_run_credential_envelope(epoch_id, int(cid)):
        return False
    return True


def _fetch_comment_by_id(
    gh: GitHubClient,
    repo: str,
    pr_number: int,
    comment_id: int,
    *,
    source_pr_number: Optional[int] = None,
    kind: str = "issue_comment.created",
) -> Optional[dict[str, Any]]:
    candidates: list[int] = []
    if source_pr_number is not None and int(source_pr_number) > 0:
        candidates.append(int(source_pr_number))
    if int(pr_number) > 0 and int(pr_number) not in candidates:
        candidates.append(int(pr_number))

    if kind == "pull_request_review.submitted":
        for num in candidates:
            try:
                review = gh.get_pr_review(repo, num, comment_id)
                if isinstance(review, dict) and str(review.get("id")) == str(comment_id):
                    return review
            except Exception:
                pass
        return None

    if kind == "pull_request_review_comment.created":
        for num in candidates:
            try:
                rev_comment = gh.get_pr_review_comment(repo, num, comment_id)
                if isinstance(rev_comment, dict) and str(rev_comment.get("id")) == str(comment_id):
                    return rev_comment
            except Exception:
                pass
        return None

    for num in candidates:
        try:
            direct = gh.get_comment(repo, num, comment_id)
            if isinstance(direct, dict) and str(direct.get("id")) == str(comment_id):
                issue_url = str(direct.get("issue_url") or "").rstrip("/").lower()
                expected_suffixes = {
                    f"/repos/{repo.lower()}/issues/{int(c)}"
                    for c in candidates
                } | {
                    f"/repos/{repo.lower()}/pulls/{int(c)}"
                    for c in candidates
                }
                if not issue_url or any(issue_url.endswith(sfx) for sfx in expected_suffixes):
                    return direct
                return None
        except Exception:
            pass
        try:
            all_comments = gh.get_comments(repo, num) or []
            if isinstance(all_comments, list):
                found = next(
                    (c for c in all_comments if isinstance(c, dict) and c.get("id") is not None and str(c.get("id")) == str(comment_id)),
                    None,
                )
                if found is not None:
                    return found
        except Exception:
            pass
    return None


def _check_run_sort_key(idx: int, cr: dict[str, Any]) -> tuple[datetime, int, int]:
    ts = _parse_ts(cr.get("completed_at") or cr.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
    try:
        cr_id = int(cr["id"]) if cr.get("id") is not None else 0
    except (TypeError, ValueError):
        cr_id = 0
    return (ts, cr_id, idx)


def _authoritative_check_run(
    check_runs: list[dict[str, Any]],
    check_name: str,
    check_run_id: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    named = [(idx, cr) for idx, cr in enumerate(check_runs) if cr.get("name") == check_name]
    if not named:
        return None
    _, latest = max(named, key=lambda item: _check_run_sort_key(item[0], item[1]))
    if check_run_id is not None:
        if latest.get("id") is None or str(latest.get("id")) != str(check_run_id):
            return None
    return latest


def rebuild_loop_state(
    tenant_repo: str,
    pr_number: int,
    gh: GitHubClient,
    store: Store,
    bot_logins: dict[str, str],
    manifest: Optional[dict[str, Any]] = None,
    *,
    exclude_comment_id: Optional[int] = None,
) -> LoopState:
    """Rebuilds LoopState by reading comments, checks, and recorded loop rounds."""
    pr_info = {}
    pr_head_sha = ""
    try:
        pr_info = gh.get_pr(tenant_repo, pr_number)
        pr_head_sha = pr_info.get("head", {}).get("sha", "")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    head_sha = pr_head_sha

    tenant = store.tenant_by_repo(tenant_repo)
    tenant_id = tenant.id if tenant else ""
    rounds = store.loop_rounds_for_pr(tenant_id, pr_number) if tenant_id else []
    comments = gh.get_comments(tenant_repo, pr_number)
    if exclude_comment_id is not None:
        comments = [
            c
            for c in comments
            if c.get("id") is None or str(c.get("id")) != str(exclude_comment_id)
        ]

    epoch_marks = [
        ts
        for c in comments
        if is_epoch_marker(c, bot_logins, store=store, tenant_id=tenant_id)
        and (ts := _parse_ts(c.get("created_at"))) is not None
    ]
    epoch_start = max(epoch_marks, default=None)
    if epoch_start is not None:
        comments = [c for c in comments if (_parse_ts(c.get("created_at")) or epoch_start) >= epoch_start]
        rounds = [r for r in rounds if (_parse_ts(r.get("created_at")) or epoch_start) >= epoch_start]

    accumulated_cost = sum(float(r.get("cost_usd", 0.0)) for r in rounds)
    accumulated_wall_clock = sum(float(r.get("wall_clock_minutes", 0.0)) for r in rounds)
    current_round = max((int(r.get("round", 0)) for r in rounds), default=0)

    latest_plan: Optional[Plan] = None
    latest_ack: Optional[Ack] = None
    latest_verdict: Optional[Verdict] = None
    latest_arbitration: Optional[Arbitration] = None
    head_sha_history: list[str] = []
    findings_history: list[list[str]] = []
    seen_legacy_run_ids: set[str] = set()

    def _apply_envelope(env_obj: BaseEnvelope) -> None:
        nonlocal latest_plan, latest_ack, latest_verdict, latest_arbitration, head_sha
        if isinstance(env_obj, Plan):
            latest_plan = env_obj
        elif isinstance(env_obj, Ack):
            latest_ack = env_obj
            if not pr_head_sha and env_obj.head_sha and env_obj.head_sha != "0" * 40:
                head_sha = env_obj.head_sha
        elif isinstance(env_obj, Verdict):
            latest_verdict = env_obj
            if env_obj.head_sha not in head_sha_history:
                head_sha_history.append(env_obj.head_sha)
                findings_history.append([f.fingerprint for f in env_obj.findings if f.fingerprint])
        elif isinstance(env_obj, Arbitration):
            latest_arbitration = env_obj

    # loop_rounds rows are persisted by process_flow_event only after run_credentials
    # (tenant/role/expiry), head_sha, and envelope_comment_id provenance checks succeed.
    # Treat DB envelopes as authoritative and fall back to GitHub comment parsing only
    # when older rounds lack usage_detail.envelope (ignoring halted/reaped tombstone rows).
    db_envelopes: list[BaseEnvelope] = []
    db_run_ids: set[str] = set()
    has_legacy_round = False
    for r in rounds:
        ud = r.get("usage_detail")
        env_raw = ud.get("envelope") if isinstance(ud, dict) else None
        if not isinstance(env_raw, dict):
            digest_str = str(r.get("envelope_digest") or "")
            is_tombstone = digest_str.startswith(("halted:", "reaped:")) or (
                isinstance(ud, dict) and ("halt_reason" in ud or "halt_detail" in ud)
            )
            if not is_tombstone:
                has_legacy_round = True
            continue
        role_key = str(env_raw.get("role") or r.get("role") or "")
        model_cls = MODEL_BY_ROLE.get(role_key)
        if model_cls is None:
            continue
        try:
            db_env = model_cls.model_validate(env_raw)
        except Exception as exc:
            logger.warning(
                "Failed to validate envelope from loop_rounds for %s#%s (role=%s): %s",
                tenant_repo,
                pr_number,
                role_key,
                exc,
            )
            continue
        db_envelopes.append(db_env)
        if db_env.run_id:
            db_run_ids.add(db_env.run_id)
        _apply_envelope(db_env)

    need_comment_scan = has_legacy_round or not db_envelopes
    for c in comments:
        body = c.get("body", "")
        if not need_comment_scan and "```flow-arbitration" not in body:
            continue
        author = c.get("user", {}).get("login", "")
        env = parse(body, author_login=author, bots=bot_logins)
        if not env or (env.run_id and env.run_id in db_run_ids):
            continue

        run_id = env.run_id
        if not run_id:
            logger.warning("Ignoring untrusted envelope from %s missing run_id (role %s)", author, env.role)
            continue
        cred_record = store.get_run_credential(run_id)
        if not cred_record or cred_record.get("tenant_id") != tenant_id or cred_record.get("role") != env.role:
            logger.warning("Ignoring untrusted envelope: unknown or mismatched run_id %s", run_id)
            continue
        cred_sha = str(cred_record.get("head_sha") or "")
        if cred_sha and cred_sha != "0" * 40 and not isinstance(env, Plan):
            env_sha = str(getattr(env, "head_sha", "") or "")
            is_rework_ack = (
                isinstance(env, Ack)
                and cred_sha in head_sha_history
                and (not pr_head_sha or env_sha == pr_head_sha)
            )
            if env_sha != cred_sha and not is_rework_ack:
                logger.warning(
                    "Ignoring untrusted envelope: head_sha mismatch for run_id %s (got %s, expected %s)",
                    run_id,
                    env_sha,
                    cred_sha,
                )
                continue
        claimed_cid = cred_record.get("envelope_comment_id")
        c_id = c.get("id")
        if claimed_cid is not None:
            if c_id is None or str(claimed_cid) != str(c_id):
                logger.warning(
                    "Ignoring untrusted envelope: comment_id %s does not match claimed %s for run_id %s",
                    c_id,
                    claimed_cid,
                    run_id,
                )
                continue
        else:
            created_dt = _parse_ts(cred_record.get("created_at"))
            if (
                created_dt is None
                or created_dt >= LEGACY_UNCLAIMED_CREDENTIAL_CUTOFF
                or run_id in seen_legacy_run_ids
            ):
                logger.warning("Ignoring unclaimed envelope for post-migration run_id %s", run_id)
                continue
            seen_legacy_run_ids.add(run_id)

        _apply_envelope(env)

    if need_comment_scan and db_envelopes:
        for db_env in db_envelopes:
            _apply_envelope(db_env)

    m = manifest or (tenant.manifest if tenant else {})
    required_checks = m.get("required_checks", [])

    required_checks_status: dict[str, str] = {}
    if head_sha:
        check_runs = gh.get_check_runs(tenant_repo, head_sha)
        seen_names = {str(cr.get("name") or "") for cr in check_runs if cr.get("name")}
        for name in seen_names:
            auth_cr = _authoritative_check_run(check_runs, name)
            if auth_cr is not None:
                required_checks_status[name] = str(auth_cr.get("conclusion") or "pending")

    labels = {lbl.get("name") if isinstance(lbl, dict) else str(lbl) for lbl in pr_info.get("labels", [])}
    if not labels and hasattr(gh, "get_issue"):
        try:
            issue_info = gh.get_issue(tenant_repo, pr_number)
            if isinstance(issue_info, dict) and isinstance(issue_info.get("labels"), list):
                labels = {lbl.get("name") if isinstance(lbl, dict) else str(lbl) for lbl in issue_info["labels"]}
        except Exception:
            pass
    status: Literal["idle", "in_plan", "in_dev", "in_eval", "in_arbiter", "converged", "escalated"] = "idle"
    if "flow:escalated" in labels:
        status = "escalated"
    elif latest_arbitration is not None:
        status = "converged"
    elif "flow:in-review" in labels:
        status = "in_eval"
    elif "flow:in-progress" in labels:
        status = "in_dev"
    elif "flow:plan" in labels or "flow:needs-approval" in labels:
        status = "in_plan"

    return LoopState(
        tenant=tenant_repo,
        pr=pr_number,
        head_sha=head_sha,
        round=current_round,
        head_sha_history=head_sha_history,
        findings_history=findings_history,
        accumulated_cost_usd=accumulated_cost,
        accumulated_wall_clock_minutes=accumulated_wall_clock,
        latest_plan=latest_plan,
        latest_ack=latest_ack,
        latest_verdict=latest_verdict,
        latest_arbitration=latest_arbitration,
        required_checks=required_checks,
        required_checks_status=required_checks_status,
        status=status,
    )


def _run_serverless_arbitrator(
    tenant_repo: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    gh: Any,
    store: Store,
    tenant: Any,
) -> bool:
    """Executes the deterministic Arbitrator gate in-process without booting an EC2 Spot VM."""
    manifest_data = dict((tenant.manifest if tenant and isinstance(tenant.manifest, dict) else None) or {})
    manifest_data.pop("created_by", None)
    manifest_data.setdefault("repo", tenant_repo)
    manifest_data.setdefault("owner_github_login", tenant_repo.split("/", 1)[0])
    arb_cfg = dict(manifest_data.get("arbitration") or {})
    if tenant and getattr(tenant, "arbitration_mode", None) in ("recommend", "auto_merge"):
        arb_cfg["mode"] = tenant.arbitration_mode
    elif tenant and getattr(tenant, "arbitration_mode", None) == "shadow":
        arb_cfg.setdefault("mode", "shadow")
    if arb_cfg:
        manifest_data["arbitration"] = arb_cfg

    manifest_obj: Optional[TenantManifest] = None
    invalid_manifest_reason: Optional[str] = None
    try:
        manifest_obj = TenantManifest.model_validate(manifest_data)
    except Exception as exc:
        if manifest_data.get("goal") is not None or manifest_data.get("metrics"):
            invalid_manifest_reason = f"Invalid tenant manifest: {exc}"
        try:
            manifest_obj = TenantManifest.model_construct(
                repo=tenant_repo,
                owner_github_login=tenant_repo.split("/", 1)[0],
                arbitration=ArbitrationPolicy.model_validate(arb_cfg or {}),
                metrics=[],
            )
            if invalid_manifest_reason:
                object.__setattr__(manifest_obj, "_invalid_manifest_reason", invalid_manifest_reason)
        except Exception:
            return False

    tenant_id = tenant.id if tenant else ""
    raw_obs: list[dict[str, Any]] = []
    seen_sigs: set[tuple[Any, ...]] = set()

    def _add_obs(items: list[dict[str, Any]]) -> None:
        for o in items:
            sig = (
                o.get("key"),
                o.get("commit_sha"),
                o.get("pr_number") if o.get("pr_number") is not None else o.get("pr"),
                o.get("run_id") or o.get("check_run_id"),
                o.get("run_attempt"),
            )
            if sig not in seen_sigs:
                raw_obs.append(o)
                seen_sigs.add(sig)

    if tenant_id and hasattr(store, "observations_for_pr_or_sha"):
        try:
            _add_obs(store.observations_for_pr_or_sha(tenant_id, pr_number=pr_number, commit_sha=head_sha))
        except Exception:
            pass
    if tenant_id and hasattr(store, "recent_observations"):
        try:
            _add_obs(store.recent_observations(tenant_id))
        except Exception:
            pass
    if tenant_id and hasattr(store, "default_branch_observations"):
        try:
            _add_obs(store.default_branch_observations(tenant_id))
        except Exception:
            pass

    pr_obs: list[Observation] = []
    base_obs: list[Observation] = []
    for o in raw_obs:
        obs_pr = o.get("pr_number") if o.get("pr_number") is not None else o.get("pr")
        obs = Observation(
            key=str(o.get("key", "")),
            value=float(o.get("value", 0.0)),
            commit_sha=str(o.get("commit_sha", "")),
            pr=int(obs_pr) if obs_pr is not None else None,
            workflow_path=o.get("workflow_path"),
            run_id=o.get("run_id") if o.get("run_id") is not None else o.get("check_run_id"),
            run_attempt=o.get("run_attempt"),
            observed_at=o.get("observed_at"),
            artifact_sha256=o.get("artifact_sha256"),
        )
        if obs.commit_sha == head_sha:
            pr_obs.append(obs)
        elif obs.pr is None:
            base_obs.append(obs)

    changed_paths: list[str] = []
    if hasattr(gh, "get_pr_files"):
        try:
            changed_paths = list(gh.get_pr_files(tenant_repo, pr_number) or [])
        except Exception:
            changed_paths = []

    if not hasattr(gh, "post_pr_comment") and hasattr(gh, "create_comment"):
        setattr(gh, "post_pr_comment", gh.create_comment)

    prev_run_id = os.environ.get("FLOW_RUN_ID")
    os.environ["FLOW_RUN_ID"] = run_id
    try:
        coro = run_arbitrator(
            manifest=manifest_obj,
            pr=pr_number,
            head_sha=head_sha,
            pr_observations=pr_obs,
            base_observations=base_obs,
            changed_paths=changed_paths,
            gh=gh,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                outcome = pool.submit(asyncio.run, coro).result(timeout=15.0)
        else:
            outcome = asyncio.run(coro)
    finally:
        if prev_run_id is None:
            os.environ.pop("FLOW_RUN_ID", None)
        else:
            os.environ["FLOW_RUN_ID"] = prev_run_id

    claimed_cid = getattr(outcome, "comment_id", None)
    if claimed_cid is None:
        try:
            for c in reversed(gh.get_comments(tenant_repo, pr_number) or []):
                if isinstance(c, dict) and c.get("id") is not None and run_id in str(c.get("body") or ""):
                    claimed_cid = int(c["id"])
                    break
        except Exception:
            claimed_cid = None
    if claimed_cid is not None:
        try:
            store.claim_run_credential_envelope(run_id, int(claimed_cid))
        except Exception:
            pass

    if tenant_id:
        from atobar_contracts.manifest import canonical_sha

        decision_row = {
            "tenant_id": tenant_id,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "mode": outcome.mode,
            "decision": outcome.decision.conclusion,
            "reasons": "; ".join(outcome.decision.reasons) if outcome.decision.reasons else "",
            "manifest_sha": canonical_sha(dict(tenant.manifest or {})) if tenant else "",
        }
        try:
            if hasattr(store, "insert_arbitration_decision"):
                store.insert_arbitration_decision(decision_row)
            elif hasattr(store, "record_arbitration_decision"):
                store.record_arbitration_decision(decision_row)
        except Exception:
            pass
    return True


def resolve_human_assignee(manifest: Any, tenant_repo: str) -> str:
    """Single source of truth for who gets pinged: manifest login, then creator, then repo owner."""
    data = manifest if isinstance(manifest, dict) else {}
    login = str(data.get("owner_github_login") or "").strip()
    if login and login != "admin":
        return login
    created_by = str(data.get("created_by") or "").strip()
    if created_by and created_by != "admin":
        return created_by
    return tenant_repo.split("/", 1)[0] if "/" in tenant_repo else ""


def _human_assignee_for_tenant(tenant_repo: str, store: Store, gh: Any = None) -> str:
    """Resolve the human to notify; returns "" (and warns) when GitHub says they cannot be assigned.

    The repo-owner fallback is an organization for org-owned repos, which GitHub rejects with 422.
    """
    tenant = store.tenant_by_repo(tenant_repo) if store is not None else None
    manifest = tenant.manifest if tenant and isinstance(getattr(tenant, "manifest", None), dict) else {}
    login = resolve_human_assignee(manifest, tenant_repo)
    if login and gh is not None and hasattr(gh, "is_valid_assignee"):
        try:
            valid = bool(gh.is_valid_assignee(tenant_repo, login))
        except Exception as exc:
            logger.warning("Could not verify assignee %s on %s: %s", login, tenant_repo, exc)
            return login
        if not valid:
            logger.warning(
                "Human assignee %s is not assignable on %s (org or no access); nobody will be notified. "
                "Set a Human Assignee in the Control Panel.",
                login,
                tenant_repo,
            )
            return ""
    return login


def _loop_caps_for_tenant(tenant_repo: str, store: Store) -> dict[str, Any]:
    tenant = store.tenant_by_repo(tenant_repo) if store is not None else None
    manifest = tenant.manifest if tenant and isinstance(getattr(tenant, "manifest", None), dict) else {}
    raw_loop = manifest.get("loop")
    loop_cfg: dict[str, Any] = dict(raw_loop) if isinstance(raw_loop, dict) else {}
    try:
        policy = LoopLimits(**loop_cfg)
    except Exception:
        policy = LoopLimits()
    return {
        "max_pr_cost_usd": policy.max_pr_cost_usd,
        "max_review_rounds": policy.max_review_rounds,
    }


def upsert_issue_comment_by_marker(
    gh: Any,
    repo: str,
    issue_number: int,
    marker: str,
    body: str,
) -> None:
    """Updates an existing comment containing `marker` in-place, or creates a new comment."""
    if gh is None or issue_number <= 0:
        return
    if hasattr(gh, "get_comments") and hasattr(gh, "update_comment"):
        try:
            for c in reversed(gh.get_comments(repo, issue_number) or []):
                if isinstance(c, dict) and marker in str(c.get("body") or "") and isinstance(c.get("id"), int):
                    gh.update_comment(repo, int(c["id"]), body)
                    return
        except Exception as exc:
            logger.warning("Failed to inspect existing comments for marker %s on %s#%s: %s", marker, repo, issue_number, exc)
    if hasattr(gh, "create_comment"):
        gh.create_comment(repo, issue_number, body)
    elif hasattr(gh, "create_issue_comment"):
        gh.create_issue_comment(repo, issue_number, body)


def _issue_labels_from_sources(
    repo: str,
    issue_num: int,
    inner_event: Optional[dict[str, Any]],
    gh: Any,
) -> set[str]:
    out: set[str] = set()
    if isinstance(inner_event, dict):
        raw_labels = inner_event.get("issue_labels") or inner_event.get("labels") or []
        if isinstance(raw_labels, list):
            for lbl in raw_labels:
                name = lbl.get("name") if isinstance(lbl, dict) else str(lbl)
                if name:
                    out.add(str(name))
    if gh is not None:
        disp_lbls_map = getattr(gh, "labels", None)
        if isinstance(disp_lbls_map, dict):
            out |= {str(x) for x in disp_lbls_map.get((repo, issue_num), set())}
        for getter_name in ("get_issue", "get_pr"):
            if hasattr(gh, getter_name):
                try:
                    info = getattr(gh, getter_name)(repo, issue_num) or {}
                    if isinstance(info, dict):
                        for lbl in info.get("labels") or []:
                            name = lbl.get("name") if isinstance(lbl, dict) else str(lbl)
                            if name:
                                out.add(str(name))
                        if out:
                            break
                except Exception as exc:
                    logger.warning("Could not read labels for %s#%s: %s", repo, issue_num, exc)
    return out


def is_issue_awaiting_plan_approval(
    repo: str,
    issue_num: int,
    inner_event: Optional[dict[str, Any]],
    gh: Any,
    store: Any,
) -> bool:
    """Single source of truth for whether `flow:approved` should dispatch a developer on `issue_num`."""
    if issue_num <= 0:
        return False
    labels = _issue_labels_from_sources(repo, issue_num, inner_event, gh)
    if "flow:onboard" in labels or "flow:metrics" in labels:
        return False
    if labels & {"flow:in-progress", "flow:in-review", "flow:escalated", "flow:shipped"}:
        return False
    if "flow:needs-approval" in labels:
        return True

    comments: list[dict[str, Any]] = []
    if gh is not None and hasattr(gh, "get_comments"):
        try:
            comments = list(gh.get_comments(repo, issue_num) or [])
        except Exception:
            comments = []
    turn_comments = getattr(store, "_turn_comments", None)
    if isinstance(turn_comments, dict):
        seen_ids = {c.get("id") for c in comments if c.get("id") is not None}
        for c in turn_comments.get((repo, issue_num), []):
            if c.get("id") is None or c.get("id") not in seen_ids:
                comments.append(c)

    if not comments:
        return False
    tenant = store.tenant_by_repo(repo) if store is not None else None
    tenant_id = tenant.id if tenant else ""
    bots = get_bot_logins()
    latest_env_in_epoch: Any = None
    for c in comments:
        body = str(c.get("body") or "")
        if is_epoch_marker(c, bots, store=store, tenant_id=tenant_id):
            latest_env_in_epoch = None
            continue
        env = parse(
            body,
            author_login=str((c.get("user") or {}).get("login") or "atobar-flow-agent[bot]"),
            bots=bots,
        )
        if env is not None:
            latest_env_in_epoch = env
    return isinstance(latest_env_in_epoch, Plan)


_FLOW_BRANCH_ISSUE_RE = re.compile(r"^flow[-/](?:issue[-/])?(\d+)(?:\b|[-_/])")
_FIXES_ISSUE_RE = re.compile(r"(?:Fixes|Closes|Resolves)\s+#(\d+)\b", re.IGNORECASE)
_FLOW_DIRECTIVE_RE = re.compile(r"@atobar-flow-agent(?:\[bot\])?(?!-)\b", re.IGNORECASE)
_ISSUE_APPROVE_INTENT_RE = re.compile(r"^approve(?:d)?\s*[.!]?$", re.IGNORECASE)
_PR_EVALUATE_INTENT_RE = re.compile(r"^eval(?:uate)?(?:\b|$)", re.IGNORECASE)
_QUEUED_DIRECTIVE_RE = re.compile(
    r"<!--\s*flow:queued-directive:(\d+):(issue|pr):(plan|approve|correct|evaluate)\s*-->"
)


def _issue_from_pr_metadata(head_ref: str, pr_body: str) -> Optional[int]:
    if head_ref:
        m_branch = _FLOW_BRANCH_ISSUE_RE.match(head_ref)
        if m_branch:
            return int(m_branch.group(1))
    if pr_body:
        m_fixes = _FIXES_ISSUE_RE.search(pr_body)
        if m_fixes:
            return int(m_fixes.group(1))
    return None


def linked_prs_for_issue(
    gh: Any,
    repo: str,
    issue: int,
    head_sha: str = "",
) -> list[int]:
    """Returns open PR numbers linked to `issue` by head_sha, branch convention, or Fixes/Closes/Resolves."""
    if gh is None or issue <= 0:
        return []
    eff_sha = head_sha.strip() if head_sha and head_sha.strip() != "0" * 40 else ""
    linked: list[int] = []
    if eff_sha:
        try:
            found_pr = gh.find_pr_for_sha(repo, eff_sha)
            if isinstance(found_pr, int) and not isinstance(found_pr, bool) and found_pr > 0 and found_pr != issue:
                linked.append(found_pr)
        except Exception:
            pass
    try:
        open_prs = gh.list_open_prs(repo) or []
        for p in open_prs:
            if not isinstance(p, dict):
                continue
            p_num = p.get("number")
            if not isinstance(p_num, int) or isinstance(p_num, bool) or p_num <= 0 or p_num == issue or p_num in linked:
                continue
            p_head = p.get("head") if isinstance(p.get("head"), dict) else {}
            p_ref = str((p_head or {}).get("ref") or "").strip()
            p_sha = str((p_head or {}).get("sha") or "").strip()
            p_body = str(p.get("body") or "")
            if _issue_from_pr_metadata(p_ref, p_body) == issue or (eff_sha and p_sha == eff_sha):
                linked.append(p_num)
    except Exception:
        pass
    return linked


def _mark_processed(
    store: Store,
    tenant_id: str,
    keys: list[Optional[str]],
    *,
    payload: Optional[dict[str, Any]] = None,
    kind: Optional[str] = None,
    sender: Optional[str] = None,
) -> None:
    for key in keys:
        if not key:
            continue
        store.record_processed_event(
            tenant_id,
            key,
            payload=payload,
            kind=kind,
            sender=sender,
        )


def parse_human_comment_directive(
    body: str,
    target: Literal["issue", "pr"],
) -> Optional[Literal["plan", "approve", "correct", "evaluate"]]:
    """Parses `@atobar-flow-agent` comment body into a deterministic intent."""
    text = (body or "").strip()
    if not text or "```flow-" in text or text.startswith(LOOP_EPOCH_MARKER) or STATUS_MARKER in text:
        return None
    m = _FLOW_DIRECTIVE_RE.search(text)
    if not m:
        return None
    remainder = text[m.end() :].strip(" \t\r\n:,-—")
    if not remainder:
        remainder = text[: m.start()].strip(" \t\r\n:,-—")
    if not remainder:
        return None
    if target == "issue":
        if _ISSUE_APPROVE_INTENT_RE.match(remainder):
            return "approve"
        return "plan"
    if _PR_EVALUATE_INTENT_RE.match(remainder):
        return "evaluate"
    return "correct"


def resolve_delegated_issue_number(
    repo: str,
    inner_event: dict[str, Any],
    gh: Any,
    store: Optional[Store] = None,
) -> Optional[int]:
    """Resolves a PR event back to its canonical delegated Issue number (`#53`) if linked."""
    pr_num = int(inner_event.get("pr_number") or inner_event.get("issue_number") or 0)
    if pr_num <= 0:
        return None

    explicit_issue = inner_event.get("delegated_issue_number")
    if explicit_issue is not None and int(explicit_issue) > 0:
        return int(explicit_issue)

    head_ref = str(
        inner_event.get("head_ref")
        or ((inner_event.get("head") or {}).get("ref") if isinstance(inner_event.get("head"), dict) else "")
        or ""
    ).strip()
    pr_body = str(inner_event.get("pr_body") or inner_event.get("body") or "")
    head_sha = str(
        inner_event.get("head_sha")
        or ((inner_event.get("head") or {}).get("sha") if isinstance(inner_event.get("head"), dict) else "")
        or ""
    ).strip()

    if not head_ref and not pr_body and gh is not None:
        try:
            pr_info = gh.get_pr(repo, pr_num) or {}
            if isinstance(pr_info, dict):
                head_ref = str((pr_info.get("head") or {}).get("ref") or "")
                pr_body = str(pr_info.get("body") or "")
                if not head_sha:
                    head_sha = str((pr_info.get("head") or {}).get("sha") or "")
        except Exception as exc:
            logger.warning("Could not read PR %s#%s while resolving issue: %s", repo, pr_num, exc)

    target_issue = _issue_from_pr_metadata(head_ref, pr_body)

    tenant = store.tenant_by_repo(repo) if store is not None else None
    if target_issue is None and tenant is not None and store is not None:
        try:
            rounds = list(store.loop_rounds_for_tenant(tenant.id) or [])
            for r in rounds:
                if head_sha and str(r.get("head_sha") or "") == head_sha and r.get("pr_number"):
                    target_issue = int(r["pr_number"])
                    break
                if int(r.get("pr_number") or 0) == pr_num:
                    target_issue = pr_num
                    break
        except Exception as exc:
            logger.warning("Could not read loop rounds for %s: %s", repo, exc)

    if target_issue is None:
        pr_labels = _issue_labels_from_sources(repo, pr_num, inner_event, gh)
        if pr_labels & {"flow:in-review", "flow:in-progress"}:
            target_issue = pr_num

    return target_issue


def handle_merged_pr_event(
    repo: str,
    inner_event: dict[str, Any],
    gh: Any,
    store: Store,
) -> Optional[dict[str, Any]]:
    """Transitions the delegated issue to `flow:shipped` when a factory PR is merged; ignores non-factory PRs."""
    if not bool(inner_event.get("merged")):
        return None
    pr_num = int(inner_event.get("pr_number") or inner_event.get("issue_number") or 0)
    if pr_num <= 0:
        return None

    target_issue = resolve_delegated_issue_number(repo, inner_event, gh, store)
    if target_issue is None:
        return None

    human_login = _human_assignee_for_tenant(repo, store)
    if gh is not None:
        for stale in ("flow:in-review", "flow:in-progress", "flow:escalated", "flow:needs-approval"):
            for num in ({target_issue, pr_num} if pr_num != target_issue else {target_issue}):
                if hasattr(gh, "remove_label"):
                    try:
                        gh.remove_label(repo, num, stale)
                    except Exception as exc:
                        logger.warning("Failed to remove %s on %s#%s: %s", stale, repo, num, exc)
                elif hasattr(gh, "remove_issue_label"):
                    try:
                        gh.remove_issue_label(repo, num, stale)
                    except Exception as exc:
                        logger.warning("Failed to remove %s on %s#%s: %s", stale, repo, num, exc)
        if hasattr(gh, "add_label"):
            try:
                gh.add_label(repo, target_issue, "flow:shipped")
            except Exception as exc:
                logger.warning("Failed to add flow:shipped on %s#%s: %s", repo, target_issue, exc)
        elif hasattr(gh, "add_issue_label"):
            try:
                gh.add_issue_label(repo, target_issue, "flow:shipped")
            except Exception as exc:
                logger.warning("Failed to add flow:shipped on %s#%s: %s", repo, target_issue, exc)
        if human_login and hasattr(gh, "remove_assignees"):
            for num in ({target_issue, pr_num} if pr_num != target_issue else {target_issue}):
                try:
                    gh.remove_assignees(repo, num, [human_login])
                except Exception as exc:
                    logger.warning("Failed to remove assignees on %s#%s: %s", repo, num, exc)

    return {"status": "shipped", "dispatched": False, "new_status": "shipped", "issue_number": target_issue}


def apply_actions(
    tenant_repo: str,
    pr_number: int,
    actions: list[Action],
    gh: GitHubClient,
    store: Store,
    *,
    claim_warm: bool = False,
    warm_instance_id: Optional[str] = None,
    spend: Optional[float] = None,
    round_num: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Dispatches roles, updates labels, and comments on GitHub."""
    claimed_dispatch: Optional[dict[str, Any]] = None
    human_login = _human_assignee_for_tenant(tenant_repo, store, gh)
    caps = _loop_caps_for_tenant(tenant_repo, store)
    has_escalate_action = any(isinstance(a, EscalateAction) for a in actions)

    for act in actions:
        if isinstance(act, DispatchAction):
            tenant = store.tenant_by_repo(tenant_repo)
            tenant_id = tenant.id if tenant else ""

            run_id, run_cred, cred_hash, expires_at = generate_run_credentials()

            if tenant_id:
                store.create_run_credential(
                    run_id,
                    tenant_id,
                    act.role,
                    cred_hash,
                    expires_at,
                    head_sha=act.head_sha or None,
                )

            can_claim_warm = bool(
                claim_warm
                and claimed_dispatch is None
                and act.role not in WARM_HANDOVER_FORBIDDEN_ROLES
            )

            if hasattr(store, "record_run_event"):
                store.record_run_event(
                    run_id,
                    "conductor.dispatch",
                    {"role": act.role, "head_sha": act.head_sha, "run_id": run_id, "warm_reuse": can_claim_warm},
                )

            if can_claim_warm:
                if hasattr(store, "upsert_session"):
                    now_iso = datetime.now(timezone.utc).isoformat()
                    store.upsert_session(
                        {
                            "instance_id": warm_instance_id or f"warm-{run_id}",
                            "run_id": run_id,
                            "tenant_id": tenant_id,
                            "role": act.role,
                            "target_repo": tenant_repo,
                            "issue_id": pr_number,
                            "status": "Running",
                            "started_at": now_iso,
                            "heartbeat_at": now_iso,
                        }
                    )
                claimed_dispatch = {
                    "action": "continue",
                    "role": act.role,
                    "run_id": run_id,
                    "run_credential": run_cred,
                    "head_sha": act.head_sha,
                }
                continue

            if (
                act.role == "arbitrator"
                and tenant is not None
                and hasattr(gh, "create_check_run")
                and not (
                    not claim_warm
                    and hasattr(store, "issue_session_active")
                    and tenant_id
                    and store.issue_session_active(tenant_id, pr_number)
                )
            ):
                if _run_serverless_arbitrator(
                    tenant_repo=tenant_repo,
                    pr_number=pr_number,
                    head_sha=act.head_sha,
                    run_id=run_id,
                    gh=gh,
                    store=store,
                    tenant=tenant,
                ):
                    continue

            agent_runtime = "nanobot"
            if tenant and hasattr(tenant, "manifest") and isinstance(tenant.manifest, dict):
                agent_runtime = tenant.manifest.get("loop", {}).get("agent_runtime", "nanobot")

            if tenant_id:
                now_iso = datetime.now(timezone.utc).isoformat()
                store.upsert_session(
                    {
                        "instance_id": f"pending-{run_id}",
                        "run_id": run_id,
                        "tenant_id": tenant_id,
                        "role": act.role,
                        "target_repo": tenant_repo,
                        "issue_id": pr_number,
                        "status": "pending",
                        "started_at": now_iso,
                        "heartbeat_at": now_iso,
                    }
                )

            gh.dispatch_flow_run(
                tenant_repo,
                {
                    "role": act.role,
                    "tenant": tenant_repo,
                    "pr": pr_number,
                    "head_sha": act.head_sha,
                    "run_id": run_id,
                    "run_credential": run_cred,
                    "agent_runtime": agent_runtime,
                },
            )
        elif isinstance(act, SetLabelAction):
            if act.label == "flow:escalated" and has_escalate_action:
                continue
            gh.add_label(tenant_repo, pr_number, act.label)
            if act.label in ("flow:needs-approval", "flow:escalated") and human_login and hasattr(gh, "add_assignees"):
                try:
                    gh.add_assignees(tenant_repo, pr_number, [human_login])
                except Exception as exc:
                    logger.warning("Failed to assign %s on %s#%s: %s", human_login, tenant_repo, pr_number, exc)
            if act.label == "flow:needs-approval":
                for stale in ("flow:in-progress", "flow:plan"):
                    try:
                        gh.remove_label(tenant_repo, pr_number, stale)
                    except Exception as exc:
                        logger.warning("Failed to remove label %s on %s#%s: %s", stale, tenant_repo, pr_number, exc)
                card_body = render_status_card(
                    "flow:needs-approval",
                    spend=spend,
                    caps=caps,
                    reason="Plan ready for human review.",
                    round_num=round_num,
                )
                upsert_issue_comment_by_marker(gh, tenant_repo, pr_number, STATUS_MARKER, card_body)
            elif act.label == "flow:escalated":
                card_body = render_status_card(
                    "flow:escalated",
                    spend=spend,
                    caps=caps,
                    reason="Run escalated for human review.",
                    round_num=round_num,
                )
                upsert_issue_comment_by_marker(gh, tenant_repo, pr_number, STATUS_MARKER, card_body)
        elif isinstance(act, RemoveLabelAction):
            gh.remove_label(tenant_repo, pr_number, act.label)
            if (
                act.label in ("flow:needs-approval", "flow:escalated")
                and human_login
                and hasattr(gh, "remove_assignees")
            ):
                try:
                    gh.remove_assignees(tenant_repo, pr_number, [human_login])
                except Exception as exc:
                    logger.warning("Failed to unassign %s on %s#%s: %s", human_login, tenant_repo, pr_number, exc)
        elif isinstance(act, CommentAction):
            gh.create_comment(tenant_repo, pr_number, act.body)
        elif isinstance(act, EscalateAction):
            if hasattr(store, "record_run_event"):
                store.record_run_event(
                    None,
                    "conductor.escalate",
                    {"tenant": tenant_repo, "pr": pr_number, "reason": act.reason},
                )
            body = render_status_card(
                "flow:escalated",
                spend=spend,
                caps=caps,
                reason=f"Circuit breaker tripped: `{act.circuit_breaker}` — {act.reason}",
                round_num=round_num,
            )
            upsert_issue_comment_by_marker(gh, tenant_repo, pr_number, STATUS_MARKER, body)
            gh.add_label(tenant_repo, pr_number, "flow:escalated")
            if human_login and hasattr(gh, "add_assignees"):
                try:
                    gh.add_assignees(tenant_repo, pr_number, [human_login])
                except Exception as exc:
                    logger.warning("Failed to assign %s on %s#%s: %s", human_login, tenant_repo, pr_number, exc)

    return claimed_dispatch


def process_flow_event(
    payload: dict[str, Any],
    gh: GitHubClient,
    store: Store,
    bot_logins: Optional[dict[str, str]] = None,
    *,
    claim_warm: bool = False,
    warm_instance_id: Optional[str] = None,
    authenticated_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Handles an incoming flow_event: idempotency check, state rebuild, step(), action apply."""
    if bot_logins is None:
        bot_logins = get_bot_logins()
    repo = payload.get("tenant") or payload.get("target_repo")
    if not repo:
        return {"status": "error", "reason": "missing tenant/target_repo"}

    raw_event = payload.get("event") or {}
    event_id = payload.get("event_id") or compute_event_id(raw_event)

    tenant = store.tenant_by_repo(repo)
    if not tenant:
        return {"status": "ignored", "reason": f"tenant not found: {repo}"}

    pr_number = raw_event.get("pr_number") or raw_event.get("issue_number")
    if not pr_number and raw_event.get("head_sha"):
        try:
            pr_number = gh.find_pr_for_sha(repo, str(raw_event["head_sha"]))
        except Exception:
            pr_number = None

    if not pr_number:
        return {"status": "ignored", "reason": "missing pr/issue number in event"}

    kind = str(raw_event.get("kind") or "")
    source_pr_number: Optional[int] = None
    pr_head_sha = str(raw_event.get("head_sha") or "").strip()
    is_pr_origin = bool(
        raw_event.get("is_pull_request") is True
        or kind
        in (
            "pull_request.opened",
            "pull_request.synchronize",
            "pull_request.closed",
            "pull_request_review.submitted",
            "pull_request_review_comment.created",
        )
        or (raw_event.get("pr_number") is not None and raw_event.get("issue_number") is None)
    )
    resolved_issue: Optional[int] = None
    pr_lookup_failed = False
    explicit_delegated = raw_event.get("delegated_issue_number")
    if explicit_delegated is not None and int(explicit_delegated) > 0:
        resolved_issue = int(explicit_delegated)
        if is_pr_origin or resolved_issue != int(pr_number):
            is_pr_origin = True
            source_pr_number = int(pr_number)
            pr_number = resolved_issue
    elif raw_event.get("is_pull_request") is not False:
        pr_info_probe: dict[str, Any] = {}
        try:
            maybe_pr = gh.get_pr(repo, int(pr_number))
            if isinstance(maybe_pr, dict):
                pr_info_probe = maybe_pr
        except Exception:
            pr_lookup_failed = True
        probe_head = pr_info_probe.get("head") if isinstance(pr_info_probe.get("head"), dict) else {}
        probe_ref = str((probe_head or {}).get("ref") or raw_event.get("head_ref") or "").strip()
        probe_body = str(pr_info_probe.get("body") or raw_event.get("pr_body") or raw_event.get("body") or "")
        probe_sha = str((probe_head or {}).get("sha") or "").strip()
        if not pr_head_sha and probe_sha:
            pr_head_sha = probe_sha
        if not is_pr_origin and (
            probe_ref
            or _FIXES_ISSUE_RE.search(probe_body)
            or "merged" in pr_info_probe
            or "base" in pr_info_probe
            or "pull_request" in pr_info_probe
        ):
            is_pr_origin = True
        if is_pr_origin:
            source_pr_number = int(pr_number)
            if not pr_lookup_failed:
                enriched_event = {
                    **raw_event,
                    "head_ref": probe_ref,
                    "pr_body": probe_body,
                    "head_sha": pr_head_sha,
                }
                resolved_issue = resolve_delegated_issue_number(repo, enriched_event, None, store)
            if resolved_issue is not None:
                pr_number = resolved_issue

    comment_id = raw_event.get("comment_id")
    comment_dedup_key = f"comment:{pr_number}:{comment_id}" if (pr_number and comment_id is not None) else None
    source_comment_dedup_key = (
        f"comment:{source_pr_number}:{comment_id}"
        if (source_pr_number and source_pr_number != pr_number and comment_id is not None)
        else None
    )

    if (
        store.has_processed_event(tenant.id, event_id)
        or (comment_dedup_key is not None and store.has_processed_event(tenant.id, comment_dedup_key))
        or (source_comment_dedup_key is not None and store.has_processed_event(tenant.id, source_comment_dedup_key))
    ):
        return {"status": "skipped", "reason": "duplicate event"}

    state = rebuild_loop_state(
        tenant_repo=repo,
        pr_number=int(pr_number),
        gh=gh,
        store=store,
        bot_logins=bot_logins,
        manifest=tenant.manifest,
        exclude_comment_id=comment_id,
    )
    if pr_head_sha and (not state.head_sha or state.head_sha == "0" * 40):
        state.head_sha = pr_head_sha

    machine_event: Optional[Event] = None
    role_to_record: Optional[str] = None
    round_to_record = state.round
    tokens_to_record = int(raw_event.get("tokens", 0))
    cost_to_record = float(raw_event.get("cost_usd", 0.0))
    wall_clock_to_record = float(raw_event.get("wall_clock_minutes", 0.0))
    verified_comment_body: Optional[str] = None
    verified_sender: Optional[str] = None

    if kind in ("issue_comment.created", "pull_request_review.submitted", "pull_request_review_comment.created"):
        try:
            cid_int = int(comment_id) if comment_id is not None else 0
        except (TypeError, ValueError):
            cid_int = 0
        if cid_int <= 0:
            return {"status": "ignored", "reason": "missing comment_id in issue_comment event"}

        target_comment = _fetch_comment_by_id(
            gh,
            repo,
            int(pr_number),
            cid_int,
            source_pr_number=source_pr_number,
            kind=kind,
        )
        if target_comment is None:
            return {"status": "ignored", "reason": f"comment {cid_int} not found on GitHub"}

        body = str(target_comment.get("body") or "")
        sender = str((target_comment.get("user") or {}).get("login") or "")
        verified_comment_body = body
        verified_sender = sender
        envelope = parse(body, author_login=sender, bots=bot_logins)

        if envelope:
            if body and hasattr(store, "loop_rounds_for_pr"):
                body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                if any(r.get("envelope_digest") == body_digest for r in store.loop_rounds_for_pr(tenant.id, int(pr_number))):
                    return {"status": "skipped", "reason": "duplicate event"}
            run_id = envelope.run_id
            if not run_id:
                logger.warning("Ignoring untrusted envelope from %s missing run_id (role %s)", sender, envelope.role)
                return {"status": "ignored", "reason": "untrusted envelope: missing run_id"}

            cred_record = store.get_run_credential(run_id)
            if (
                not cred_record
                or cred_record.get("tenant_id") != tenant.id
                or cred_record.get("role") != envelope.role
            ):
                logger.warning("Untrusted envelope: unknown or mismatched run_id %s", run_id)
                return {"status": "ignored", "reason": "untrusted envelope: unknown run_id"}

            expires_dt = _parse_ts(cred_record.get("expires_at"))
            if expires_dt is None or datetime.now(timezone.utc) > expires_dt + CREDENTIAL_EXPIRY_GRACE:
                logger.warning("Untrusted envelope: expired run_id %s", run_id)
                return {"status": "ignored", "reason": "untrusted envelope: expired run_id"}

            cred_sha = str(cred_record.get("head_sha") or "")
            if cred_sha and cred_sha != "0" * 40 and not isinstance(envelope, Plan):
                env_sha = str(getattr(envelope, "head_sha", "") or "")
                is_rework_ack = (
                    isinstance(envelope, Ack)
                    and cred_sha in state.head_sha_history
                    and bool(state.head_sha)
                    and env_sha == state.head_sha
                )
                if env_sha != cred_sha and not is_rework_ack:
                    logger.warning(
                        "Untrusted envelope: head_sha mismatch for run_id %s (got %s, expected %s)",
                        run_id,
                        env_sha,
                        cred_sha,
                    )
                    return {"status": "ignored", "reason": "untrusted envelope: head_sha mismatch"}

            claimed_cid = cred_record.get("envelope_comment_id")
            if claimed_cid is not None:
                if int(claimed_cid) != cid_int:
                    logger.warning("Untrusted envelope: run_id %s already claimed by another comment", run_id)
                    return {"status": "ignored", "reason": "untrusted envelope: run_id already claimed"}
            else:
                if envelope.role in WARM_HANDOVER_FORBIDDEN_ROLES and authenticated_run_id != run_id:
                    active_sess = store.get_session_by_run(run_id)
                    if active_sess and str(active_sess.get("status") or "") in (
                        "pending",
                        "Running",
                        "running",
                        "active",
                    ):
                        logger.warning(
                            "Ignoring unauthenticated verifier envelope for active/pending run_id %s before turn-complete",
                            run_id,
                        )
                        return {
                            "status": "ignored",
                            "reason": "untrusted envelope: verifier run must claim envelope via turn-complete",
                        }
                if not store.claim_run_credential_envelope(run_id, cid_int):
                    logger.warning("Untrusted envelope: run_id %s already claimed by another comment", run_id)
                    return {"status": "ignored", "reason": "untrusted envelope: run_id already claimed"}

        if isinstance(envelope, Plan):
            machine_event = PlanEvent(
                plan=envelope,
                trigger_label=str(raw_event.get("trigger_label") or ""),
                tokens=tokens_to_record,
                cost_usd=cost_to_record,
                wall_clock_minutes=wall_clock_to_record,
            )
            role_to_record = "planner"
        elif isinstance(envelope, Ack):
            machine_event = AckEvent(ack=envelope)
            role_to_record = "developer"
        elif isinstance(envelope, Verdict):
            machine_event = VerdictEvent(
                verdict=envelope,
                tokens=tokens_to_record,
                cost_usd=cost_to_record,
                wall_clock_minutes=wall_clock_to_record,
            )
            role_to_record = "evaluator"
            round_to_record = state.round + 1
        elif isinstance(envelope, Arbitration):
            machine_event = ArbitrationEvent(arbitration=envelope)
            role_to_record = None
        else:
            target_kind: Literal["issue", "pr"] = "pr" if is_pr_origin else "issue"
            intent = parse_human_comment_directive(body, target=target_kind)
            if intent is None:
                return {"status": "ignored", "reason": "comment is not a valid envelope"}

            sender_lower = sender.strip().lower()
            bot_set = {str(v).strip().lower() for v in (bot_logins or {}).values() if v}
            user_type = str((target_comment.get("user") or {}).get("type") or "").strip().lower()
            if not sender_lower or sender_lower.endswith("[bot]") or sender_lower in bot_set or user_type == "bot":
                return {"status": "ignored", "reason": "comment author is a bot"}

            assoc = str(target_comment.get("author_association") or "").strip().upper()
            human_assignee = resolve_human_assignee(tenant.manifest, repo)
            is_trusted = assoc in {"OWNER", "MEMBER", "COLLABORATOR"} or (
                not assoc and bool(human_assignee) and sender_lower == human_assignee.strip().lower()
            )
            if not is_trusted:
                return {"status": "ignored", "reason": f"untrusted comment author_association: {assoc or 'NONE'}"}

            if is_pr_origin and (resolved_issue is None or pr_lookup_failed):
                return {"status": "ignored", "reason": "PR is not a tracked factory PR"}

            is_active_sess = bool(store.issue_session_active(tenant.id, int(pr_number)))
            if is_active_sess:
                queued_marker = f"<!-- flow:queued-directive:{cid_int}:{target_kind}:{intent} -->"
                try:
                    gh.create_comment(
                        repo,
                        int(pr_number),
                        f"{queued_marker}\n⏳ **flow:** A run is currently active on this issue — "
                        f"your `@atobar-flow-agent` instruction (#{cid_int}) is queued.",
                    )
                except Exception as exc:
                    logger.warning("Failed to post queued directive notice on %s#%s: %s", repo, pr_number, exc)
                _mark_processed(
                    store,
                    tenant.id,
                    [event_id, comment_dedup_key, source_comment_dedup_key],
                    payload=raw_event,
                    kind=raw_event.get("kind"),
                    sender=sender,
                )
                return {
                    "status": "queued",
                    "dispatched": False,
                    "reason": "a run is already active on this issue; comment recorded for next turn",
                }

            try:
                gh.add_comment_reaction(
                    repo,
                    cid_int,
                    "eyes",
                    is_review_comment=(kind == "pull_request_review_comment.created"),
                )
            except Exception:
                pass

            effective_sha = pr_head_sha or state.head_sha
            machine_event = HumanCommentEvent(
                target=target_kind,
                intent=intent,
                head_sha=effective_sha,
                comment_id=cid_int,
            )

    elif kind in ("pull_request.synchronize", "pull_request.opened"):
        head_sha = raw_event.get("head_sha", "")
        machine_event = PushEvent(head_sha=head_sha)

    elif kind == "check_run.completed":
        check_name = raw_event.get("check_name", "")
        head_sha = raw_event.get("head_sha", "")
        check_run_id = raw_event.get("check_run_id") or raw_event.get("id")
        if head_sha != state.head_sha:
            return {
                "status": "ignored",
                "reason": f"stale check_run head_sha (got {head_sha}, expected {state.head_sha})",
            }
        gh_checks = gh.get_check_runs(repo, head_sha)
        auth_check = _authoritative_check_run(gh_checks, check_name, check_run_id=check_run_id)
        if auth_check is None or not auth_check.get("conclusion"):
            return {
                "status": "ignored",
                "reason": f"check_run {check_name} not found or not completed on GitHub for {head_sha}",
            }
        conclusion = str(auth_check["conclusion"])
        machine_event = CheckCompletedEvent(
            check_name=check_name,
            conclusion=conclusion,
            head_sha=head_sha,
        )

    elif kind == "issues.labeled":
        label = raw_event.get("label", "")
        machine_event = HumanLabelEvent(label=label)

    else:
        return {"status": "ignored", "reason": f"unhandled event kind: {kind}"}

    if role_to_record:
        event_head_sha = state.head_sha
        run_id_for_event = None
        if isinstance(machine_event, PlanEvent):
            event_head_sha = machine_event.plan.head_sha or state.head_sha or ("0" * 40)
            run_id_for_event = machine_event.plan.run_id
        elif isinstance(machine_event, AckEvent):
            event_head_sha = machine_event.ack.head_sha or state.head_sha
            run_id_for_event = machine_event.ack.run_id
        elif isinstance(machine_event, VerdictEvent):
            event_head_sha = machine_event.verdict.head_sha or state.head_sha
            run_id_for_event = getattr(machine_event.verdict, "run_id", None)
        elif isinstance(machine_event, ArbitrationEvent):
            event_head_sha = machine_event.arbitration.head_sha or state.head_sha
            run_id_for_event = getattr(machine_event.arbitration, "run_id", None)
        if not run_id_for_event and raw_event.get("run_id"):
            run_id_for_event = raw_event.get("run_id")

        usage_detail_to_record: Optional[dict[str, Any]] = None
        if run_id_for_event and hasattr(store, "get_session_by_run"):
            sess = store.get_session_by_run(run_id_for_event)
            if sess and sess.get("usage"):
                sess_usage = sess.get("usage", {})
                if isinstance(sess_usage, dict):
                    usage_detail_to_record = dict(sess_usage)
                    if sess.get("agent_runtime"):
                        usage_detail_to_record.setdefault("agent_runtime", str(sess["agent_runtime"]))
                    tot = usage_detail_to_record.get("total", {})
                    if isinstance(tot, dict):
                        in_tok = tot.get("input_tokens", 0) or 0
                        out_tok = tot.get("output_tokens", 0) or 0
                        cost_val = float(tot.get("cost_usd", 0.0) or 0.0)
                        if in_tok + out_tok > 0:
                            tokens_to_record = in_tok + out_tok
                        if cost_val > 0.0:
                            cost_to_record = cost_val
                    elif usage_detail_to_record.get("total_tokens"):
                        tokens_to_record = int(usage_detail_to_record.get("total_tokens", 0))
                    if usage_detail_to_record.get("cost_usd") and cost_to_record == 0.0:
                        cost_to_record = float(usage_detail_to_record.get("cost_usd", 0.0))
            if wall_clock_to_record <= 0.0 and sess and isinstance(sess.get("started_at"), str):
                try:
                    dt_start = datetime.fromisoformat(str(sess["started_at"]).replace("Z", "+00:00"))
                    elapsed_m = (datetime.now(timezone.utc) - dt_start).total_seconds() / 60.0
                    if elapsed_m > 0:
                        wall_clock_to_record = round(max(elapsed_m, 0.01), 4)
                except Exception:
                    pass

        envelope_digest = None
        comment_body = verified_comment_body if verified_comment_body is not None else raw_event.get("comment_body")
        if comment_body:
            envelope_digest = hashlib.sha256(comment_body.encode("utf-8")).hexdigest()

        raw_sha = str(event_head_sha or "")
        normalized_sha = (
            raw_sha.lower()
            if (len(raw_sha) == 40 and all(c in "0123456789abcdef" for c in raw_sha.lower()))
            else ("0" * 40)
        )

        if envelope is not None:
            if usage_detail_to_record is None:
                usage_detail_to_record = {}
            usage_detail_to_record["envelope"] = envelope.model_dump(mode="json")

        round_row: dict[str, Any] = {
            "tenant_id": tenant.id,
            "pr_number": int(pr_number),
            "round": round_to_record,
            "head_sha": normalized_sha,
            "role": role_to_record,
            "tokens": tokens_to_record,
            "cost_usd": cost_to_record,
            "wall_clock_minutes": wall_clock_to_record,
        }
        if usage_detail_to_record is not None:
            round_row["usage_detail"] = usage_detail_to_record
        if envelope_digest is not None:
            round_row["envelope_digest"] = envelope_digest
        store.record_loop_round(round_row)
        if cost_to_record > 0.0:
            store.record_goal_spend(tenant.id, float(cost_to_record))

        if hasattr(store, "record_run_event"):
            store.record_run_event(
                run_id_for_event,
                "conductor.round",
                {"pr": int(pr_number), "round": round_to_record, "envelope_digest": envelope_digest},
            )

    policy_dict = tenant.manifest.get("loop") or {}
    try:
        policy = LoopLimits(**policy_dict)
    except Exception:
        policy = LoopLimits()

    new_state, actions = step(state, machine_event, policy)

    # Re-check unconsumed queued human comments when a run would otherwise converge,
    # or mark them consumed when a matching role is dispatched.
    dispatched_roles = {a.role for a in actions if isinstance(a, DispatchAction)}
    if (
        not isinstance(machine_event, ArbitrationEvent)
        and (new_state.status == "converged" or bool(dispatched_roles & {"developer", "planner", "evaluator"}))
    ):
        try:
            issue_comments = gh.get_comments(repo, int(pr_number)) or []
        except Exception:
            issue_comments = []
        pending_queued: list[tuple[int, Literal["issue", "pr"], Literal["plan", "approve", "correct", "evaluate"]]] = []
        for ic in issue_comments:
            ic_body = str(ic.get("body") or "")
            if is_epoch_marker(ic, bot_logins, store=store, tenant_id=tenant.id):
                pending_queued = []
                continue
            m_q = _QUEUED_DIRECTIVE_RE.search(ic_body)
            if not m_q:
                continue
            q_cid = int(m_q.group(1))
            if store.has_processed_event(tenant.id, f"consumed_directive:{q_cid}"):
                continue
            q_target: Literal["issue", "pr"] = "pr" if m_q.group(2) == "pr" else "issue"
            q_intent_raw = m_q.group(3)
            q_intent: Literal["plan", "approve", "correct", "evaluate"] = (
                "approve"
                if q_intent_raw == "approve"
                else "plan"
                if q_intent_raw == "plan"
                else "evaluate"
                if q_intent_raw == "evaluate"
                else "correct"
            )
            pending_queued.append((q_cid, q_target, q_intent))
        if pending_queued:
            if new_state.status == "converged":
                q_cid, q_target, q_intent = pending_queued[-1]
                _mark_processed(store, tenant.id, [f"consumed_directive:{c}" for c, _, _ in pending_queued])
                new_state, actions = step(
                    new_state,
                    HumanCommentEvent(
                        target=q_target,
                        intent=q_intent,
                        head_sha=new_state.head_sha,
                        comment_id=q_cid,
                    ),
                    policy,
                )
            elif dispatched_roles & {"developer", "planner", "evaluator"}:
                _mark_processed(store, tenant.id, [f"consumed_directive:{c}" for c, _, _ in pending_queued])

    if (
        isinstance(machine_event, ArbitrationEvent)
        and machine_event.arbitration.decision == "pass"
        and machine_event.arbitration.action_taken == "merged"
    ):
        store.record_goal_merge(tenant.id, bool(machine_event.arbitration.progress))

    handover = apply_actions(
        repo,
        int(pr_number),
        actions,
        gh,
        store,
        claim_warm=claim_warm,
        warm_instance_id=warm_instance_id,
        spend=new_state.accumulated_cost_usd,
        round_num=new_state.round,
    )

    recorded_sender = verified_sender if verified_sender is not None else raw_event.get("sender")
    _mark_processed(
        store,
        tenant.id,
        [event_id, comment_dedup_key, source_comment_dedup_key],
        payload=raw_event,
        kind=raw_event.get("kind"),
        sender=recorded_sender,
    )

    return {
        "status": "success",
        "actions_count": len(actions),
        "new_status": new_state.status,
        "handover": handover,
    }
