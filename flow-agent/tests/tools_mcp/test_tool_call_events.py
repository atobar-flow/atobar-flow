from unittest.mock import patch

import pytest

from flow_agent.tools_mcp.server import create_server


@pytest.mark.asyncio
async def test_mcp_tool_execution_emits_tool_call_event():
    emitted = []

    def fake_emit(kind, **payload):
        emitted.append((kind, payload))

    with patch("flow_agent.telemetry.event_sink.emit", side_effect=fake_emit):
        server = create_server("developer")
        # Find read_file tool on the FastMCP server
        tool_obj = server._tool_manager.get_tool("read_file")
        assert tool_obj is not None

        # Execute read_file
        # When calling the underlying fn directly
        res = tool_obj.fn(file_path="pyproject.toml")
        assert "Autonomous AI SWE orchestrator" in res

    assert len(emitted) >= 1
    call = emitted[-1]
    assert call[0] == "tool_call"
    payload = call[1]
    assert payload["name"] == "read_file"
    assert payload["ok"] is True
    assert "duration_ms" in payload
    assert payload["duration_ms"] >= 0
    assert "result_head" in payload
    assert "Autonomous AI SWE orchestrator" in payload["result_head"]
    assert "args_head" in payload
