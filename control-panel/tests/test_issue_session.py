from datetime import datetime, timedelta, timezone

from control_panel.store import MemoryStore, session_is_live

NOW = datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc)


def _row(status: str, minutes_ago: int, issue: int = 33, tenant: str = "t") -> dict:
    return {
        "instance_id": f"i-{status}-{minutes_ago}-{issue}",
        "tenant_id": tenant,
        "issue_id": issue,
        "status": status,
        "heartbeat_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }


def test_session_is_live_requires_fresh_non_terminal_heartbeat():
    assert session_is_live(_row("Running", 1), NOW)
    assert session_is_live(_row("pending", 0), NOW)
    assert not session_is_live(_row("Running", 11), NOW)
    assert not session_is_live(_row("Complete", 1), NOW)
    assert not session_is_live(_row("Halted: watchdog", 1), NOW)
    assert not session_is_live(_row("dispatched", 1), NOW)


def test_issue_session_active_is_scoped_to_tenant_and_issue():
    store = MemoryStore()
    store.upsert_session(_row("Running", 2))
    assert store.issue_session_active("t", 33, NOW)
    assert not store.issue_session_active("t", 43, NOW)
    assert not store.issue_session_active("other", 33, NOW)
