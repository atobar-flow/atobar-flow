"""Instance lifecycle garbage collection and stale-session reaper (delegates to flow_engine.janitor)."""

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from atobar_contracts.github_rest import RealGitHubClient, SpotInstanceTags
from flow_engine.janitor import sweep_instances
from flow_engine.store import MemoryStore, SupabaseStore, get_store

logger = logging.getLogger(__name__)


@dataclass
class GCTarget:
    """Spot instance identified for garbage collection."""

    instance_id: str
    role: str
    tenant: str
    pr: int
    launch_time: datetime
    age_minutes: float
    reason: str


class _StaticEC2Adapter:
    """Thin EC2 adapter so evaluate_gc_candidates/run_gc delegate evaluation to janitor.sweep_instances."""

    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        return {"Reservations": [{"Instances": self._instances}]}

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        return {"TerminatingInstances": [{"InstanceId": iid} for iid in InstanceIds]}


def evaluate_gc_candidates(
    instances: list[dict[str, Any]],
    gh: Any,
    max_wall_clock_minutes: float = 60.0,
    now: Optional[datetime] = None,
) -> list[GCTarget]:
    """Evaluates running spot instances against termination criteria via janitor.sweep_instances."""
    current_time = now or datetime.now(timezone.utc)
    temp_store = MemoryStore()
    prepared_instances: list[dict[str, Any]] = []
    meta_by_id: dict[str, tuple[str, str, int, datetime, float]] = {}

    for inst in instances:
        instance_id = str(inst.get("InstanceId") or "")
        launch_time = inst.get("LaunchTime")
        if not launch_time or not instance_id:
            continue
        if isinstance(launch_time, datetime) and launch_time.tzinfo is None:
            launch_time = launch_time.replace(tzinfo=timezone.utc)
        age_minutes = (current_time - launch_time).total_seconds() / 60.0

        parsed = SpotInstanceTags.from_ec2_instance(inst)
        if not parsed.tenant and not parsed.managed:
            continue

        is_timed_out = age_minutes >= max_wall_clock_minutes
        eff_pr = 0 if is_timed_out else parsed.pr_number
        eff_max = max(1, int(age_minutes) - 1) if is_timed_out else max(1, int(max_wall_clock_minutes))
        inst_tags = SpotInstanceTags(
            managed=True,
            tenant=parsed.tenant,
            issue_id=eff_pr,
            pr_number=eff_pr,
            run_id=parsed.run_id or f"gc-{instance_id}",
            max_minutes=eff_max,
            role=parsed.role,
            head_sha=parsed.head_sha,
        ).to_ec2_tags()
        inst_copy = {**inst, "LaunchTime": launch_time, "Tags": inst_tags}
        prepared_instances.append(inst_copy)
        meta_by_id[instance_id] = (parsed.role, parsed.tenant, parsed.pr_number, launch_time, age_minutes)

        temp_store.upsert_session(
            {
                "instance_id": instance_id,
                "run_id": parsed.run_id or f"gc-{instance_id}",
                "tenant_id": "",
                "target_repo": parsed.tenant,
                "issue_id": eff_pr,
                "role": parsed.role,
                "status": "Running",
                "heartbeat_at": current_time.isoformat(),
            }
        )

    decisions = sweep_instances(
        ec2=_StaticEC2Adapter(prepared_instances),
        store=temp_store,
        gh=gh,
        now=current_time,
        boot_grace_minutes=max_wall_clock_minutes + 1000.0,
        heartbeat_timeout_minutes=max_wall_clock_minutes + 1000.0,
        default_max_minutes=max(1, int(max_wall_clock_minutes)),
        orphan_grace_minutes=max_wall_clock_minutes + 1000.0,
        dry_run=True,
    )

    candidates: list[GCTarget] = []
    for dec in decisions:
        meta = meta_by_id.get(dec.instance_id)
        if meta is None:
            continue
        role, orig_tenant, orig_pr, launch_time, age_minutes = meta
        reason_msg = (
            f"timeout: exceeded max_wall_clock_minutes ({age_minutes:.1f}m >= {max_wall_clock_minutes}m)"
            if dec.rule == "max_minutes_exceeded"
            else dec.reason
        )
        candidates.append(
            GCTarget(
                instance_id=dec.instance_id,
                role=role,
                tenant=orig_tenant,
                pr=orig_pr,
                launch_time=launch_time,
                age_minutes=age_minutes,
                reason=reason_msg,
            )
        )
    return candidates


