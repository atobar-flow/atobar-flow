"""Janitor service for EC2 Spot instance lifecycle and orphan branch hygiene (#301 WP-5)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from atobar_contracts.constants import get_bot_logins
from atobar_contracts.github_rest import SpotInstanceTags
from atobar_contracts.store import Store
from flow_engine.api import get_github_client
from flow_engine.store import get_store, session_is_live

logger = logging.getLogger("flow_engine.janitor")

_FLOW_BRANCH_PREFIXES = ("flow/", "flow-", "atobar/")
_BRANCH_ISSUE_RE = re.compile(r"^(?:flow[-/](?:wip[-/])?|atobar/)(?:issue[-/])?(\d+)\b")
_OPEN_SESSION_STATUSES = ("Running", "running", "active", "pending", "")
_LOOP_ROUND_ROLES = ("developer", "evaluator", "arbitrator", "conductor")


@dataclass
class InstanceTerminationDecision:
    """Decision record for a Janitor instance/session sweep action."""

    instance_id: str
    run_id: Optional[str]
    tenant: str
    pr: int
    rule: str
    reason: str
    terminated: bool
    dry_run: bool


@dataclass
class BranchSweepDecision:
    """Decision record for a Janitor branch hygiene evaluation."""

    repo: str
    tenant_id: str
    branch: str
    action: str
    reason: str
    dry_run: bool


def _is_dry_run(explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("JANITOR_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_managed_instance(inst: dict[str, Any]) -> bool:
    """Tag safety check: only instances explicitly tagged atobar:managed=true are eligible."""
    return SpotInstanceTags.from_ec2_instance(inst).managed


def _list_open_sessions(store: Store) -> list[dict[str, Any]]:
    return list(store.list_open_sessions())


def _find_session(store: Store, instance_id: str, run_id: str) -> Optional[dict[str, Any]]:
    if instance_id:
        sess = store.get_session_by_instance(instance_id)
        if isinstance(sess, dict):
            return dict(sess)
    if run_id:
        sess = store.get_session_by_run(run_id)
        if isinstance(sess, dict):
            return dict(sess)
    return None


def _mark_session_terminal(
    store: Store,
    *,
    instance_id: str,
    run_id: Optional[str],
    tenant_id: str,
    target_repo: str,
    issue_id: int,
    role: str,
    session: Optional[dict[str, Any]],
    status_value: str,
    halted_reason: Optional[str],
    now: datetime,
) -> None:
    existing_status = str((session or {}).get("status") or "").strip()
    if existing_status not in _OPEN_SESSION_STATUSES:
        return
    patch: dict[str, Any] = {"status": status_value}
    if halted_reason is not None:
        patch["halted_reason"] = halted_reason
        patch["halt_reason"] = halted_reason

    existing_inst_id = str((session or {}).get("instance_id") or "")
    pending_id = f"pending-{run_id}" if run_id else ""

    if instance_id and not instance_id.startswith("pending-"):
        if pending_id:
            store.update_session(pending_id, {"status": "dispatched"})
        if existing_inst_id and existing_inst_id not in (pending_id, instance_id):
            store.update_session(existing_inst_id, patch)
        base_row: dict[str, Any] = dict(session or {})
        base_row.update(
            {
                "instance_id": instance_id,
                "run_id": run_id or base_row.get("run_id"),
                "tenant_id": tenant_id or base_row.get("tenant_id") or "",
                "target_repo": target_repo or base_row.get("target_repo") or "",
                "issue_id": issue_id or int(base_row.get("issue_id") or 0),
                "role": role or str(base_row.get("role") or "developer"),
                "status": status_value,
            }
        )
        if halted_reason is not None:
            base_row["halted_reason"] = halted_reason
            base_row["halt_reason"] = halted_reason
        if not base_row.get("heartbeat_at"):
            base_row["heartbeat_at"] = now.isoformat()
        store.upsert_session(base_row)
    else:
        target_inst_id = instance_id or existing_inst_id or pending_id
        if target_inst_id:
            store.update_session(target_inst_id, patch)


def _record_tombstone_round(
    store: Store,
    *,
    tenant_id: str,
    pr_number: int,
    session: Optional[dict[str, Any]],
    role: str,
    run_id: Optional[str] = None,
) -> None:
    if not tenant_id or pr_number <= 0:
        return
    sess = session or {}
    eff_role = str(sess.get("role") or role or "developer")
    if eff_role not in _LOOP_ROUND_ROLES:
        return
    eff_run_id = str(run_id or sess.get("run_id") or sess.get("instance_id") or "").strip()
    existing_rounds = store.loop_rounds_for_pr(tenant_id, pr_number)
    if eff_run_id:
        for existing in existing_rounds:
            if existing.get("envelope_digest") != "reaped:lost_heartbeat":
                continue
            existing_usage = existing.get("usage_detail")
            existing_rid = str(
                existing.get("run_id")
                or (existing_usage.get("run_id") if isinstance(existing_usage, dict) else "")
                or ""
            ).strip()
            if existing_rid == eff_run_id:
                return
    next_round = max((int(r.get("round", 0)) for r in existing_rounds), default=0) + 1
    usage_detail = sess.get("usage")
    tokens_to_record = 0
    if isinstance(usage_detail, dict):
        tokens_to_record = int(
            usage_detail.get("total_tokens")
            or ((usage_detail.get("input_tokens") or 0) + (usage_detail.get("output_tokens") or 0))
        )
    merged_usage: dict[str, Any] = dict(usage_detail) if isinstance(usage_detail, dict) else {}
    if eff_run_id:
        merged_usage.setdefault("run_id", eff_run_id)
    round_row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "pr_number": pr_number,
        "round": next_round,
        "head_sha": str(sess.get("head_sha") or "0" * 40),
        "role": eff_role,
        "cost_usd": float(sess.get("cost_estimate") or 0.0),
        "tokens": tokens_to_record,
        "envelope_digest": "reaped:lost_heartbeat",
    }
    if merged_usage:
        round_row["usage_detail"] = merged_usage
    try:
        store.record_loop_round(round_row)
    except Exception as exc:
        logger.warning("Failed to record tombstone loop_round for %s#%d: %s", tenant_id, pr_number, exc)


def _check_pr_terminal_state(
    gh: Any,
    repo: str,
    pr_number: int,
) -> Optional[tuple[str, str, Optional[str]]]:
    """Return (reason, session_status, halted_reason) if PR/issue is closed, escalated, or shipped."""
    if gh is None or not repo or pr_number <= 0:
        return None
    pr_info: Optional[dict[str, Any]] = None
    if hasattr(gh, "get_pr"):
        try:
            res = gh.get_pr(repo, pr_number)
            if isinstance(res, dict) and res:
                pr_info = res
        except Exception as exc:
            logger.debug("PR lookup %s#%d fell back to issue check: %s", repo, pr_number, exc)
    if pr_info is None and hasattr(gh, "get_issue"):
        try:
            res = gh.get_issue(repo, pr_number)
            if isinstance(res, dict) and res:
                pr_info = res
        except Exception as exc:
            logger.warning("Failed to query PR/issue %s#%d during janitor sweep: %s", repo, pr_number, exc)
            return None
    if not isinstance(pr_info, dict) or not pr_info:
        return None

    state = str(pr_info.get("state") or "open").lower()
    merged = bool(pr_info.get("merged") or pr_info.get("merged_at"))
    raw_labels = pr_info.get("labels") or []
    labels = {
        str(lbl.get("name") if isinstance(lbl, dict) else lbl)
        for lbl in raw_labels
        if lbl
    }

    if "flow:escalated" in labels:
        return ("pr_escalated", "Halted: pr_escalated", "pr_escalated")
    if "flow:shipped" in labels or merged:
        return ("pr_shipped", "Complete", None)
    if state == "closed":
        return ("pr_closed", "Halted: pr_closed", "pr_closed")
    return None


def sweep_instances(
    ec2: Any,
    store: Any,
    gh: Optional[Any] = None,
    *,
    now: Optional[datetime] = None,
    boot_grace_minutes: float = 10.0,
    heartbeat_timeout_minutes: float = 10.0,
    default_max_minutes: int = 60,
    orphan_grace_minutes: float = 1.0,
    dry_run: Optional[bool] = None,
) -> list[InstanceTerminationDecision]:
    """Evaluate Rules 1–5 across managed EC2 Spot instances and active sessions."""
    current_time = now or datetime.now(timezone.utc)
    is_dry = _is_dry_run(dry_run)

    filters = [
        {"Name": "tag:atobar:managed", "Values": ["true"]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ]
    managed_instances: list[dict[str, Any]] = []
    verified_managed_ids: set[str] = set()
    next_token: Optional[str] = None
    while True:
        desc_kwargs: dict[str, Any] = {"Filters": filters}
        if next_token:
            desc_kwargs["NextToken"] = next_token
        resp = (
            ec2.describe_instances(**desc_kwargs)
            if (ec2 is not None and hasattr(ec2, "describe_instances"))
            else {"Reservations": []}
        )
        for res in (resp or {}).get("Reservations", []):
            for inst in (res or {}).get("Instances", []):
                iid = str(inst.get("InstanceId") or "")
                if not iid:
                    continue
                state_name = str((inst.get("State") or {}).get("Name") or "running").lower()
                if state_name not in ("running", "pending"):
                    continue
                # Strict code-level tag safety: never touch an instance without atobar:managed == "true"
                if not _is_managed_instance(inst):
                    continue
                managed_instances.append(inst)
                verified_managed_ids.add(iid)
        next_token = str((resp or {}).get("NextToken") or "") or None
        if not next_token:
            break

    decisions: list[InstanceTerminationDecision] = []
    handled_instance_ids: set[str] = set()
    handled_run_ids: set[str] = set()
    to_terminate_ids: list[str] = []
    pending_ec2_commits: list[tuple[str, Callable[[], None]]] = []

    def _stage_ec2_decision(
        *,
        instance_id: str,
        run_id: Optional[str],
        event_run_id: Optional[str],
        tenant_id: str,
        tenant_repo: str,
        pr_num: int,
        role: str,
        session: Optional[dict[str, Any]],
        rule: str,
        reason: str,
        session_status: Optional[str],
        halted_reason: Optional[str],
        record_tombstone: bool,
        event_payload: dict[str, Any],
    ) -> None:
        to_terminate_ids.append(instance_id)

        def _commit() -> None:
            if not is_dry:
                if session_status is not None:
                    _mark_session_terminal(
                        store,
                        instance_id=instance_id,
                        run_id=run_id,
                        tenant_id=tenant_id,
                        target_repo=tenant_repo,
                        issue_id=pr_num,
                        role=role,
                        session=session,
                        status_value=session_status,
                        halted_reason=halted_reason,
                        now=current_time,
                    )
                if record_tombstone:
                    _record_tombstone_round(
                        store,
                        tenant_id=tenant_id,
                        pr_number=pr_num,
                        session=session,
                        role=role,
                        run_id=run_id,
                    )
                if run_id:
                    store.mark_run_credential_used(run_id)
            decisions.append(
                InstanceTerminationDecision(
                    instance_id=instance_id,
                    run_id=run_id,
                    tenant=tenant_repo,
                    pr=pr_num,
                    rule=rule,
                    reason=reason,
                    terminated=not is_dry,
                    dry_run=is_dry,
                )
            )
            store.record_run_event(
                event_run_id,
                "janitor.instance_terminated",
                event_payload,
            )

        pending_ec2_commits.append((instance_id, _commit))

    for inst in managed_instances:
        instance_id = str(inst["InstanceId"])
        launch_time = _parse_iso_dt(inst.get("LaunchTime"))
        age_minutes = (
            (current_time - launch_time).total_seconds() / 60.0
            if launch_time is not None
            else None
        )
        parsed_tags = SpotInstanceTags.from_ec2_instance(inst)

        run_id = parsed_tags.run_id
        tenant_repo = parsed_tags.tenant
        pr_num = parsed_tags.pr_number
        role = parsed_tags.role

        cred = store.get_run_credential(run_id) if run_id else None
        session = _find_session(store, instance_id, run_id)

        if not run_id and session is not None:
            run_id = str(session.get("run_id") or "")
            if run_id and cred is None:
                cred = store.get_run_credential(run_id)

        # Only pass run_id to run_events FK column when a matching run_credentials row exists
        event_run_id = run_id if (run_id and cred is not None) else None

        tenant_id = str(
            (session or {}).get("tenant_id")
            or (cred or {}).get("tenant_id")
            or ""
        )
        if not tenant_repo and session is not None:
            tenant_repo = str(session.get("target_repo") or "")
        if not tenant_id and tenant_repo:
            t_obj = store.tenant_by_repo(tenant_repo)
            if t_obj is not None:
                tenant_id = t_obj.id
        if not tenant_repo and tenant_id:
            t_obj = store.tenant_by_id(tenant_id)
            if t_obj is not None:
                tenant_repo = t_obj.repo
        if pr_num <= 0 and session is not None:
            pr_num = _safe_int(session.get("issue_id"), 0)
        if cred is not None and cred.get("role"):
            role = str(cred["role"])
        elif session is not None and session.get("role"):
            role = str(session["role"])

        # Rule 4: orphan_instance (no matching run_credentials or sessions row, past grace window or unknown LaunchTime)
        if cred is None and session is None:
            if age_minutes is not None and age_minutes < orphan_grace_minutes:
                continue
            handled_instance_ids.add(instance_id)
            if run_id:
                handled_run_ids.add(run_id)
            _stage_ec2_decision(
                instance_id=instance_id,
                run_id=run_id or None,
                event_run_id=event_run_id,
                tenant_id=tenant_id,
                tenant_repo=tenant_repo,
                pr_num=pr_num,
                role=role,
                session=None,
                rule="orphan_instance",
                reason="orphan_instance",
                session_status=None,
                halted_reason=None,
                record_tombstone=False,
                event_payload={
                    "instance_id": instance_id,
                    "run_id": run_id or None,
                    "tenant": tenant_repo,
                    "pr": pr_num,
                    "rule": "orphan_instance",
                    "reason": "orphan_instance",
                    "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
                    "dry_run": is_dry,
                },
            )
            continue

        eff_age_minutes = age_minutes if age_minutes is not None else 0.0

        # Rule 5: pr_closed_or_escalated
        pr_terminal = _check_pr_terminal_state(gh, tenant_repo, pr_num)
        if pr_terminal is not None:
            pr_reason, session_status, halted_reason = pr_terminal
            handled_instance_ids.add(instance_id)
            if run_id:
                handled_run_ids.add(run_id)
            _stage_ec2_decision(
                instance_id=instance_id,
                run_id=run_id or None,
                event_run_id=event_run_id,
                tenant_id=tenant_id,
                tenant_repo=tenant_repo,
                pr_num=pr_num,
                role=role,
                session=session,
                rule="pr_closed_or_escalated",
                reason=pr_reason,
                session_status=session_status,
                halted_reason=halted_reason,
                record_tombstone=False,
                event_payload={
                    "instance_id": instance_id,
                    "run_id": run_id or None,
                    "tenant": tenant_repo,
                    "pr": pr_num,
                    "rule": "pr_closed_or_escalated",
                    "reason": pr_reason,
                    "age_minutes": round(eff_age_minutes, 2),
                    "dry_run": is_dry,
                },
            )
            continue

        # Rule 3: max_minutes_exceeded
        if parsed_tags.max_minutes is not None:
            max_minutes = parsed_tags.max_minutes
        elif tenant_id:
            max_minutes = int(store.get_spot_max_minutes(tenant_id))
        else:
            max_minutes = int(default_max_minutes)

        if eff_age_minutes > float(max_minutes):
            handled_instance_ids.add(instance_id)
            if run_id:
                handled_run_ids.add(run_id)
            _stage_ec2_decision(
                instance_id=instance_id,
                run_id=run_id or None,
                event_run_id=event_run_id,
                tenant_id=tenant_id,
                tenant_repo=tenant_repo,
                pr_num=pr_num,
                role=role,
                session=session,
                rule="max_minutes_exceeded",
                reason="max_minutes_exceeded",
                session_status="Halted: max_minutes_exceeded",
                halted_reason="max_minutes_exceeded",
                record_tombstone=False,
                event_payload={
                    "instance_id": instance_id,
                    "run_id": run_id or None,
                    "tenant": tenant_repo,
                    "pr": pr_num,
                    "rule": "max_minutes_exceeded",
                    "reason": "max_minutes_exceeded",
                    "age_minutes": round(eff_age_minutes, 2),
                    "max_minutes": max_minutes,
                    "dry_run": is_dry,
                },
            )
            continue

        # Distinguish Rule 1 (boot_timeout) vs Rule 2 (stale_heartbeat)
        sess_inst_id = str((session or {}).get("instance_id") or "")
        hb_raw = (session or {}).get("heartbeat_at")
        has_first_heartbeat = bool(
            session is not None and hb_raw and not sess_inst_id.startswith("pending-")
        )

        if not has_first_heartbeat:
            # Rule 1: boot_timeout
            if eff_age_minutes > boot_grace_minutes:
                handled_instance_ids.add(instance_id)
                if run_id:
                    handled_run_ids.add(run_id)
                _stage_ec2_decision(
                    instance_id=instance_id,
                    run_id=run_id or None,
                    event_run_id=event_run_id,
                    tenant_id=tenant_id,
                    tenant_repo=tenant_repo,
                    pr_num=pr_num,
                    role=role,
                    session=session,
                    rule="boot_timeout",
                    reason="boot_timeout",
                    session_status="Halted: boot_timeout",
                    halted_reason="boot_timeout",
                    record_tombstone=False,
                    event_payload={
                        "instance_id": instance_id,
                        "run_id": run_id or None,
                        "tenant": tenant_repo,
                        "pr": pr_num,
                        "rule": "boot_timeout",
                        "reason": "boot_timeout",
                        "age_minutes": round(eff_age_minutes, 2),
                        "dry_run": is_dry,
                    },
                )
            else:
                handled_instance_ids.add(instance_id)
                if run_id:
                    handled_run_ids.add(run_id)
            continue

        # Rule 2: stale_heartbeat on a managed EC2 instance (also retries EC2 termination if session is already terminal)
        sess_status = str((session or {}).get("status") or "Running").strip()
        is_open_session = sess_status in _OPEN_SESSION_STATUSES
        hb_dt = _parse_iso_dt(hb_raw)
        if hb_dt is not None:
            hb_age_min = (current_time - hb_dt).total_seconds() / 60.0
            if hb_age_min > heartbeat_timeout_minutes:
                handled_instance_ids.add(instance_id)
                if run_id:
                    handled_run_ids.add(run_id)
                _stage_ec2_decision(
                    instance_id=instance_id,
                    run_id=run_id or None,
                    event_run_id=event_run_id,
                    tenant_id=tenant_id,
                    tenant_repo=tenant_repo,
                    pr_num=pr_num,
                    role=role,
                    session=session,
                    rule="stale_heartbeat",
                    reason="stale_heartbeat",
                    session_status="Halted: stale_heartbeat",
                    halted_reason="stale_heartbeat",
                    record_tombstone=is_open_session,
                    event_payload={
                        "instance_id": instance_id,
                        "run_id": run_id or None,
                        "tenant": tenant_repo,
                        "pr": pr_num,
                        "rule": "stale_heartbeat",
                        "reason": "stale_heartbeat",
                        "heartbeat_age_minutes": round(hb_age_min, 2),
                        "dry_run": is_dry,
                    },
                )
            else:
                handled_instance_ids.add(instance_id)
                if run_id:
                    handled_run_ids.add(run_id)

    # Terminate verified atobar:managed=true EC2 instances BEFORE committing their terminal DB state
    safe_ids = list(dict.fromkeys(iid for iid in to_terminate_ids if iid in verified_managed_ids))
    confirmed_ids: set[str] = set(safe_ids) if is_dry else set()
    if safe_ids and not is_dry and ec2 is not None and hasattr(ec2, "terminate_instances"):
        term_resp = ec2.terminate_instances(InstanceIds=safe_ids)
        term_list = (term_resp or {}).get("TerminatingInstances") if isinstance(term_resp, dict) else None
        if isinstance(term_list, list):
            confirmed_ids = {
                str(item.get("InstanceId") or "")
                for item in term_list
                if isinstance(item, dict) and item.get("InstanceId")
            }
        else:
            confirmed_ids = set(safe_ids)
    for staged_iid, commit_fn in pending_ec2_commits:
        if staged_iid in confirmed_ids:
            commit_fn()

    # Also reap stale or PR-closed sessions in DB whose EC2 instance is absent or unmanaged
    open_db_sessions = sorted(
        _list_open_sessions(store),
        key=lambda s: 1 if str(s.get("instance_id") or "").startswith("pending-") else 0,
    )
    for sess in open_db_sessions:
        sess_inst_id = str(sess.get("instance_id") or "")
        sess_run_id = str(sess.get("run_id") or "")
        if (sess_inst_id and sess_inst_id in handled_instance_ids) or (
            sess_run_id and sess_run_id in handled_run_ids
        ):
            continue

        sess_cred = store.get_run_credential(sess_run_id) if sess_run_id else None
        sess_event_run_id = sess_run_id if (sess_run_id and sess_cred is not None) else None

        tenant_id = str(sess.get("tenant_id") or "")
        tenant_repo = str(sess.get("target_repo") or "")
        if not tenant_id and tenant_repo:
            t_obj = store.tenant_by_repo(tenant_repo)
            if t_obj is not None:
                tenant_id = t_obj.id
        if not tenant_repo and tenant_id:
            t_obj = store.tenant_by_id(tenant_id)
            if t_obj is not None:
                tenant_repo = t_obj.repo
        pr_num = _safe_int(sess.get("issue_id"), 0)
        role = str(sess.get("role") or "developer")

        pr_terminal = _check_pr_terminal_state(gh, tenant_repo, pr_num)
        if pr_terminal is not None:
            pr_reason, session_status, halted_reason = pr_terminal
            if sess_inst_id:
                handled_instance_ids.add(sess_inst_id)
            if sess_run_id:
                handled_run_ids.add(sess_run_id)
            if not is_dry:
                _mark_session_terminal(
                    store,
                    instance_id=sess_inst_id,
                    run_id=sess_run_id or None,
                    tenant_id=tenant_id,
                    target_repo=tenant_repo,
                    issue_id=pr_num,
                    role=role,
                    session=sess,
                    status_value=session_status,
                    halted_reason=halted_reason,
                    now=current_time,
                )
                if sess_run_id:
                    store.mark_run_credential_used(sess_run_id)
            decisions.append(
                InstanceTerminationDecision(
                    instance_id=sess_inst_id,
                    run_id=sess_run_id or None,
                    tenant=tenant_repo,
                    pr=pr_num,
                    rule="pr_closed_or_escalated",
                    reason=pr_reason,
                    terminated=False,
                    dry_run=is_dry,
                )
            )
            store.record_run_event(
                sess_event_run_id,
                "janitor.instance_terminated",
                {
                    "instance_id": sess_inst_id,
                    "run_id": sess_run_id or None,
                    "tenant": tenant_repo,
                    "pr": pr_num,
                    "rule": "pr_closed_or_escalated",
                    "reason": pr_reason,
                    "dry_run": is_dry,
                },
            )
            continue

        hb_raw = sess.get("heartbeat_at")
        hb_dt = _parse_iso_dt(hb_raw or sess.get("started_at"))
        if hb_dt is None:
            continue
        hb_age_min = (current_time - hb_dt).total_seconds() / 60.0
        is_unbooted = bool(not hb_raw or sess_inst_id.startswith("pending-"))
        threshold_min = boot_grace_minutes if is_unbooted else heartbeat_timeout_minutes
        if hb_age_min > threshold_min:
            rule_name = "boot_timeout" if is_unbooted else "stale_heartbeat"
            status_val = f"Halted: {rule_name}"
            if sess_inst_id:
                handled_instance_ids.add(sess_inst_id)
            if sess_run_id:
                handled_run_ids.add(sess_run_id)
            if not is_dry:
                _mark_session_terminal(
                    store,
                    instance_id=sess_inst_id,
                    run_id=sess_run_id or None,
                    tenant_id=tenant_id,
                    target_repo=tenant_repo,
                    issue_id=pr_num,
                    role=role,
                    session=sess,
                    status_value=status_val,
                    halted_reason=rule_name,
                    now=current_time,
                )
                if not is_unbooted:
                    _record_tombstone_round(
                        store,
                        tenant_id=tenant_id,
                        pr_number=pr_num,
                        session=sess,
                        role=role,
                        run_id=sess_run_id or None,
                    )
                if sess_run_id:
                    store.mark_run_credential_used(sess_run_id)
            decisions.append(
                InstanceTerminationDecision(
                    instance_id=sess_inst_id,
                    run_id=sess_run_id or None,
                    tenant=tenant_repo,
                    pr=pr_num,
                    rule=rule_name,
                    reason=rule_name,
                    terminated=False,
                    dry_run=is_dry,
                )
            )
            store.record_run_event(
                sess_event_run_id,
                "janitor.instance_terminated",
                {
                    "instance_id": sess_inst_id,
                    "run_id": sess_run_id or None,
                    "tenant": tenant_repo,
                    "pr": pr_num,
                    "rule": rule_name,
                    "reason": rule_name,
                    "heartbeat_age_minutes": round(hb_age_min, 2),
                    "dry_run": is_dry,
                },
            )

    return decisions


def _extract_login(field: Any) -> str:
    if isinstance(field, str):
        return field.strip()
    if isinstance(field, dict):
        return str(field.get("login") or field.get("name") or "").strip()
    return ""


def _is_bot_commit(commit: dict[str, Any], allowed_bots: set[str]) -> bool:
    if "author" in commit and commit.get("author") is None:
        return False
    author_login = _extract_login(commit.get("author"))
    if not author_login and commit.get("login"):
        author_login = str(commit.get("login")).strip()

    committer_login = _extract_login(commit.get("committer"))

    if not author_login:
        return False
    if author_login not in allowed_bots:
        return False
    if committer_login and committer_login not in allowed_bots and committer_login != "web-flow":
        return False
    return True


def _extract_branch_commit_dt(branch: dict[str, Any], commits: list[dict[str, Any]]) -> Optional[datetime]:
    for candidate in (
        branch.get("committed_at"),
        branch.get("commit_date"),
        ((branch.get("commit") or {}).get("commit") or {}).get("committer", {}).get("date"),
        ((branch.get("commit") or {}).get("commit") or {}).get("author", {}).get("date"),
    ):
        dt = _parse_iso_dt(candidate)
        if dt is not None:
            return dt
    for c in reversed(commits):
        for candidate in (
            c.get("committed_at"),
            ((c.get("commit") or {}).get("committer") or {}).get("date"),
            ((c.get("commit") or {}).get("author") or {}).get("date"),
        ):
            dt = _parse_iso_dt(candidate)
            if dt is not None:
                return dt
    return None


def _parse_branch_issue_number(branch_name: str) -> Optional[int]:
    m = _BRANCH_ISSUE_RE.match(branch_name)
    if m:
        return int(m.group(1))
    return None


def sweep_branches(
    store: Any,
    gh: Any,
    *,
    now: Optional[datetime] = None,
    pr_closed_grace_hours: float = 24.0,
    no_pr_grace_days: float = 7.0,
    bot_logins: Optional[set[str] | dict[str, str]] = None,
    dry_run: Optional[bool] = None,
) -> list[BranchSweepDecision]:
    """Evaluate Rule 6 (orphan_branch_purge) across active tenants with all 4 safety guards."""
    current_time = now or datetime.now(timezone.utc)
    is_dry = _is_dry_run(dry_run)

    if isinstance(bot_logins, dict):
        allowed_bots = {str(v) for v in bot_logins.values() if v}
    elif isinstance(bot_logins, set):
        allowed_bots = {str(v) for v in bot_logins if v}
    else:
        allowed_bots = {str(v) for v in get_bot_logins().values() if v}
    allowed_bots.update({"atobar-flow-agent[bot]", "atobar-flow-agent"})

    tenants = store.list_tenants()
    decisions: list[BranchSweepDecision] = []

    def _log_skip(tenant_id: str, repo: str, branch_name: str, reason: str) -> None:
        decisions.append(
            BranchSweepDecision(
                repo=repo,
                tenant_id=tenant_id,
                branch=branch_name,
                action="skipped",
                reason=reason,
                dry_run=is_dry,
            )
        )
        store.record_run_event(
            None,
            "janitor.branch_skipped",
            {
                "repo": repo,
                "tenant_id": tenant_id,
                "branch": branch_name,
                "reason": reason,
                "dry_run": is_dry,
            },
        )

    for tenant in tenants:
        if getattr(tenant, "status", "") != "active":
            continue
        repo = tenant.repo
        manifest = getattr(tenant, "manifest", None) or {}
        default_branch = str(manifest.get("default_branch") or "").strip()
        if not default_branch:
            if gh is None or not hasattr(gh, "get_default_branch"):
                logger.warning("Cannot determine default_branch for %s; skipping branch sweep", repo)
                continue
            try:
                default_branch = str(gh.get_default_branch(repo) or "").strip()
            except Exception as exc:
                logger.warning("Failed to query default_branch for %s: %s; skipping branch sweep", repo, exc)
                continue
            if not default_branch:
                logger.warning("Empty default_branch for %s; skipping branch sweep", repo)
                continue

        try:
            branches = gh.list_branches(repo) if (gh is not None and hasattr(gh, "list_branches")) else []
        except Exception as exc:
            logger.warning("Failed to list branches for %s during janitor sweep: %s", repo, exc)
            continue

        for branch in branches:
            branch_name = str(branch.get("name") or "")
            if not branch_name:
                continue

            # Guard 1: Default / protected / prefix match
            if branch_name in ("main", "master", default_branch) or bool(branch.get("protected")):
                _log_skip(tenant.id, repo, branch_name, "protected_branch")
                continue

            if not branch_name.startswith(_FLOW_BRANCH_PREFIXES):
                _log_skip(tenant.id, repo, branch_name, "non_flow_branch")
                continue

            try:
                prs = (
                    gh.list_prs_for_branch(repo, branch_name)
                    if (gh is not None and hasattr(gh, "list_prs_for_branch"))
                    else []
                )
            except Exception as exc:
                logger.warning("Failed to list PRs for %s:%s: %s", repo, branch_name, exc)
                _log_skip(tenant.id, repo, branch_name, "github_api_error")
                continue

            # Guard 3: No live session on the tenant for that PR/issue or branch head
            candidate_issues: set[int] = set()
            parsed_issue = _parse_branch_issue_number(branch_name)
            if parsed_issue is not None:
                candidate_issues.add(parsed_issue)
            for pr_obj in prs:
                num = _safe_int(pr_obj.get("number"), 0)
                if num > 0:
                    candidate_issues.add(num)

            branch_sha = str((branch.get("commit") or {}).get("sha") or "")
            has_live_issue_session = any(
                store.issue_session_active(tenant.id, issue_num, now=current_time)
                for issue_num in candidate_issues
            )
            has_live_branch_session = any(
                s.get("tenant_id") == tenant.id
                and session_is_live(s, current_time)
                and (
                    (branch_sha and str(s.get("head_sha") or "") == branch_sha)
                    or str(s.get("branch") or "") == branch_name
                )
                for s in _list_open_sessions(store)
            )
            if has_live_issue_session or has_live_branch_session:
                _log_skip(tenant.id, repo, branch_name, "active_session")
                continue

            # Guard 2: PR closed/merged > 24h ago, OR no PR and head commit > 7d old
            if prs:
                if any(str(p.get("state") or "open").lower() == "open" for p in prs):
                    _log_skip(tenant.id, repo, branch_name, "open_pr")
                    continue

                closed_times = [
                    _parse_iso_dt(p.get("closed_at") or p.get("merged_at") or p.get("updated_at"))
                    for p in prs
                ]
                valid_closed = [dt for dt in closed_times if dt is not None]
                if not valid_closed:
                    _log_skip(tenant.id, repo, branch_name, "closed_within_grace_window")
                    continue
                latest_closed = max(valid_closed)
                if (current_time - latest_closed) <= timedelta(hours=pr_closed_grace_hours):
                    _log_skip(tenant.id, repo, branch_name, "closed_within_grace_window")
                    continue
                try:
                    commits = (
                        gh.compare_commits(repo, default_branch, branch_name)
                        if (gh is not None and hasattr(gh, "compare_commits"))
                        else []
                    )
                except Exception as exc:
                    logger.warning("Failed to compare commits for %s:%s: %s", repo, branch_name, exc)
                    _log_skip(tenant.id, repo, branch_name, "github_api_error")
                    continue
            else:
                try:
                    commits = (
                        gh.compare_commits(repo, default_branch, branch_name)
                        if (gh is not None and hasattr(gh, "compare_commits"))
                        else []
                    )
                except Exception as exc:
                    logger.warning("Failed to compare commits for %s:%s: %s", repo, branch_name, exc)
                    _log_skip(tenant.id, repo, branch_name, "github_api_error")
                    continue
                commit_dt = _extract_branch_commit_dt(branch, commits)
                if commit_dt is None or (current_time - commit_dt) <= timedelta(days=no_pr_grace_days):
                    _log_skip(tenant.id, repo, branch_name, "commit_too_recent")
                    continue

            # Guard 4: Every commit on the branch since merge-base with default_branch is by a known bot
            if not commits or not all(_is_bot_commit(c, allowed_bots) for c in commits):
                _log_skip(tenant.id, repo, branch_name, "human_commits_present")
                continue

            # All 4 safety guards hold: purge branch
            if not is_dry and gh is not None and hasattr(gh, "delete_branch"):
                try:
                    gh.delete_branch(repo, branch_name)
                except Exception as exc:
                    logger.warning("Failed to delete branch %s:%s: %s", repo, branch_name, exc)
                    _log_skip(tenant.id, repo, branch_name, "github_api_error")
                    continue

            decisions.append(
                BranchSweepDecision(
                    repo=repo,
                    tenant_id=tenant.id,
                    branch=branch_name,
                    action="purged",
                    reason="orphan_branch_purge",
                    dry_run=is_dry,
                )
            )
            store.record_run_event(
                None,
                "janitor.branch_purged",
                {
                    "repo": repo,
                    "tenant_id": tenant.id,
                    "branch": branch_name,
                    "reason": "orphan_branch_purge",
                    "dry_run": is_dry,
                },
            )

    return decisions


def run_janitor_once(
    ec2: Any,
    store: Any,
    gh: Optional[Any] = None,
    *,
    now: Optional[datetime] = None,
    include_branches: bool = True,
    boot_grace_minutes: float = 10.0,
    heartbeat_timeout_minutes: float = 10.0,
    default_max_minutes: int = 60,
    orphan_grace_minutes: float = 1.0,
    pr_closed_grace_hours: float = 24.0,
    no_pr_grace_days: float = 7.0,
    dry_run: Optional[bool] = None,
) -> dict[str, Any]:
    """Execute one Janitor pass (instance sweep + optional branch sweep)."""
    current_time = now or datetime.now(timezone.utc)
    is_dry = _is_dry_run(dry_run)

    inst_decisions = sweep_instances(
        ec2=ec2,
        store=store,
        gh=gh,
        now=current_time,
        boot_grace_minutes=boot_grace_minutes,
        heartbeat_timeout_minutes=heartbeat_timeout_minutes,
        default_max_minutes=default_max_minutes,
        orphan_grace_minutes=orphan_grace_minutes,
        dry_run=is_dry,
    )
    branch_decisions: list[BranchSweepDecision] = []
    if include_branches and gh is not None:
        branch_decisions = sweep_branches(
            store=store,
            gh=gh,
            now=current_time,
            pr_closed_grace_hours=pr_closed_grace_hours,
            no_pr_grace_days=no_pr_grace_days,
            dry_run=is_dry,
        )
    return {
        "instances": inst_decisions,
        "branches": branch_decisions,
        "dry_run": is_dry,
    }


def _default_ec2_client() -> Any:
    import boto3  # type: ignore[import-untyped]

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    return boto3.client("ec2", region_name=region)


def run_daemon(
    *,
    ec2: Optional[Any] = None,
    store: Optional[Any] = None,
    gh: Optional[Any] = None,
    stop_event: Optional[threading.Event] = None,
    instance_interval_seconds: float = 60.0,
    branch_interval_seconds: float = 3600.0,
    dry_run: Optional[bool] = None,
    clock: Callable[[], float] = time.monotonic,
    max_iterations: Optional[int] = None,
    install_signal_handlers: bool = True,
) -> dict[str, int]:
    """Run the continuous Janitor daemon loop (60s instance sweep, 60m branch sweep)."""
    stopper = stop_event or threading.Event()

    if install_signal_handlers and threading.current_thread() is threading.main_thread():
        def _handle_signal(signum: int, _frame: Any) -> None:
            logger.info("Janitor received signal %d, initiating graceful shutdown", signum)
            stopper.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    eff_ec2 = ec2 if ec2 is not None else _default_ec2_client()
    eff_store = store if store is not None else get_store()
    eff_gh = gh if gh is not None else get_github_client()

    instance_sweeps = 0
    branch_sweeps = 0
    last_branch_sweep_at: Optional[float] = None

    while not stopper.is_set():
        now_mono = clock()
        if stopper.is_set():
            break

        should_sweep_branches = (
            last_branch_sweep_at is None
            or (now_mono - last_branch_sweep_at) >= branch_interval_seconds
        )
        try:
            run_janitor_once(
                ec2=eff_ec2,
                store=eff_store,
                gh=eff_gh,
                include_branches=should_sweep_branches,
                dry_run=dry_run,
            )
            instance_sweeps += 1
            if should_sweep_branches:
                branch_sweeps += 1
                last_branch_sweep_at = now_mono
        except Exception as exc:
            logger.exception("Janitor sweep iteration failed: %s", exc)

        if max_iterations is not None and instance_sweeps >= max_iterations:
            break
        if clock is time.monotonic:
            stopper.wait(timeout=instance_interval_seconds)

    return {"instance_sweeps": instance_sweeps, "branch_sweeps": branch_sweeps}


def main() -> None:
    parser = argparse.ArgumentParser(description="Flow Engine Janitor service")
    parser.add_argument("--once", action="store_true", help="Run a single janitor sweep and exit")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log decisions without mutating EC2/GitHub")
    parser.add_argument("--instance-interval", type=float, default=60.0, help="Instance sweep interval in seconds")
    parser.add_argument("--branch-interval", type=float, default=3600.0, help="Branch sweep interval in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    dry_run = True if args.dry_run else None

    if args.once:
        run_janitor_once(
            ec2=_default_ec2_client(),
            store=get_store(),
            gh=get_github_client(),
            include_branches=True,
            dry_run=dry_run,
        )
        return

    run_daemon(
        instance_interval_seconds=args.instance_interval,
        branch_interval_seconds=args.branch_interval,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    main()
