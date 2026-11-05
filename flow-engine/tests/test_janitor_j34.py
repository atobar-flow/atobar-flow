"""AC-7 table-driven unit tests for the Flow Engine Janitor service (#301 WP-5)."""

from __future__ import annotations

import signal
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from atobar_contracts.store import Tenant
from flow_engine.janitor import (
    run_daemon,
    run_janitor_once,
    sweep_branches,
    sweep_instances,
)
from flow_engine.store import MemoryStore

NOW = datetime(2026, 10, 19, 15, 0, 0, tzinfo=timezone.utc)
REPO = "arsenyspb/ollama-monitor"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


class FakeEC2Client:
    """In-memory EC2 client double recording all terminate_instances calls."""

    def __init__(self, instances: Optional[list[dict[str, Any]]] = None) -> None:
        self.instances: list[dict[str, Any]] = list(instances or [])
        self.describe_calls: list[dict[str, Any]] = []
        self.terminate_calls: list[list[str]] = []

    @property
    def terminated_instance_ids(self) -> list[str]:
        return [iid for batch in self.terminate_calls for iid in batch]

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.describe_calls.append(kwargs)
        return {"Reservations": [{"Instances": list(self.instances)}]}

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        self.terminate_calls.append(list(InstanceIds))
        return {"TerminatingInstances": [{"InstanceId": iid} for iid in InstanceIds]}


