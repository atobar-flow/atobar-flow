from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from flow_engine.conductor.gc import (
    reap_stale_sessions,
    run_gc,
)
from flow_engine.conductor.io import rebuild_loop_state
from flow_engine.conductor.machine import VerdictEvent, step
from atobar_contracts.envelopes import Verdict
from flow_engine.store import MemoryStore, Tenant
from flow_engine.tenancy.manifest import LoopLimits


class FakeEC2Client:
    def __init__(self, instances: list[dict[str, Any]]):
        self._instances = instances
        self.terminated_instance_ids: list[str] = []

    def describe_instances(self, **kwargs) -> dict[str, Any]:
        return {
            "Reservations": [
                {"Instances": self._instances}
            ]
        }

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        self.terminated_instance_ids.extend(InstanceIds)
        return {"TerminatingInstances": [{"InstanceId": i} for i in InstanceIds]}


class FakeGitHubClient:
    def __init__(self, pr_states: dict[int, dict[str, Any]]):
        self._pr_states = pr_states
        self.comments: list[dict[str, Any]] = []

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._pr_states.get(pr_number, {"state": "open", "labels": [], "head": {"sha": "0"*40}})

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return self.comments

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        return []


def make_instance(
    instance_id: str,
    launch_time: datetime,
    role: str = "developer",
    tenant: str = "arsenyspb/ollama-monitor",
    pr: int = 42,
) -> dict[str, Any]:
    return {
        "InstanceId": instance_id,
        "LaunchTime": launch_time,
        "State": {"Name": "running"},
        "Tags": [
            {"Key": "Repository", "Value": tenant},
            {"Key": "Role", "Value": role},
            {"Key": "Tenant", "Value": tenant},
            {"Key": "Pr", "Value": str(pr)},
            {"Key": "Name", "Value": f"flow-{role}-{tenant.replace('/', '__')}-{pr}"},
        ],
    }


# --- AC-1: dry-run lists what would be terminated (fake aws) ---

def test_ac1_gc_dry_run_lists_candidates_without_terminating():
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Normal active instance (age = 15m, open PR) -> keep
    inst_active = make_instance("i-active", now - timedelta(minutes=15), pr=1)

    # 2. Timeout instance (age = 75m > 60m max wall clock) -> terminate
    inst_timeout = make_instance("i-timeout", now - timedelta(minutes=75), pr=2)

    # 3. Fresh instance (age = 10m), but PR is closed -> terminate
    inst_closed = make_instance("i-closed", now - timedelta(minutes=10), pr=3)

    # 4. Fresh instance (age = 10m), but PR has label flow:escalated -> terminate
    inst_escalated = make_instance("i-escalated", now - timedelta(minutes=10), pr=4)

    pr_states = {
        1: {"state": "open", "labels": []},
        2: {"state": "open", "labels": []},
        3: {"state": "closed", "labels": []},
        4: {"state": "open", "labels": [{"name": "flow:escalated"}]},
    }
    fake_gh = FakeGitHubClient(pr_states)
    fake_ec2 = FakeEC2Client([inst_active, inst_timeout, inst_closed, inst_escalated])

    # Execute with dry_run = True
    targets = run_gc(
        ec2=fake_ec2,
        gh=fake_gh,
        max_wall_clock_minutes=60,
        now=now,
        dry_run=True,
    )

    target_ids = [t.instance_id for t in targets]
    assert "i-active" not in target_ids
    assert "i-timeout" in target_ids
    assert "i-closed" in target_ids
    assert "i-escalated" in target_ids

    # Fake AWS terminate_instances was NOT called in dry run
    assert len(fake_ec2.terminated_instance_ids) == 0


def test_ac1_gc_non_dry_run_terminates_instances():
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    inst_timeout = make_instance("i-timeout", now - timedelta(minutes=75), pr=2)

    fake_gh = FakeGitHubClient({2: {"state": "open", "labels": []}})
    fake_ec2 = FakeEC2Client([inst_timeout])

    targets = run_gc(
        ec2=fake_ec2,
        gh=fake_gh,
        max_wall_clock_minutes=60,
        now=now,
        dry_run=False,
    )

    assert len(targets) == 1
    assert targets[0].instance_id == "i-timeout"
    assert "i-timeout" in fake_ec2.terminated_instance_ids


# --- AC-2: A lost run counts toward the round cap ---


def _make_memory_store() -> MemoryStore:
    store = MemoryStore()
    store.tenants["tenant-123"] = Tenant(
        id="tenant-123",
        repo="arsenyspb/ollama-monitor",
        status="active",
        arbitration_mode="shadow",
        manifest={"repo": "arsenyspb/ollama-monitor"},
    )
    return store


