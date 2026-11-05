import json

from flow_agent.agents.nanobot_config import render_nanobot_run_config


def test_nanobot_config_rendered(tmp_path):
    cfg_path, agent_ws = render_nanobot_run_config(
        run_id="run_test123",
        role="developer",
        tenant_provider="gemini",
        role_model="gemini-2.5-flash",
        system_prompt="You are a developer.",
        base_dir=str(tmp_path / "flow-run"),
    )

    assert cfg_path.exists()
    assert agent_ws.exists()

    with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # AC-4: config.json contains "${FLOW_LLM_API_KEY}" and never the key value
    gemini_provider = data.get("providers", {}).get("gemini", {})
    assert gemini_provider.get("apiKey") == "${FLOW_LLM_API_KEY}"

    # AC-4: tools.mcpServers.flow.args end with --role <role>
    mcp_flow = data.get("tools", {}).get("mcpServers", {}).get("flow", {})
    mcp_args = mcp_flow.get("args", [])
    assert mcp_args[-2:] == ["--role", "developer"]
    assert mcp_flow.get("env", {}).get("DISPLAY") == ":0"
    assert mcp_flow.get("env", {}).get("WORKSPACE_DIR") == str(agent_ws)
    assert mcp_flow.get("cwd") == str(agent_ws)

    # AC-4: no channels/cron keys present
    assert "channels" not in data
    assert "cron" not in data
    assert "cron_jobs" not in data

    # Check SOUL.md in agent_ws
    soul_file = agent_ws / "SOUL.md"
    assert soul_file.exists()
    assert soul_file.read_text(encoding="utf-8") == "You are a developer."


def test_nanobot_config_multi_provider(tmp_path):
    cfg_path, _ = render_nanobot_run_config(
        run_id="run_multi",
        role="evaluator",
        tenant_provider="claude",
        role_model="claude-sonnet-4-5",
        system_prompt="You are an evaluator.",
        base_dir=str(tmp_path / "flow-run"),
        extra_providers={"deepseek", "grok"},
    )

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    providers = data["providers"]

    # claude -> nanobot's native "anthropic" key, primary placeholder
    assert providers["anthropic"]["apiKey"] == "${FLOW_LLM_API_KEY}"
    # extra providers get per-provider env placeholders
    assert providers["deepseek"]["apiKey"] == "${FLOW_LLM_API_KEY_DEEPSEEK}"
    # grok is a custom OpenAI-compatible entry needing apiBase
    assert providers["grok"]["apiKey"] == "${FLOW_LLM_API_KEY_GROK}"
    assert providers["grok"]["apiBase"] == "https://api.x.ai/v1"
    # preset pins the role to the canonical provider key
    assert data["modelPresets"]["evaluator"]["provider"] == "anthropic"


def _defaults_for(tmp_path, role):
    from nanobot.config.schema import AgentDefaults

    cfg_path, _ = render_nanobot_run_config(
        run_id=f"run_{role}", role=role, tenant_provider="gemini", role_model="gemini-2.5-flash",
        system_prompt="x", base_dir=str(tmp_path / "flow-run"),
    )
    data = json.loads(cfg_path.read_text())
    return data, AgentDefaults.model_validate(data["agents"]["defaults"])


def test_planner_gets_hard_tool_iteration_budget_and_developer_keeps_default(tmp_path):
    _, planner = _defaults_for(tmp_path, "planner")
    _, developer = _defaults_for(tmp_path, "developer")
    assert planner.max_tool_iterations == 8
    assert developer.max_tool_iterations == 200


def test_mcp_server_accepts_every_role_the_config_launches_it_with(tmp_path):
    from flow_agent.runtime.tools import ROLE_ALLOWLISTS
    from flow_agent.tools_mcp.server import create_server, parse_args

    for role in ROLE_ALLOWLISTS:
        data, _ = _defaults_for(tmp_path, role)
        argv = data["tools"]["mcpServers"]["flow"]["args"]
        args = parse_args(argv[argv.index("--role"):])
        assert args.role == role
        assert create_server(role=args.role) is not None


def test_j44_wp1_nanobot_home_isolated_from_worktree_bootstrap_memory_and_skills(tmp_path, monkeypatch):
    import asyncio
    import base64
    from nanobot.agent.context import ContextBuilder
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry
    from unittest.mock import MagicMock

    from flow_agent.agents.nanobot_runtime import _restrict_builtin_tools

    worktree = tmp_path / "client_worktree"
    worktree.mkdir()
    (worktree / "AGENTS.md").write_text("EVIL_TENANT_AGENTS_PROMPT_PAYLOAD", encoding="utf-8")
    (worktree / "USER.md").write_text("EVIL_TENANT_USER_PROMPT_PAYLOAD", encoding="utf-8")
    (worktree / "memory").mkdir()
    (worktree / "memory" / "MEMORY.md").write_text("EVIL_TENANT_MEMORY_PAYLOAD", encoding="utf-8")
    (worktree / "skills" / "evil").mkdir(parents=True)
    (worktree / "skills" / "evil" / "SKILL.md").write_text(
        "---\nname: evil\ndescription: evil skill\nalways: true\n---\nEVIL_TENANT_SKILL_PAYLOAD",
        encoding="utf-8",
    )

    cfg_path, nanobot_home = render_nanobot_run_config(
        run_id="run_iso_1",
        role="evaluator",
        tenant_provider="gemini",
        role_model="gemini-3.8-flash",
        system_prompt="Trusted evaluator system prompt.",
        base_dir=str(tmp_path / "flow-run"),
        workspace_dir=str(worktree),
    )

    assert nanobot_home != worktree
    sys_prompt = ContextBuilder(nanobot_home).build_system_prompt()
    assert "Trusted evaluator system prompt." in sys_prompt
    assert "EVIL_TENANT_AGENTS_PROMPT_PAYLOAD" not in sys_prompt
    assert "EVIL_TENANT_USER_PROMPT_PAYLOAD" not in sys_prompt
    assert "EVIL_TENANT_MEMORY_PAYLOAD" not in sys_prompt
    assert "EVIL_TENANT_SKILL_PAYLOAD" not in sys_prompt

    cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
    mcp_flow = cfg_data["tools"]["mcpServers"]["flow"]
    assert mcp_flow["cwd"] == str(worktree)
    assert mcp_flow["env"]["WORKSPACE_DIR"] == str(worktree)

    # Verify view_image resolves relative screenshot paths against WORKSPACE_DIR and blocks outside paths
    monkeypatch.setenv("WORKSPACE_DIR", str(worktree))
    art_dir = worktree / ".atobar" / "artifacts"
    art_dir.mkdir(parents=True)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
    )
    (art_dir / "shot.png").write_bytes(png)
    outside_png = tmp_path / "outside.png"
    outside_png.write_bytes(png)

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=nanobot_home, allowed_dir=nanobot_home, restrict_to_workspace=True))
    bot = MagicMock()
    bot._loop.tools = registry
    _restrict_builtin_tools(bot, frozenset({"read_file"}), workspace_dir=str(worktree))
    viewer = registry.get("view_image")

    ok_res = asyncio.run(viewer.execute(path=".atobar/artifacts/shot.png"))
    assert isinstance(ok_res, list) and any(b.get("type") == "image_url" for b in ok_res)

    escaped_res = asyncio.run(viewer.execute(path=str(outside_png)))
    assert "escapes workspace boundary" in str(escaped_res)