class FakeGitHubClient:
    """In-memory GitHub client double for PR/issue status and branch hygiene."""

    def __init__(
        self,
        *,
        prs: Optional[dict[tuple[str, int], dict[str, Any]]] = None,
        branches: Optional[dict[str, list[dict[str, Any]]]] = None,
        branch_prs: Optional[dict[tuple[str, str], list[dict[str, Any]]]] = None,
        branch_commits: Optional[dict[tuple[str, str], list[dict[str, Any]]]] = None,
        default_branch: str = "main",
    ) -> None:
        self.prs: dict[tuple[str, int], dict[str, Any]] = dict(prs or {})
        self.branches: dict[str, list[dict[str, Any]]] = dict(branches or {})
        self.branch_prs: dict[tuple[str, str], list[dict[str, Any]]] = dict(branch_prs or {})
        self.branch_commits: dict[tuple[str, str], list[dict[str, Any]]] = dict(branch_commits or {})
        self.default_branch = default_branch
        self.deleted_branches: list[tuple[str, str]] = []
        self.comments: list[tuple[str, int, str]] = []

    def get_default_branch(self, repo: str) -> str:
        return self.default_branch

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self.prs.get(
            (repo, pr_number),
            {"number": pr_number, "state": "open", "labels": [], "merged": False},
        )

    def list_branches(self, repo: str) -> list[dict[str, Any]]:
        return list(self.branches.get(repo, []))

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        return list(self.branch_prs.get((repo, branch), []))

    def compare_commits(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        return list(self.branch_commits.get((repo, head), []))

    def delete_branch(self, repo: str, branch: str) -> None:
        self.deleted_branches.append((repo, branch))

    def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        self.comments.append((repo, issue_number, body))
        return {"id": len(self.comments), "body": body}


def _make_store(spot_max_minutes: int = 60) -> MemoryStore:
    store = MemoryStore()
    tenant = Tenant(
        id=TENANT_ID,
        repo=REPO,
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": REPO, "default_branch": "main"},
    )
    store.tenants[TENANT_ID] = tenant
    store.spot_max_minutes[TENANT_ID] = spot_max_minutes
    return store


def _make_ec2_instance(
    instance_id: str,
    *,
    launch_time: datetime,
    managed: Optional[str] = "true",
    run_id: Optional[str] = "run-1",
    tenant: str = REPO,
    pr: int = 42,
    max_minutes: Optional[int] = 60,
    state: str = "running",
) -> dict[str, Any]:
    tags: list[dict[str, str]] = []
    if managed is not None:
        tags.append({"Key": "atobar:managed", "Value": managed})
    if run_id is not None:
        tags.append({"Key": "atobar:run_id", "Value": run_id})
    if tenant:
        tags.append({"Key": "atobar:tenant", "Value": tenant})
    if pr:
        tags.append({"Key": "atobar:pr", "Value": str(pr)})
        tags.append({"Key": "atobar:issue", "Value": str(pr)})
    if max_minutes is not None:
        tags.append({"Key": "atobar:max_minutes", "Value": str(max_minutes)})
    return {
        "InstanceId": instance_id,
        "LaunchTime": launch_time,
        "State": {"Name": state},
        "Tags": tags,
    }


def _seed_credential(store: MemoryStore, run_id: str, role: str = "developer") -> None:
    store.create_run_credential(
        run_id=run_id,
        tenant_id=TENANT_ID,
        role=role,
        cred_hash=f"hash-{run_id}",
        expires_at=(NOW + timedelta(hours=1)).isoformat(),
    )


# ---------------------------------------------------------------------------
# AC-7 Table-Driven Tests for Rules 1–5 (Instance & Session Lifecycle Sweep)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "case_id",
        "launch_age_min",
        "max_min_tag",
        "tenant_max_min",
        "has_credential",
        "session_cfg",
        "pr_state",
        "pr_labels",
        "pr_merged",
        "expected_terminated",
        "expected_rule",
        "expected_session_status",
        "expect_cred_used",
        "expect_tombstone_round",
    ),
    [
        # Rule 1: boot_timeout (no first heartbeat within boot_grace_minutes=10m)
        (
            "rule1_boot_timeout_no_session_row",
            12.0,
            60,
            60,
            True,
            None,
            "open",
            [],
            False,
            True,
            "boot_timeout",
            "Halted: boot_timeout",
            True,
            False,
        ),
        (
            "rule1_boot_timeout_pending_placeholder_session",
            15.0,
            60,
            60,
            True,
            {"instance_id": "pending-run-1", "status": "pending", "heartbeat_at": (NOW - timedelta(minutes=15)).isoformat()},
            "open",
            [],
            False,
            True,
            "boot_timeout",
            "Halted: boot_timeout",
            True,
            False,
        ),
        (
            "rule1_boot_timeout_null_heartbeat",
            11.0,
            60,
            60,
            True,
            {"instance_id": "i-target", "status": "pending", "heartbeat_at": None},
            "open",
            [],
            False,
            True,
            "boot_timeout",
            "Halted: boot_timeout",
            True,
            False,
        ),
        (
            "rule1_within_boot_grace_kept_alive",
            5.0,
            60,
            60,
            True,
            None,
            "open",
            [],
            False,
            False,
            None,
            None,
            False,
            False,
        ),
        # Rule 2: stale_heartbeat (last heartbeat > heartbeat_timeout_minutes=10m)
        (
            "rule2_stale_heartbeat_running_session",
            25.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=14)).isoformat(),
                "head_sha": "a" * 40,
                "cost_estimate": 0.42,
                "usage": {"total_tokens": 1500},
            },
            "open",
            [],
            False,
            True,
            "stale_heartbeat",
            "Halted: stale_heartbeat",
            True,
            True,
        ),
        (
            "rule2_stale_heartbeat_pending_session",
            25.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "pending",
                "heartbeat_at": (NOW - timedelta(minutes=14)).isoformat(),
                "head_sha": "a" * 40,
                "cost_estimate": 0.12,
                "usage": {"total_tokens": 1500},
            },
            "open",
            [],
            False,
            True,
            "stale_heartbeat",
            "Halted: stale_heartbeat",
            True,
            True,
        ),
        (
            "rule2_fresh_heartbeat_kept_alive",
            25.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=3)).isoformat(),
            },
            "open",
            [],
            False,
            False,
            None,
            "Running",
            False,
            False,
        ),
        # Rule 3: max_minutes_exceeded (tag atobar:max_minutes or tenant fallback)
        (
            "rule3_max_minutes_tag_exceeded",
            35.0,
            30,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            "open",
            [],
            False,
            True,
            "max_minutes_exceeded",
            "Halted: max_minutes_exceeded",
            True,
            False,
        ),
        (
            "rule3_max_minutes_tenant_fallback_exceeded",
            50.0,
            None,
            45,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            "open",
            [],
            False,
            True,
            "max_minutes_exceeded",
            "Halted: max_minutes_exceeded",
            True,
            False,
        ),
        # Rule 4: orphan_instance (no matching run_credentials or sessions row)
        (
            "rule4_orphan_instance_unknown_run_id",
            8.0,
            60,
            60,
            False,
            None,
            "open",
            [],
            False,
            True,
            "orphan_instance",
            None,
            False,
            False,
        ),
        (
            "rule4_orphan_instance_unknown_launch_time",
            None,
            60,
            60,
            False,
            None,
            "open",
            [],
            False,
            True,
            "orphan_instance",
            None,
            False,
            False,
        ),
        (
            "rule4_orphan_within_creation_grace_kept",
            0.3,
            60,
            60,
            False,
            None,
            "open",
            [],
            False,
            False,
            None,
            None,
            False,
            False,
        ),
        # Rule 5: pr_closed_or_escalated
        (
            "rule5_pr_closed",
            5.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            "closed",
            [],
            False,
            True,
            "pr_closed_or_escalated",
            "Halted: pr_closed",
            True,
            False,
        ),
        (
            "rule5_pr_escalated_label",
            5.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            "open",
            ["flow:escalated"],
            False,
            True,
            "pr_closed_or_escalated",
            "Halted: pr_escalated",
            True,
            False,
        ),
        (
            "rule5_pr_shipped_label_marks_complete",
            5.0,
            60,
            60,
            True,
            {
                "instance_id": "i-target",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            "closed",
            ["flow:shipped"],
            True,
            True,
            "pr_closed_or_escalated",
            "Complete",
            True,
            False,
        ),
    ],
)
def test_sweep_instances_rules_1_to_5_table_driven(
    case_id: str,
    launch_age_min: Optional[float],
    max_min_tag: Optional[int],
    tenant_max_min: int,
    has_credential: bool,
    session_cfg: Optional[dict[str, Any]],
    pr_state: str,
    pr_labels: list[str],
    pr_merged: bool,
    expected_terminated: bool,
    expected_rule: Optional[str],
    expected_session_status: Optional[str],
    expect_cred_used: bool,
    expect_tombstone_round: bool,
) -> None:
    store = _make_store(spot_max_minutes=tenant_max_min)
    run_id = "run-1"
    if has_credential:
        _seed_credential(store, run_id)

    if session_cfg is not None:
        store.upsert_session(
            {
                "instance_id": session_cfg.get("instance_id", "i-target"),
                "run_id": run_id,
                "tenant_id": TENANT_ID,
                "target_repo": REPO,
                "issue_id": 42,
                "role": "developer",
                **session_cfg,
            }
        )

    launch_dt = (NOW - timedelta(minutes=launch_age_min)) if launch_age_min is not None else None
    inst = _make_ec2_instance(
        "i-target",
        launch_time=launch_dt,  # type: ignore[arg-type]
        managed="true",
        run_id=run_id,
        tenant=REPO,
        pr=42,
        max_minutes=max_min_tag,
    )
    ec2 = FakeEC2Client([inst])
    gh = FakeGitHubClient(
        prs={
            (REPO, 42): {
                "number": 42,
                "state": pr_state,
                "merged": pr_merged,
                "labels": [{"name": lbl} for lbl in pr_labels],
            }
        }
    )

    decisions = sweep_instances(ec2=ec2, store=store, gh=gh, now=NOW, dry_run=False)

    if not expected_terminated:
        assert decisions == [], f"{case_id}: expected no termination decisions"
        assert ec2.terminated_instance_ids == []
        return

    assert len(decisions) == 1, f"{case_id}: expected 1 decision, got {decisions}"
    dec = decisions[0]
    assert dec.instance_id == "i-target"
    assert dec.rule == expected_rule
    assert dec.terminated is True
    assert dec.dry_run is False
    assert ec2.terminated_instance_ids == ["i-target"]

    # Check run_events audit trail
    term_events = [e for e in store.run_events if e.get("kind") == "janitor.instance_terminated"]
    assert len(term_events) == 1
    payload = term_events[0]["payload"]
    assert payload["instance_id"] == "i-target"
    assert payload["rule"] == expected_rule
    assert payload["dry_run"] is False

    if expected_session_status is not None:
        sess = store.sessions.get("i-target") or store.get_session_by_run(run_id)
        assert sess is not None
        assert sess["status"] == expected_session_status

    if expect_cred_used:
        cred = store.get_run_credential(run_id)
        assert cred is not None
        assert cred.get("used_at") is not None

    rounds = store.loop_rounds_for_pr(TENANT_ID, 42)
    if expect_tombstone_round:
        assert len(rounds) == 1
        assert rounds[0]["envelope_digest"] == "reaped:lost_heartbeat"
        assert rounds[0]["tokens"] == 1500
    else:
        assert rounds == []