def run_gc(
    ec2: Any,
    gh: Any,
    max_wall_clock_minutes: float = 60.0,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> list[GCTarget]:
    """Queries running instances, determines GC candidates via janitor.sweep_instances, and terminates them."""
    filters = [
        {"Name": "tag-key", "Values": ["Repository"]},
        {"Name": "instance-state-name", "Values": ["running", "pending"]},
    ]
    resp = ec2.describe_instances(Filters=filters)
    instances: list[dict[str, Any]] = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            state = inst.get("State", {}).get("Name", "")
            if state in ("running", "pending"):
                instances.append(inst)

    targets = evaluate_gc_candidates(
        instances=instances,
        gh=gh,
        max_wall_clock_minutes=max_wall_clock_minutes,
        now=now,
    )

    if dry_run:
        logger.info("[DRY RUN] Would terminate %d instances:", len(targets))
        for t in targets:
            logger.info(
                "  %s (%s, %s#%d, age %.1fm): %s",
                t.instance_id,
                t.role,
                t.tenant,
                t.pr,
                t.age_minutes,
                t.reason,
            )
        return targets

    if targets:
        instance_ids = [t.instance_id for t in targets]
        logger.info("Terminating %d instances: %s", len(instance_ids), instance_ids)
        ec2.terminate_instances(InstanceIds=instance_ids)

    return targets


def reap_stale_sessions(
    store: Any,
    heartbeat_timeout_minutes: float = 10.0,
    now: Optional[datetime] = None,
    gh: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Delegates stale-session reaping to janitor.sweep_instances and emits tombstone comments."""
    current_time = now or datetime.now(timezone.utc)

    if not isinstance(store, (MemoryStore, SupabaseStore)) and not list(store.list_open_sessions()):
        try:
            fallback_open = list(store.active_sessions(now=current_time))
        except TypeError:
            fallback_open = list(store.active_sessions())
        store.list_open_sessions = lambda: fallback_open
        store.get_run_credential = lambda _rid: None
        store.tenant_by_repo = lambda _repo: None
        store.tenant_by_id = lambda _tid: None

    pre_sweep_by_inst = {
        str(s.get("instance_id") or ""): dict(s)
        for s in store.list_open_sessions()
        if s.get("instance_id")
    }

    decisions = sweep_instances(
        ec2=None,
        store=store,
        gh=None,
        now=current_time,
        boot_grace_minutes=heartbeat_timeout_minutes,
        heartbeat_timeout_minutes=heartbeat_timeout_minutes,
        dry_run=False,
    )

    reaped: list[dict[str, Any]] = []
    for dec in decisions:
        if dec.dry_run or dec.rule not in ("stale_heartbeat", "boot_timeout"):
            continue
        instance_id = dec.instance_id
        session = store.get_session_by_instance(instance_id)
        if not isinstance(session, dict):
            session = pre_sweep_by_inst.get(instance_id, {"instance_id": instance_id})

        store.update_session(
            instance_id,
            {
                "status": "Halted",
                "halted_reason": "stale/reaped",
            },
        )
        session["status"] = "Halted"
        session["halted_reason"] = "stale/reaped"

        hb_str = session.get("heartbeat_at") or session.get("started_at")
        elapsed_sec = 0
        if hb_str:
            try:
                hb_time = datetime.fromisoformat(str(hb_str).replace("Z", "+00:00"))
                if hb_time.tzinfo is None:
                    hb_time = hb_time.replace(tzinfo=timezone.utc)
                elapsed_sec = int((current_time - hb_time).total_seconds())
            except Exception:
                elapsed_sec = 0

        agent_runtime = session.get("agent_runtime", "nanobot")
        iterations = session.get("iterations", 0)
        target_repo = str(session.get("target_repo") or dec.tenant or "")
        pr_number = int(session.get("issue_id") or dec.pr or 0)

        tombstone_body = (
            f"🌊 **flow Event Bus:** session terminated: last heartbeat {elapsed_sec}s ago "
            f"(agent_runtime={agent_runtime}, iterations={iterations})"
        )
        gh_client = gh if gh is not None else getattr(store, "gh", None)
        if gh_client is None:
            token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
            if token:
                gh_client = RealGitHubClient(token=token)
        if gh_client and target_repo and pr_number:
            try:
                gh_client.create_comment(target_repo, pr_number, tombstone_body)
            except Exception:
                pass

        reaped.append(session)

    return reaped


def main() -> None:
    parser = argparse.ArgumentParser(description="flow-engine GC and stale-session reaper")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without terminating")
    parser.add_argument("--max-wall-clock", type=float, default=60.0, help="Max allowed instance minutes")
    parser.add_argument("--heartbeat-timeout", type=float, default=10.0, help="Heartbeat loss timeout in minutes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import boto3

    ec2 = boto3.client("ec2")
    token = os.environ.get("GITHUB_TOKEN", "")
    gh = RealGitHubClient(token=token) if token else None

    targets = run_gc(
        ec2=ec2,
        gh=gh,
        max_wall_clock_minutes=args.max_wall_clock,
        dry_run=args.dry_run,
    )
    print(f"GC evaluated: {len(targets)} candidate(s) {'(dry-run)' if args.dry_run else 'terminated'}.")

    store = get_store()
    reaped = reap_stale_sessions(
        store=store,
        heartbeat_timeout_minutes=args.heartbeat_timeout,
    )
    print(f"Reaper evaluated: {len(reaped)} stale session(s) reaped.")


if __name__ == "__main__":
    main()
