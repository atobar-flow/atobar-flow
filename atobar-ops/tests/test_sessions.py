"""Tests for `atobar-ops sessions audit` on Pi and Claude Code synthetic JSONL fixtures (J47 / #359 WP-8)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from atobar_ops import sessions


def _write_pi_fixture_session(path: Path) -> None:
    records = [
        {"type": "session", "version": 1, "id": "s1", "timestamp": "2026-10-22T03:15:49Z", "cwd": "/repo"},
        "NOT VALID JSON LINE {",
        {
            "type": "message",
            "id": "m1",
            "timestamp": "2026-10-22T03:16:00Z",
            "message": {
                "role": "assistant",
                "usage": {
                    "totalTokens": 100000,
                    "cacheRead": 90000,
                    "input": 8000,
                    "output": 2000,
                    "cost": {"total": 0.25},
                },
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc1",
                        "name": "read",
                        "arguments": {"path": ".pi/skills/atobar-ops/SKILL.md", "offset": 1, "limit": 50},
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "m2",
            "timestamp": "2026-10-22T03:16:01Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "tc1",
                "toolName": "read",
                "isError": False,
                "content": [{"type": "text", "text": "SUPER_SECRET_SKILL_TEXT_DO_NOT_PRINT"}],
            },
        },
        {
            "type": "message",
            "id": "m3",
            "timestamp": "2026-10-22T03:16:10Z",
            "message": {
                "role": "assistant",
                "usage": {
                    "totalTokens": 120000,
                    "cacheRead": 0,
                    "input": 115000,
                    "output": 5000,
                    "cost": {"total": 0.50},
                },
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc2",
                        "name": "read",
                        "arguments": {"path": ".pi/skills/atobar-ops/SKILL.md", "offset": 25, "limit": 40},
                    },
                    {
                        "type": "toolCall",
                        "id": "tc3",
                        "name": "bash",
                        "arguments": {
                            "command": "for i in $(seq 20); do sleep 30; done && gh api repos/o/r/pulls/335/comments && uv run --no-sync atobar-ops db sql 'select bad from t'"
                        },
                    },
                    {
                        "type": "toolCall",
                        "id": "tc4",
                        "name": "bash",
                        "arguments": {"command": "uv run --no-sync atobar-ops ship --issue 359"},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": "m4",
            "timestamp": "2026-10-22T03:16:20Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "tc3",
                "toolName": "bash",
                "isError": True,
                "content": [{"type": "text", "text": 'ERROR: column "bad" does not exist'}],
            },
        },
        {
            "type": "message",
            "id": "m5",
            "timestamp": "2026-10-22T03:16:25Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "tc4",
                "toolName": "bash",
                "isError": True,
                "content": [{"type": "text", "text": "REFUSED: uncommitted tracked changes in working tree"}],
            },
        },
    ]
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_claude_fixture_session(path: Path) -> None:
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-10-23T08:00:00Z",
            "message": {
                "role": "assistant",
                "usage": {
                    "input_tokens": 2000,
                    "cache_read_input_tokens": 50000,
                    "cache_creation_input_tokens": 1000,
                    "output_tokens": 500,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Read",
                        "input": {"file_path": "flow-engine/src/flow_engine/api.py", "offset": 10, "limit": 50},
                    },
                    {
                        "type": "tool_use",
                        "id": "tu2",
                        "name": "Edit",
                        "input": {"file_path": "flow-engine/src/flow_engine/api.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tu3",
                        "name": "Read",
                        "input": {"file_path": "flow-engine/src/flow_engine/api.py", "offset": 10, "limit": 50},
                    },
                ],
            },
        }
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_wp8_sessions_audit_counts_overlapping_reads_cold_turns_and_never_leaks_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f1 = tmp_path / "2026-10-22T03-15-49-000Z_uuid1.jsonl"
    _write_pi_fixture_session(f1)

    args = argparse.Namespace(
        dir=str(tmp_path),
        harness="pi",
        since="2026-10-22",
        timeout=30,
        json=False,
        verbose=False,
        max_chars=None,
    )
    rc = sessions.cmd_sessions_audit(args)
    out = capsys.readouterr().out
    assert rc == sessions.EXIT_OK
    assert "2026-10-22T03-15-49" in out
    assert "note: skipped 1 malformed transcript line(s)" in out
    assert "SUPER_SECRET_SKILL_TEXT_DO_NOT_PRINT" not in out

    args_json = argparse.Namespace(
        dir=str(tmp_path),
        harness="pi",
        since="2026-10-22",
        timeout=30,
        json=True,
        verbose=False,
        max_chars=None,
    )
    assert sessions.cmd_sessions_audit(args_json) == sessions.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    row = payload["sessions"][0]
    assert row["turns"] == 2
    assert row["total_tokens"] == 220000
    assert pytest.approx(row["cost_usd"], rel=1e-3) == 0.75
    assert row["cold_turns"] == 1
    assert pytest.approx(row["cold_cost_usd"], rel=1e-3) == 0.50
    assert row["tool_errors"] == 2
    assert row["single_op_reads"] == 1
    assert row["repeat_reads"] == 1
    assert row["long_sleeps"] == 1
    assert row["raw_gh_comments"] == 1
    assert row["failed_db_sql"] == 1
    assert row["ship_refusals"] == 1
    assert row["read_skill_md"] is True
    assert row["malformed_lines"] == 1


def test_wp8_sessions_audit_supports_claude_format_and_resets_read_intervals_on_edit(
    tmp_path: Path,
) -> None:
    f_claude = tmp_path / "2026-10-23T08-00-00_claude.jsonl"
    _write_claude_fixture_session(f_claude)
    m = sessions.audit_transcript_file(f_claude)
    assert m.harness == "claude"
    assert m.turns == 1
    assert m.total_tokens == 53500
    # Read after Edit on the same path is NOT counted as a repeat read
    assert m.repeat_reads == 0