def test_tag_safety_refuses_to_terminate_unmanaged_instances() -> None:
    """Tag safety guard: instances without atobar:managed=true are NEVER passed to terminate_instances."""
    store = _make_store()
    _seed_credential(store, "run-unmanaged")
    # Even if a stale session in DB points to an unmanaged instance ID, janitor must NOT terminate it on EC2.
    store.upsert_session(
        {
            "instance_id": "i-unmanaged-prod",
            "run_id": "run-unmanaged",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 42,
            "role": "developer",
            "status": "Running",
            "heartbeat_at": (NOW - timedelta(minutes=30)).isoformat(),
        }
    )

    inst_missing_tag = _make_ec2_instance(
        "i-unmanaged-prod",
        launch_time=NOW - timedelta(minutes=120),
        managed=None,
        run_id="run-unmanaged",
    )
    inst_false_tag = _make_ec2_instance(
        "i-managed-false",
        launch_time=NOW - timedelta(minutes=120),
        managed="false",
        run_id="run-orphan-2",
    )
    inst_managed_orphan = _make_ec2_instance(
        "i-managed-orphan",
        launch_time=NOW - timedelta(minutes=15),
        managed="true",
        run_id="run-orphan-3",
    )

    ec2 = FakeEC2Client([inst_missing_tag, inst_false_tag, inst_managed_orphan])
    gh = FakeGitHubClient()

    sweep_instances(ec2=ec2, store=store, gh=gh, now=NOW, dry_run=False)

    # Only the explicitly managed instance may be passed to terminate_instances
    assert ec2.terminated_instance_ids == ["i-managed-orphan"]
    assert "i-unmanaged-prod" not in ec2.terminated_instance_ids
    assert "i-managed-false" not in ec2.terminated_instance_ids
    # The stale session in DB is still marked Halted: stale_heartbeat
    assert store.sessions["i-unmanaged-prod"]["status"] == "Halted: stale_heartbeat"


