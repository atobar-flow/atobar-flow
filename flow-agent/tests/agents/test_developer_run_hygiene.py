"""Regression tests for ollama-monitor#33: runtime files, WIP sync and tool exposure."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from flow_agent.agents.nanobot_config import render_nanobot_run_config
from flow_agent.agents.nanobot_runtime import FlowHook, _has_unpublished_work, _restrict_builtin_tools
from flow_agent.runtime.wip import WipSyncer


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True, capture_output=True)
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    (ws / "app.py").write_text("print(1)\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-qm", "init")
    _git(ws, "push", "-q", "origin", "main")
    _git(ws, "remote", "set-head", "origin", "main")
    return ws


def test_nanobot_runtime_files_are_git_ignored(tmp_path):
    ws = _repo(tmp_path)
    _, nanobot_home = render_nanobot_run_config(
        run_id="r1", role="developer", tenant_provider="gemini", role_model="gemini-3.8-flash",
        system_prompt="soul", base_dir=str(tmp_path / "run"), workspace_dir=str(ws),
        allowed_tools=frozenset({"read_file"}),
    )
    (ws / ".nanobot").mkdir(exist_ok=True)
    (ws / ".nanobot" / "workspace-id").write_text("x")
    for rel in ("USER.md", "memory/history.jsonl", "memory/.cursor", "memory/MEMORY.md", ".atobar/state.json"):
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("x")
    assert (nanobot_home / "SOUL.md").exists()
    assert not (ws / "SOUL.md").exists()
    assert _git(ws, "status", "--porcelain") == ""
    assert not _has_unpublished_work(str(ws))


def test_mcp_server_limited_to_role_allowlist(tmp_path):
    import json

    cfg, _ = render_nanobot_run_config(
        run_id="r2", role="developer", tenant_provider="gemini", role_model="gemini-3.8-flash",
        system_prompt="soul", base_dir=str(tmp_path), allowed_tools=frozenset({"read_file", "execute_command"}),
    )
    server = json.loads(cfg.read_text())["tools"]["mcpServers"]["flow"]
    assert server["enabledTools"] == ["execute_command", "read_file"]


def test_wip_sync_leaves_work_branch_untouched(tmp_path):
    import asyncio

    ws = _repo(tmp_path)
    head = _git(ws, "rev-parse", "HEAD")
    (ws / "app.py").write_text("print(2)\n")
    syncer = WipSyncer(role="developer", issue_id=33, workspace_dir=str(ws))
    asyncio.run(syncer.sync_once())

    assert _git(ws, "rev-parse", "HEAD") == head
    assert _git(ws, "status", "--porcelain") == "M app.py"
    snap = _git(ws, "ls-remote", "origin", "refs/heads/flow-wip/developer/33").split()[0]
    assert _git(ws, "show", f"{snap}:app.py") == "print(2)"
    assert _has_unpublished_work(str(ws))


def _builtin_registry(tmp_path):
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path))
    for name in ("exec", "write_file", "edit_file", "grep", "list_dir", "spawn", "web_search", "mcp_flow_read_file"):
        tool = MagicMock()
        tool.name = name
        registry.register(tool)
    bot = MagicMock()
    bot._loop.tools = registry
    return bot, registry


def test_builtins_duplicating_flow_mcp_tools_are_unregistered(tmp_path):
    bot, registry = _builtin_registry(tmp_path)
    _restrict_builtin_tools(bot, frozenset({"read_file", "execute_command", "multi_edit", "search_codebase"}))
    # Only the flow MCP tools plus the images-only viewer remain: no second shell/reader.
    assert sorted(registry.tool_names) == ["mcp_flow_read_file", "view_image"]


def test_image_viewer_is_dropped_when_role_cannot_read_files(tmp_path):
    bot, registry = _builtin_registry(tmp_path)
    _restrict_builtin_tools(bot, frozenset({"execute_command"}))
    assert registry.tool_names == ["mcp_flow_read_file"]


def test_view_image_shows_images_and_redirects_text_reads(tmp_path):
    import asyncio
    import base64

    bot, registry = _builtin_registry(tmp_path)
    _restrict_builtin_tools(bot, frozenset({"read_file"}))
    viewer = registry.get("view_image")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
    )
    (tmp_path / "shot.png").write_bytes(png)
    (tmp_path / "README.md").write_text("hello\n")

    image_result = asyncio.run(viewer.execute(path=str(tmp_path / "shot.png")))
    text_result = asyncio.run(viewer.execute(path=str(tmp_path / "README.md")))

    assert isinstance(image_result, list) and any(b.get("type") == "image_url" for b in image_result)
    assert "mcp_flow_read_file" in str(text_result)
    assert FlowHook._normalize_tool_name("view_image") == "read_file"


def test_wip_sync_is_a_noop_without_an_issue(tmp_path):
    import asyncio
    from unittest.mock import MagicMock

    proc = MagicMock()
    syncer = WipSyncer(role="metric_compiler", issue_id=0, workspace_dir=str(tmp_path))
    syncer._subprocess = proc
    asyncio.run(syncer.sync_once())
    proc.run.assert_not_called()
