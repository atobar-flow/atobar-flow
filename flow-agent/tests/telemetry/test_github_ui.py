import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from flow_agent.telemetry.github_ui import GitHubTelemetryUI


@pytest.mark.asyncio
async def test_initialize_posts_master_comment():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")
    ui.client = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": 123456}
    ui.client.post.return_value = mock_resp

    await ui.initialize()

    ui.client.post.assert_called_once()
    assert ui.comment_id == 123456

    # Clean up worker task
    if ui.worker_task:
        ui.worker_task.cancel()
        try:
            await ui.worker_task
        except asyncio.CancelledError:
            pass

def test_format_payload():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")

    ui.update_state({
        "last_tool": "multi_edit",
        "iterations": 3,
        "tasks": [
            {"status": "completed", "content": "First task"},
            {"status": "in_progress", "content": "Second task"},
            {"status": "pending", "content": "Third task"},
        ],
    })

    payload = ui.format_payload(ui.agent_state)

    # Check state and plan formatting
    assert "Last Tool: `multi_edit`" in payload
    assert "Iterations: 3" in payload
    assert "- [x] First task" in payload
    assert "- [ ] Second task *(In Progress)*" in payload
    assert "- [ ] Third task" in payload
    assert not ui.queue.empty()

def test_update_state_merges_cognitive_plan_without_overwrite():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")

    # First update: cognitive plan established via todo_write
    ui.update_state({
        "tasks": [
            {"status": "in_progress", "content": "Implement feature"},
            {"status": "pending", "content": "Run tests"},
        ],
    })

    # Subsequent update: tool call execution updates last_tool and iterations
    ui.update_state({
        "last_tool": "multi_edit",
        "iterations": 1,
    })

    payload = ui.format_payload(ui.agent_state)
    assert "- [ ] Implement feature *(In Progress)*" in payload
    assert "- [ ] Run tests" in payload
    assert "Last Tool: `multi_edit`" in payload
    assert "Iterations: 1" in payload


@pytest.mark.asyncio
async def test_token_heartbeat_delta_tracking():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")
    ui.comment_id = 123456
    ui.client = AsyncMock()

    # Step 1: Agent uses 1000 tokens
    ui.update_state({"total_tokens": 1000})
    payload1 = ui.format_payload(ui.agent_state)
    assert "Tokens: 1,000 (+1,000 since last heartbeat)" in payload1

    # Simulate heartbeat push (PATCH comment)
    await ui._push_update()
    assert ui.last_heartbeat_tokens == 1000

    # Step 2: Next iteration, agent uses 500 more tokens (total 1500)
    ui.update_state({"total_tokens": 1500})
    payload2 = ui.format_payload(ui.agent_state)
    assert "Tokens: 1,500 (+500 since last heartbeat)" in payload2

    # Heartbeat push again
    await ui._push_update()
    assert ui.last_heartbeat_tokens == 1500

    # Step 3: No new tokens used before next heartbeat
    payload3 = ui.format_payload(ui.agent_state)
    assert "Tokens: 1,500 (+0 since last heartbeat)" in payload3


def test_format_payload_renders_per_tool_token_breakdown():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")
    ui.update_state({
        "total_tokens": 1200,
        "by_tool": {
            "execute_command": {"calls": 3, "total_tokens": 800, "result_tokens": 150, "cost_usd": 0.0012},
            "read_file": {"calls": 2, "total_tokens": 400, "result_tokens": 90, "cost_usd": 0.0006},
        },
        "screenshots": [
            {
                "url": "https://cp.example.invalid/v1/runs/run-1/screenshots/compact.png",
                "markdown": "![Compact layout](https://cp.example.invalid/v1/runs/run-1/screenshots/compact.png)",
            }
        ],
    })
    payload = ui.format_payload(ui.agent_state)
    assert "execute_command" in payload
    assert "800" in payload
    assert "read_file" in payload
    assert "https://cp.example.invalid/v1/runs/run-1/screenshots/compact.png" in payload
    assert "![Compact layout]" in payload



@pytest.mark.asyncio
async def test_terminate():
    ui = GitHubTelemetryUI("owner/repo", 42, "fake-token")
    ui.client = AsyncMock()
    ui.comment_id = 123456

    await ui.terminate(error=False)

    assert ui.status == "✅ Complete"
    ui.client.patch.assert_called_once()
    ui.client.aclose.assert_called_once()