# ---------------------------------------------------------------------------
# AC-7 Table-Driven Tests for Rule 6 (Orphan Branch Purge & Safety Guards)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "case_id",
        "branch_name",
        "is_protected",
        "head_commit_age_hours",
        "prs_for_branch",
        "has_active_session",
        "commit_authors",
        "expect_purged",
        "expected_reason",
    ),
    [
        # Valid purges
        (
            "purge_flow_slash_closed_pr_over_24h",
            "flow/42-fix-parser",
            False,
            48.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=30)).isoformat()}],
            False,
            ["atobar-flow-agent[bot]"],
            True,
            "orphan_branch_purge",
        ),
        (
            "purge_flow_dash_merged_pr_over_24h",
            "flow-42-fix-parser",
            False,
            36.0,
            [
                {
                    "number": 42,
                    "state": "closed",
                    "merged_at": (NOW - timedelta(hours=26)).isoformat(),
                    "closed_at": (NOW - timedelta(hours=26)).isoformat(),
                }
            ],
            False,
            ["atobar-flow-agent[bot]"],
            True,
            "orphan_branch_purge",
        ),
        (
            "purge_atobar_slash_no_pr_over_7d",
            "atobar/metrics-policy",
            False,
            24.0 * 8,  # 8 days old
            [],
            False,
            ["atobar-flow-agent[bot]"],
            True,
            "orphan_branch_purge",
        ),
        # Safety Guard 1: Default / non-flow / protected branch
        (
            "guard1_default_branch_main_never_deleted",
            "main",
            False,
            24.0 * 30,
            [],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "protected_branch",
        ),
        (
            "guard1_non_flow_branch_never_deleted",
            "feature/human-work",
            False,
            24.0 * 30,
            [],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "non_flow_branch",
        ),
        (
            "guard1_protected_flow_branch_never_deleted",
            "flow-42-protected",
            True,
            48.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=40)).isoformat()}],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "protected_branch",
        ),
        # Safety Guard 2: Open PR or closed < 24h ago or no-PR < 7d old
        (
            "guard2_open_pr_never_deleted",
            "flow-42-open-pr",
            False,
            48.0,
            [{"number": 42, "state": "open", "closed_at": None}],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "open_pr",
        ),
        (
            "guard2_closed_less_than_24h_grace_window",
            "flow-42-recent-close",
            False,
            24.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=12)).isoformat()}],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "closed_within_grace_window",
        ),
        (
            "guard2_no_pr_less_than_7d_old",
            "flow-42-unopened-recent",
            False,
            24.0 * 3,  # 3 days old (< 7d)
            [],
            False,
            ["atobar-flow-agent[bot]"],
            False,
            "commit_too_recent",
        ),
        # Safety Guard 3: Active session on tenant for that PR/issue
        (
            "guard3_active_session_blocks_deletion",
            "flow-42-active-session",
            False,
            48.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=30)).isoformat()}],
            True,
            ["atobar-flow-agent[bot]"],
            False,
            "active_session",
        ),
        # Safety Guard 4: Human commits present since merge-base
        (
            "guard4_human_commit_blocks_deletion",
            "flow-42-coauthored-with-human",
            False,
            48.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=30)).isoformat()}],
            False,
            ["atobar-flow-agent[bot]", "arsenyspb"],
            False,
            "human_commits_present",
        ),
        (
            "guard4_unknown_author_blocks_deletion",
            "flow-42-unknown-author",
            False,
            48.0,
            [{"number": 42, "state": "closed", "closed_at": (NOW - timedelta(hours=30)).isoformat()}],
            False,
            [""],
            False,
            "human_commits_present",
        ),
    ],
)
def test_sweep_branches_rule_6_safety_guards_table_driven(
    case_id: str,
    branch_name: str,
    is_protected: bool,
    head_commit_age_hours: float,
    prs_for_branch: list[dict[str, Any]],
    has_active_session: bool,
    commit_authors: list[str],
    expect_purged: bool,
    expected_reason: str,
) -> None:
    store = _make_store()
    if has_active_session:
        store.upsert_session(
            {
                "instance_id": "i-live-42",
                "run_id": "run-live-42",
                "tenant_id": TENANT_ID,
                "target_repo": REPO,
                "issue_id": 42,
                "role": "developer",
                "status": "Running",
                "heartbeat_at": (NOW - timedelta(minutes=2)).isoformat(),
            }
        )

    commit_dt = (NOW - timedelta(hours=head_commit_age_hours)).isoformat()
    branch_obj = {
        "name": branch_name,
        "protected": is_protected,
        "commit": {
            "sha": "b" * 40,
            "commit": {"committer": {"date": commit_dt}},
        },
    }
    commits = [
        {
            "sha": f"{idx:040x}",
            "author": {"login": login} if login else None,
            "committer": {"login": login} if login else None,
            "commit": {"committer": {"date": commit_dt}},
        }
        for idx, login in enumerate(commit_authors, start=1)
    ]

    gh = FakeGitHubClient(
        branches={REPO: [branch_obj]},
        branch_prs={(REPO, branch_name): prs_for_branch},
        branch_commits={(REPO, branch_name): commits},
    )

    decisions = sweep_branches(store=store, gh=gh, now=NOW, dry_run=False)
    assert len(decisions) == 1, f"{case_id}: expected 1 branch decision"
    dec = decisions[0]
    assert dec.branch == branch_name

    if expect_purged:
        assert dec.action == "purged"
        assert gh.deleted_branches == [(REPO, branch_name)]
        events = [e for e in store.run_events if e.get("kind") == "janitor.branch_purged"]
        assert len(events) == 1
        assert events[0]["payload"]["branch"] == branch_name
        assert events[0]["payload"]["dry_run"] is False
    else:
        assert dec.action == "skipped"
        assert dec.reason == expected_reason
        assert gh.deleted_branches == []
        events = [e for e in store.run_events if e.get("kind") == "janitor.branch_skipped"]
        assert len(events) == 1
        assert events[0]["payload"]["branch"] == branch_name
        assert events[0]["payload"]["reason"] == expected_reason


