from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from flow_engine.conductor.gc import reap_stale_sessions, run_gc


def test_j06_ac3_reap_stale_sessions_no_typeerror():
    store = MagicMock()
    now = datetime.now(timezone.utc)
    stale_time = (now - timedelta(minutes=20)).isoformat()

    store.active_sessions.return_value = [
        {"instance_id": "i-123", "tenant_id": "t-1", "issue_id": 42, "heartbeat_at": stale_time}
    ]
    store.loop_rounds_for_pr.return_value = [{"round": 1}]

    reaped = reap_stale_sessions(store, heartbeat_timeout_minutes=10, now=now)
    assert len(reaped) == 1

    # Assert record_loop_round called with a dict, not kwargs
    store.record_loop_round.assert_called_once()
    args, kwargs = store.record_loop_round.call_args
    assert len(args) == 1
    assert isinstance(args[0], dict)
    assert args[0]["round"] == 2


def test_j06_ac4_run_gc_dry_run():
    ec2 = MagicMock()
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(minutes=100)

    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-abc",
                        "State": {"Name": "running"},
                        "LaunchTime": old_time,
                        "Tags": [
                            {"Key": "Repository", "Value": "owner/repo"},
                            {"Key": "Role", "Value": "developer"},
                            {"Key": "Tenant", "Value": "owner/repo"},
                            {"Key": "Pr", "Value": "42"},
                        ],
                    }
                ]
            }
        ]
    }

    gh = MagicMock()

    targets = run_gc(ec2, gh, dry_run=True, now=now)
    assert len(targets) == 1
    assert targets[0].instance_id == "i-abc"
    ec2.terminate_instances.assert_not_called()
