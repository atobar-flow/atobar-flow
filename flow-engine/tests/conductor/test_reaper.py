from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from flow_engine.store import MemoryStore, SupabaseStore
from flow_engine.conductor.gc import reap_stale_sessions


def test_active_sessions_and_update_session_on_memory_store():
    store = MemoryStore()
    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    stale_time = (now - timedelta(minutes=20)).isoformat()
    recent_time = (now - timedelta(minutes=5)).isoformat()

    store.sessions = {
        "inst-stale": {
            "instance_id": "inst-stale",
            "status": "Running",
            "heartbeat_at": stale_time,
            "tenant_id": "tenant-1",
            "issue_id": 1,
            "role": "developer",
            "head_sha": "a" * 40,
        },
        "inst-fresh": {
            "instance_id": "inst-fresh",
            "status": "Running",
            "heartbeat_at": recent_time,
            "tenant_id": "tenant-1",
            "issue_id": 2,
            "role": "developer",
            "head_sha": "b" * 40,
        },
        "inst-completed": {
            "instance_id": "inst-completed",
            "status": "Complete",
            "heartbeat_at": stale_time,
            "tenant_id": "tenant-1",
            "issue_id": 3,
            "role": "developer",
            "head_sha": "c" * 40,
        },
    }

    active = store.active_sessions(now=now)
    assert len(active) == 1
    assert active[0]["instance_id"] == "inst-stale"

    store.update_session("inst-stale", {"status": "Halted", "halted_reason": "stale/reaped"})
    assert store.sessions["inst-stale"]["status"] == "Halted"
    assert store.sessions["inst-stale"]["halted_reason"] == "stale/reaped"


def test_active_sessions_and_update_session_on_supabase_store():
    store = SupabaseStore("https://mock.supabase.co", "mock-key")
    mock_db = MagicMock()
    store._client = mock_db

    # Test active_sessions queries sessions table with proper filters
    mock_query = MagicMock()
    mock_db.table.return_value = mock_query
    mock_query.select.return_value = mock_query
    mock_query.in_.return_value = mock_query
    mock_query.lt.return_value = mock_query
    mock_query.execute.return_value = MagicMock(data=[{"instance_id": "inst-1", "status": "Running"}])

    sessions = store.active_sessions()
    assert sessions == [{"instance_id": "inst-1", "status": "Running"}]
    mock_db.table.assert_called_with("sessions")
    mock_query.in_.assert_called_with("status", ["Running", "running", "active", "pending"])

    # Test update_session calls update on sessions table
    mock_update = MagicMock()
    mock_db.table.return_value = mock_update
    mock_update.update.return_value = mock_update
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=[])

    store.update_session("inst-1", {"status": "Halted"})
    mock_update.update.assert_called_with({"status": "Halted"})
    mock_update.eq.assert_called_with("instance_id", "inst-1")


def test_reap_stale_sessions_writes_tombstone_loop_round():
    store = MemoryStore()
    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    stale_time = (now - timedelta(minutes=20)).isoformat()

    store.sessions = {
        "inst-stale": {
            "instance_id": "inst-stale",
            "status": "Running",
            "heartbeat_at": stale_time,
            "tenant_id": "tenant-1",
            "issue_id": 42,
            "role": "developer",
            "head_sha": "deadbeef" * 5,
            "usage": {"total_tokens": 1200},
        }
    }

    fake_gh = MagicMock()
    reaped = reap_stale_sessions(store, heartbeat_timeout_minutes=10.0, now=now, gh=fake_gh)

    assert len(reaped) == 1
    assert store.sessions["inst-stale"]["status"] == "Halted"

    # Verify tombstone loop_round was recorded
    rounds = store.loop_rounds_for_pr("tenant-1", 42)
    assert len(rounds) == 1
    assert rounds[0]["envelope_digest"] == "reaped:lost_heartbeat"
    assert rounds[0]["head_sha"] == "deadbeef" * 5
    assert rounds[0]["tokens"] == 1200