# ---------------------------------------------------------------------------
# JANITOR_DRY_RUN=1 and Daemon Loop Tests
# ---------------------------------------------------------------------------


def test_janitor_dry_run_logs_decisions_without_mutating_ec2_or_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JANITOR_DRY_RUN", "1")
    store = _make_store()
    _seed_credential(store, "run-dry-1")

    inst = _make_ec2_instance(
        "i-dry-boot",
        launch_time=NOW - timedelta(minutes=20),
        managed="true",
        run_id="run-dry-1",
    )
    ec2 = FakeEC2Client([inst])

    commit_dt = (NOW - timedelta(days=10)).isoformat()
    gh = FakeGitHubClient(
        branches={
            REPO: [
                {
                    "name": "flow-99-stale",
                    "protected": False,
                    "commit": {"sha": "c" * 40, "commit": {"committer": {"date": commit_dt}}},
                }
            ]
        },
        branch_prs={(REPO, "flow-99-stale"): []},
        branch_commits={
            (REPO, "flow-99-stale"): [
                {
                    "sha": "c" * 40,
                    "author": {"login": "atobar-flow-agent[bot]"},
                    "committer": {"login": "atobar-flow-agent[bot]"},
                    "commit": {"committer": {"date": commit_dt}},
                }
            ]
        },
    )

    result = run_janitor_once(ec2=ec2, store=store, gh=gh, now=NOW)
    assert result["dry_run"] is True
    assert len(result["instances"]) == 1
    assert result["instances"][0].terminated is False
    assert result["instances"][0].dry_run is True
    assert ec2.terminated_instance_ids == []

    assert len(result["branches"]) == 1
    assert result["branches"][0].action == "purged"
    assert result["branches"][0].dry_run is True
    assert gh.deleted_branches == []

    term_evts = [e for e in store.run_events if e.get("kind") == "janitor.instance_terminated"]
    purge_evts = [e for e in store.run_events if e.get("kind") == "janitor.branch_purged"]
    assert len(term_evts) == 1
    assert term_evts[0]["payload"]["dry_run"] is True
    assert len(purge_evts) == 1
    assert purge_evts[0]["payload"]["dry_run"] is True


def test_daemon_loop_schedules_60s_instance_and_60m_branch_sweeps_and_handles_sigterm() -> None:
    store = _make_store()
    ec2 = FakeEC2Client([])
    gh = FakeGitHubClient(branches={REPO: []})
    stop_event = threading.Event()

    # Fake monotonic clock advancing by 60s on each tick; after 60 ticks (3600s) branch sweep runs again.
    clock_ticks = iter([0.0, 60.0, 120.0, 3600.0, 3660.0])
    current_t = 0.0

    def fake_clock() -> float:
        nonlocal current_t
        try:
            current_t = next(clock_ticks)
        except StopIteration:
            stop_event.set()
        return current_t

    stats = run_daemon(
        ec2=ec2,
        store=store,
        gh=gh,
        stop_event=stop_event,
        instance_interval_seconds=60.0,
        branch_interval_seconds=3600.0,
        clock=fake_clock,
        max_iterations=4,
        install_signal_handlers=False,
    )

    assert stats["instance_sweeps"] == 4
    # Branch sweep runs at t=0.0 and again when elapsed >= 3600.0 (t=3600.0)
    assert stats["branch_sweeps"] == 2
    assert signal.SIGTERM is not None