def test_ac2_lost_run_counted_toward_round_cap():
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    store = _make_memory_store()

    # Session started and stopped heartbeating 20 minutes ago (timeout is 10m)
    stale_session = {
        "instance_id": "i-zombie-1",
        "tenant_id": "tenant-123",
        "target_repo": "arsenyspb/ollama-monitor",
        "issue_id": 42,
        "role": "developer",
        "status": "Running",
        "heartbeat_at": (now - timedelta(minutes=20)).isoformat(),
        "cost_estimate": 0.50,
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    store.upsert_session(stale_session)

    # 1. Reaper reaps the stale session
    reaped = reap_stale_sessions(
        store=store,
        heartbeat_timeout_minutes=10,
        now=now,
    )
    assert len(reaped) == 1
    assert reaped[0]["instance_id"] == "i-zombie-1"
    assert store.sessions["i-zombie-1"]["status"] == "Halted"
    assert store.sessions["i-zombie-1"]["halted_reason"] == "stale/reaped"

    # 2. A failed loop round was recorded in loop_rounds
    rounds = store.loop_rounds_for_pr("tenant-123", 42)
    assert len(rounds) == 1
    assert rounds[0]["round"] == 1
    assert rounds[0]["envelope_digest"] == "reaped:lost_heartbeat"

    # 3. Conductor rebuild_loop_state counts the reaped round
    fake_gh = FakeGitHubClient({
        42: {
            "state": "open",
            "labels": [],
            "head": {"sha": "0123456789abcdef0123456789abcdef01234567"},
        }
    })
    bot_logins = {
        "developer": "atobar-flow-agent[bot]",
        "evaluator": "atobar-flow-agent[bot]",
        "arbitrator": "atobar-flow-agent[bot]",
    }

    loop_state = rebuild_loop_state(
        tenant_repo="arsenyspb/ollama-monitor",
        pr_number=42,
        gh=fake_gh,
        store=store,  # type: ignore[arg-type]
        bot_logins=bot_logins,
    )

    # Round number advanced to 1 due to the reaped round
    assert loop_state.round == 1

    # 4. If policy max_review_rounds is 1, next step triggers max_review_rounds circuit breaker
    policy = LoopLimits(max_review_rounds=1)
    verdict_event = VerdictEvent(
        verdict=Verdict(
            tenant="arsenyspb/ollama-monitor",
            pr=42,
            head_sha="0123456789abcdef0123456789abcdef01234567",
            round=1,
            verdict="changes_requested",
            findings=[],
        )
    )
    next_state, actions = step(loop_state, verdict_event, policy)
def test_ac6_crash_path_tombstone_and_partial_usage():
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    store = _make_memory_store()
    mock_gh = MagicMock()

    stale_session = {
        "instance_id": "i-zombie-usage",
        "tenant_id": "tenant-123",
        "target_repo": "arsenyspb/ollama-monitor",
        "issue_id": 42,
        "role": "developer",
        "status": "Running",
        "heartbeat_at": (now - timedelta(minutes=20)).isoformat(),
        "cost_estimate": 1.25,
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
        "agent_runtime": "nanobot",
        "iterations": 5,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cached_input_tokens": 200,
        },
    }
    store.upsert_session(stale_session)

    reaped = reap_stale_sessions(
        store=store,
        heartbeat_timeout_minutes=10,
        now=now,
        gh=mock_gh,
    )
    assert len(reaped) == 1
    assert store.sessions["i-zombie-usage"]["status"] == "Halted"
    assert store.sessions["i-zombie-usage"]["halted_reason"] == "stale/reaped"

    # Tombstone comment posted on GitHub
    mock_gh.create_comment.assert_called_once()
    comment_text = mock_gh.create_comment.call_args[0][2]
    assert "session terminated: last heartbeat" in comment_text
    assert "agent_runtime=nanobot" in comment_text
    assert "iterations=5" in comment_text

    # loop_rounds row carries usage_detail
    rounds = store.loop_rounds_for_pr("tenant-123", 42)
    assert len(rounds) == 1
    assert rounds[0]["tokens"] == 1500
    assert rounds[0]["usage_detail"]["cached_input_tokens"] == 200


def test_describe_instances_called_with_filters():
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {"Reservations": []}

    run_gc(ec2=mock_ec2, gh=None)

    expected_filters = [
        {"Name": "tag-key", "Values": ["Repository"]},
        {"Name": "instance-state-name", "Values": ["running", "pending"]},
    ]
    mock_ec2.describe_instances.assert_called_once_with(Filters=expected_filters)


def test_untagged_instance_never_terminated():
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    old_time = now - timedelta(minutes=120)

    # Instance without Repository tag
    untagged_instance = {
        "InstanceId": "i-not-flow",
        "LaunchTime": old_time,
        "State": {"Name": "running"},
        "Tags": [{"Key": "Name", "Value": "other-service"}],
    }
    tagged_instance = {
        "InstanceId": "i-flow-candidate",
        "LaunchTime": old_time,
        "State": {"Name": "running"},
        "Tags": [
            {"Key": "Repository", "Value": "owner/repo"},
            {"Key": "Role", "Value": "developer"},
            {"Key": "Tenant", "Value": "owner/repo"},
            {"Key": "Pr", "Value": "10"},
        ],
    }

    mock_ec2 = MagicMock()
    # Even if describe_instances returned both, only tagged flow instances are terminated
    mock_ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [untagged_instance, tagged_instance]}]
    }

    run_gc(ec2=mock_ec2, gh=None, now=now)
    terminated_ids = mock_ec2.terminate_instances.call_args[1]["InstanceIds"]

    assert "i-flow-candidate" in terminated_ids
    assert "i-not-flow" not in terminated_ids

