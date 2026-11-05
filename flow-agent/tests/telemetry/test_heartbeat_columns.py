from flow_agent.telemetry.session_registry import SessionRegistry

# Hardcoded post-0012/sessions-telemetry migration column list for public.sessions
POST_0012_SESSIONS_COLUMNS = {
    "instance_id",
    "target_repo",
    "issue_id",
    "status",
    "started_at",
    "heartbeat_at",
    "current_todo",
    "last_tool",
    "cost_estimate",
    "issue_url",
    "pr_url",
    "trace_head_seq",
    "halted_reason",
    "tenant_id",
    "role",
    "run_id",
    # Telemetry columns added in migration
    "usage",
    "iterations",
    "agent_runtime",
    "last_activity_at",
    "head_sha",
    "halt_reason",
    "halt_detail",
}


def test_session_registry_payload_keys_subset_of_sessions_columns(monkeypatch):
    monkeypatch.setenv("FLOW_HEAD_SHA", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("FLOW_AGENT_RUNTIME", "nanobot")
    registry = SessionRegistry("inst-123", "owner/repo", 99)
    registry.usage = {"total": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}}
    registry.iterations = 3
    registry.halt_reason = "stall"
    registry.halt_detail = "no progress"

    payload = registry._payload()

    assert payload["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert payload["agent_runtime"] == "nanobot"
    assert payload["iterations"] == 3
    assert payload["usage"] is not None
    # Headline regression check: every payload key must exist in sessions table
    assert set(payload.keys()).issubset(POST_0012_SESSIONS_COLUMNS)