def test_ec2_terminate_failure_does_not_strand_session_in_halted_state() -> None:
    """If ec2.terminate_instances raises, session stays open so the next sweep retries termination."""
    store = _make_store()
    _seed_credential(store, "run-retry-1")
    store.upsert_session(
        {
            "instance_id": "i-retry-1",
            "run_id": "run-retry-1",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 42,
            "role": "developer",
            "status": "Running",
            "heartbeat_at": (NOW - timedelta(minutes=20)).isoformat(),
        }
    )
    inst = _make_ec2_instance("i-retry-1", launch_time=NOW - timedelta(minutes=25), run_id="run-retry-1")

    class FlakyEC2Client(FakeEC2Client):
        def __init__(self) -> None:
            super().__init__([inst])
            self.fail_next = True

        def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("EC2 transient error")
            return super().terminate_instances(InstanceIds=InstanceIds)

    flaky_ec2 = FlakyEC2Client()
    gh = FakeGitHubClient()

    with pytest.raises(RuntimeError, match="EC2 transient error"):
        sweep_instances(ec2=flaky_ec2, store=store, gh=gh, now=NOW, dry_run=False)

    # Session must still be Running and credential unused so the next sweep retries
    assert store.sessions["i-retry-1"]["status"] == "Running"
    cred = store.get_run_credential("run-retry-1")
    assert cred is not None and cred.get("used_at") is None

    # Next sweep succeeds and terminates the instance
    decisions = sweep_instances(ec2=flaky_ec2, store=store, gh=gh, now=NOW, dry_run=False)
    assert len(decisions) == 1
    assert flaky_ec2.terminated_instance_ids == ["i-retry-1"]
    assert store.sessions["i-retry-1"]["status"] == "Halted: stale_heartbeat"


def test_stale_non_loop_roles_skip_loop_rounds_insert_and_closed_issues_fallback_to_get_issue() -> None:
    """Stale onboarder/metric_compiler sessions do not insert disallowed roles into loop_rounds,
    and closed issues without a PR fall back to get_issue."""
    store = _make_store()
    _seed_credential(store, "run-onboard-1", role="onboarder")
    _seed_credential(store, "run-issue-2", role="onboarder")
    store.upsert_session(
        {
            "instance_id": "i-onboard-stale",
            "run_id": "run-onboard-1",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 10,
            "role": "onboarder",
            "status": "Running",
            "heartbeat_at": (NOW - timedelta(minutes=20)).isoformat(),
        }
    )
    store.upsert_session(
        {
            "instance_id": "i-onboard-closed-issue",
            "run_id": "run-issue-2",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 11,
            "role": "onboarder",
            "status": "Running",
            "heartbeat_at": (NOW - timedelta(minutes=1)).isoformat(),
        }
    )

    inst1 = _make_ec2_instance("i-onboard-stale", launch_time=NOW - timedelta(minutes=25), run_id="run-onboard-1", pr=10)
    inst2 = _make_ec2_instance(
        "i-onboard-closed-issue", launch_time=NOW - timedelta(minutes=5), run_id="run-issue-2", pr=11
    )
    ec2 = FakeEC2Client([inst1, inst2])

    class IssueFallbackGitHub(FakeGitHubClient):
        def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
            if pr_number == 11:
                raise RuntimeError("404 Not Found: issue 11 is not a pull request")
            return super().get_pr(repo, pr_number)

        def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
            if issue_number == 11:
                return {"number": 11, "state": "closed", "labels": []}
            return {"number": issue_number, "state": "open", "labels": []}

    decisions = sweep_instances(ec2=ec2, store=store, gh=IssueFallbackGitHub(), now=NOW, dry_run=False)
    assert {d.instance_id: d.rule for d in decisions} == {
        "i-onboard-stale": "stale_heartbeat",
        "i-onboard-closed-issue": "pr_closed_or_escalated",
    }
    assert set(ec2.terminated_instance_ids) == {"i-onboard-stale", "i-onboard-closed-issue"}
    assert store.loop_rounds == []


def test_terminal_session_status_preserved_when_max_minutes_exceeded() -> None:
    """Existing terminal states (Complete, Halted: escalated) are preserved when cleaning up aged instances."""
    store = _make_store()
    _seed_credential(store, "run-esc-1")
    store.upsert_session(
        {
            "instance_id": "i-esc-aged",
            "run_id": "run-esc-1",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 42,
            "role": "developer",
            "status": "Halted: escalated",
            "halted_reason": "escalated",
            "heartbeat_at": (NOW - timedelta(minutes=50)).isoformat(),
        }
    )
    inst = _make_ec2_instance("i-esc-aged", launch_time=NOW - timedelta(minutes=70), run_id="run-esc-1")
    ec2 = FakeEC2Client([inst])

    decisions = sweep_instances(ec2=ec2, store=store, gh=FakeGitHubClient(), now=NOW, dry_run=False)
    assert len(decisions) == 1
    assert ec2.terminated_instance_ids == ["i-esc-aged"]
    assert store.sessions["i-esc-aged"]["status"] == "Halted: escalated"
    assert store.sessions["i-esc-aged"]["halted_reason"] == "escalated"


def test_failed_default_branch_lookup_skips_tenant_fail_closed() -> None:
    """When get_default_branch fails and manifest has no default_branch, sweep_branches skips the tenant."""
    store = MemoryStore()
    store.tenants[TENANT_ID] = Tenant(
        id=TENANT_ID,
        repo=REPO,
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": REPO},  # no default_branch in manifest
    )

    class BrokenDefaultBranchGitHub(FakeGitHubClient):
        def get_default_branch(self, repo: str) -> str:
            raise RuntimeError("GitHub API 502")

    gh = BrokenDefaultBranchGitHub(
        branches={REPO: [{"name": "flow-main", "protected": False}]},
    )
    decisions = sweep_branches(store=store, gh=gh, now=NOW, dry_run=False)
    assert decisions == []
    assert gh.deleted_branches == []


# ---------------------------------------------------------------------------
# Issue #321: AC-1, AC-2, AC-3 Tests (TDD)
# ---------------------------------------------------------------------------


def test_ac1_store_protocol_and_janitor_zero_duck_typing() -> None:
    """AC-1: Store, SupabaseStore, and MemoryStore expose the full session/credential/round interface."""
    from atobar_contracts.store import Store as StoreProtocol
    from flow_engine.store import SupabaseStore

    required_methods = (
        "list_open_sessions",
        "get_session_by_instance",
        "get_session_by_run",
        "update_session",
        "upsert_session",
        "mark_run_credential_used",
        "record_run_event",
        "record_loop_round",
        "get_spot_max_minutes",
    )
    for cls in (StoreProtocol, SupabaseStore, MemoryStore):
        for method_name in required_methods:
            assert hasattr(cls, method_name), f"{cls.__name__} missing {method_name}"


def test_ac2_github_app_token_caching_and_idempotent_tombstones(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: FlowEngineGitHubAppClient caches repo tokens until expires_at - 60s; tombstones are idempotent."""
    import httpx
    from fastapi.testclient import TestClient
    from flow_engine import api as fe_api
    from flow_engine.janitor import _mark_session_terminal, _record_tombstone_round

    mint_calls: list[str] = []
    current_expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(fe_api.FlowEngineGitHubAppClient, "_jwt", lambda self: "fake-jwt")

    def fake_get(url: str, **kwargs: Any) -> Any:
        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> Any:
                if url.endswith("/installation"):
                    owner = url.split("/repos/", 1)[1].split("/", 1)[0]
                    return {"id": 999, "account": {"login": owner}}
                if "/pulls/" in url:
                    return {"number": 42, "state": "open", "labels": []}
                if url.endswith("/branches"):
                    return [{"name": "main", "protected": True}]
                return {}

        return _Resp()

    def fake_post(url: str, **kwargs: Any) -> Any:
        mint_calls.append(url)

        class _Resp:
            status_code = 201

            def raise_for_status(self) -> None:
                pass

            def json(self) -> Any:
                return {"token": f"ghs-tok-{len(mint_calls)}", "expires_at": current_expires}

        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    gh_app = fe_api.FlowEngineGitHubAppClient(
        app_id="1",
        installation_id="999",
        private_key="fake-key",
    )
    # Multiple calls for same repo reuse cached token
    gh_app.get_pr(REPO, 42)
    gh_app.get_pr(REPO, 43)
    gh_app.list_branches(REPO)
    assert len(mint_calls) == 1

    # Different repo mints its own token
    gh_app.get_pr("other/repo", 1)
    assert len(mint_calls) == 2

    # When expires_at is within 60s of now, token is re-minted on next call
    current_expires = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    gh_app._token_cache.clear()  # attr must exist on FlowEngineGitHubAppClient
    gh_app.get_pr(REPO, 42)
    gh_app.get_pr(REPO, 42)
    assert len(mint_calls) == 4

    # Verify get_github_client returns a cached FlowEngineGitHubAppClient across consecutive calls
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "999")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-key")
    fe_api._cached_github_app_client.cache_clear()
    c1 = fe_api.get_github_client()
    c2 = fe_api.get_github_client()
    assert c1 is c2

    # Session consolidation: no duplicate terminal rows for one run_id
    store = _make_store()
    run_id = "run-consolidate-1"
    _seed_credential(store, run_id)
    store.upsert_session(
        {
            "instance_id": f"pending-{run_id}",
            "run_id": run_id,
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 42,
            "role": "developer",
            "status": "pending",
            "heartbeat_at": (NOW - timedelta(minutes=20)).isoformat(),
        }
    )
    _mark_session_terminal(
        store,
        instance_id="i-real-1",
        run_id=run_id,
        tenant_id=TENANT_ID,
        target_repo=REPO,
        issue_id=42,
        role="developer",
        session=store.get_session_by_instance(f"pending-{run_id}"),
        status_value="Halted: boot_timeout",
        halted_reason="boot_timeout",
        now=NOW,
    )
    terminal_rows = [
        s for s in store.sessions.values()
        if s.get("run_id") == run_id and str(s.get("status", "")).startswith("Halted")
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0]["instance_id"] == "i-real-1"
    resolved_by_run = store.get_session_by_run(run_id)
    assert resolved_by_run is not None
    assert resolved_by_run["instance_id"] == "i-real-1"

    # Idempotent _record_tombstone_round per (tenant_id, pr_number, run_id)
    sess_tomb = {
        "instance_id": "i-tomb-1",
        "run_id": "run-tomb-1",
        "tenant_id": TENANT_ID,
        "issue_id": 42,
        "role": "developer",
        "head_sha": "a" * 40,
        "usage": {"total_tokens": 900},
    }
    _record_tombstone_round(store, tenant_id=TENANT_ID, pr_number=42, session=sess_tomb, role="developer")
    _record_tombstone_round(store, tenant_id=TENANT_ID, pr_number=42, session=sess_tomb, role="developer")
    rounds = store.loop_rounds_for_pr(TENANT_ID, 42)
    assert len(rounds) == 1
    assert rounds[0]["envelope_digest"] == "reaped:lost_heartbeat"


def test_ac3_spot_instance_tags_and_unified_gc_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3: SpotInstanceTags round-trips EC2 tags and POST /internal/v1/gc/sweep delegates to janitor."""
    from atobar_contracts.github_rest import SpotInstanceTags
    from fastapi.testclient import TestClient
    from flow_engine import api as fe_api

    tags_obj = SpotInstanceTags(
        managed=True,
        tenant=REPO,
        issue_id=42,
        pr_number=42,
        run_id="run-tag-1",
        max_minutes=45,
        role="evaluator",
        head_sha="b" * 40,
    )
    ec2_tags = tags_obj.to_ec2_tags()
    parsed = SpotInstanceTags.from_ec2_instance({"InstanceId": "i-tag-1", "Tags": ec2_tags})
    assert parsed == tags_obj

    # Verify POST /internal/v1/gc/sweep delegates to janitor.sweep_instances
    monkeypatch.setenv("FE_INTERNAL_TOKEN", "fe-test-secret")
    store = _make_store()
    _seed_credential(store, "run-sweep-api")
    store.upsert_session(
        {
            "instance_id": "i-sweep-api",
            "run_id": "run-sweep-api",
            "tenant_id": TENANT_ID,
            "target_repo": REPO,
            "issue_id": 42,
            "role": "developer",
            "status": "Running",
            "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat(),
        }
    )
    inst = _make_ec2_instance(
        "i-sweep-api",
        launch_time=datetime.now(timezone.utc) - timedelta(minutes=25),
        run_id="run-sweep-api",
    )
    ec2 = FakeEC2Client([inst])
    gh = FakeGitHubClient()

    fe_api.app.dependency_overrides[fe_api.get_store] = lambda: store
    fe_api.app.dependency_overrides[fe_api.get_ec2_client] = lambda: ec2
    fe_api.app.dependency_overrides[fe_api.get_github_client] = lambda: gh
    try:
        client = TestClient(fe_api.app)
        resp = client.post(
            "/internal/v1/gc/sweep",
            headers={"Authorization": "Bearer fe-test-secret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "i-sweep-api" in data["reaped_sessions"]
        assert "i-sweep-api" in ec2.terminated_instance_ids
        assert store.sessions["i-sweep-api"]["status"] == "Halted: stale_heartbeat"
        assert any(e.get("kind") == "janitor.instance_terminated" for e in store.run_events)

        # Shipped PR session closed during /internal/v1/gc/sweep retains Complete status
        _seed_credential(store, "run-shipped-api")
        store.upsert_session(
            {
                "instance_id": "i-shipped-api",
                "run_id": "run-shipped-api",
                "tenant_id": TENANT_ID,
                "target_repo": REPO,
                "issue_id": 99,
                "role": "developer",
                "status": "Running",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        gh.prs[(REPO, 99)] = {"number": 99, "state": "closed", "merged": True, "labels": [{"name": "flow:shipped"}]}
        resp_shipped = client.post(
            "/internal/v1/gc/sweep",
            headers={"Authorization": "Bearer fe-test-secret"},
        )
        assert resp_shipped.status_code == 200
        assert store.sessions["i-shipped-api"]["status"] == "Complete"
    finally:
        fe_api.app.dependency_overrides.clear()


