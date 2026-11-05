"""Renders per-run nanobot configuration and workspace files."""

import json
import os
from pathlib import Path
from typing import Collection, Optional, Tuple

from flow_agent.llm.providers import canonical_provider, key_env_var, nanobot_api_base, nanobot_config_key

# Files nanobot and the engine write into the workspace (the client checkout).
RUNTIME_FILES = (
    "SOUL.md",
    "USER.md",
    ".nanobot/",
    ".atobar/",
    "memory/MEMORY.md",
    "memory/HISTORY.md",
    "memory/history.jsonl",
    "memory/.cursor",
    "memory/.dream_cursor",
    "prompts/README.md",
    "skills/flow-github/",
)

GITHUB_SKILL = """---
name: flow-github
description: "The only way this run may touch GitHub: the flow tools. Replaces nanobot's gh-based github skill."
always: true
---

# GitHub via flow tools

The run's GitHub identity is held by the factory, not by you. Use only these tools:

- Open or update the PR for your branch: `create_and_verify_pr`. It pushes, opens the PR and
  returns the PR number and head SHA to put in your `flow-ack`.
- CI status of a PR head: `git_pr_checks`. Failing job output: `git_ci_job_logs`.
- Local history and diffs: plain `git` (`git status`, `git diff`, `git log`, `git commit`).

Never:

- read, print or export credentials (`git credential fill`, `~/.git-credentials`, `env`, `printenv`, `GITHUB_TOKEN`);
- call the GitHub API yourself (`curl api.github.com`, `gh`, `hub`, python `requests`/`httpx`);
- `git push` directly, push the default branch, or merge a PR.

If a flow tool fails, report the error in your final reply instead of working around it.
"""


def _git_exclude_runtime_files(workspace: Path) -> None:
    """Keep nanobot's per-run files out of the client repo's commits and PRs (supports git worktrees)."""
    git_entry = workspace / ".git"
    info_dirs: list[Path] = []
    if git_entry.is_dir():
        info_dirs.append(git_entry / "info")
    elif git_entry.is_file():
        try:
            raw = git_entry.read_text(encoding="utf-8").strip()
            if raw.startswith("gitdir:"):
                gitdir_str = raw.split(":", 1)[1].strip()
                gitdir = Path(gitdir_str) if Path(gitdir_str).is_absolute() else (workspace / gitdir_str).resolve()
                info_dirs.append(gitdir / "info")
                # In git worktrees, common gitdir is gitdir.parent.parent ($GIT_COMMON_DIR)
                if gitdir.parent.name == "worktrees":
                    info_dirs.append(gitdir.parent.parent / "info")
        except Exception:
            pass

    for info in info_dirs:
        try:
            info.mkdir(parents=True, exist_ok=True)
            exclude = info / "exclude"
            existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            missing = [f"/{name}" for name in RUNTIME_FILES if f"/{name}" not in existing]
            if missing:
                with exclude.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(["", *missing, ""]))
        except Exception:
            pass


# Hard per-role tool-iteration budgets. When the cap is hit, nanobot makes one final
# no-tools call so the role still emits its envelope. The planner's prompt asks for
# <= 5 localization turns; without a cap it was observed taking 17 on a 2-file repo.
_ROLE_MAX_TOOL_ITERATIONS: dict[str, int] = {"planner": 8}


def render_nanobot_run_config(
    run_id: str,
    role: str,
    tenant_provider: str,
    role_model: str,
    system_prompt: str,
    base_dir: Optional[str] = None,
    repo_dir: str = "/home/ubuntu/atobar-flow",
    workspace_dir: Optional[str] = None,
    extra_providers: Optional[Collection[str]] = None,
    allowed_tools: Optional[Collection[str]] = None,
) -> Tuple[Path, Path]:
    """Renders config.json and workspace with SOUL.md under base_dir/<run_id>.

    Returns (config_path, agent_workspace_path).
    """
    if base_dir is None:
        base_dir = os.environ.get("FLOW_RUN_BASE_DIR") or (
            "/home/ubuntu/flow-run" if os.path.exists("/home/ubuntu") else "/tmp/flow-run"
        )
    run_root = Path(base_dir) / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    nanobot_home = run_root / "agent_ws"
    nanobot_home.mkdir(parents=True, exist_ok=True)
    if workspace_dir is not None:
        client_ws = Path(workspace_dir)
        client_ws.mkdir(parents=True, exist_ok=True)
        _git_exclude_runtime_files(client_ws)
    else:
        client_ws = nanobot_home

    # Write SOUL.md and skills/flow-github/SKILL.md into the factory-owned nanobot_home (J44 WP-1)
    soul_file = nanobot_home / "SOUL.md"
    soul_file.write_text(system_prompt, encoding="utf-8")
    skill_file = nanobot_home / "skills" / "flow-github" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(GITHUB_SKILL, encoding="utf-8")

    # Build providers map. apiKey values are ${ENV} placeholders resolved by
    # nanobot at load time — plaintext keys never touch disk.
    try:
        primary = canonical_provider(tenant_provider)
    except ValueError:
        primary = tenant_provider.strip().lower()  # custom provider passthrough
    providers: dict[str, dict[str, str]] = {}
    for provider in {primary, *(extra_providers or {})}:
        entry: dict[str, str] = {
            "apiKey": "${FLOW_LLM_API_KEY}" if provider == primary else "${" + key_env_var(provider) + "}"
        }
        api_base = nanobot_api_base(provider)
        if api_base:
            entry["apiBase"] = api_base
        providers[nanobot_config_key(provider)] = entry

    reasoning_effort = os.environ.get("FLOW_ROLE_REASONING_EFFORT")

    preset = {
        "model": role_model,
        "provider": nanobot_config_key(primary),
    }
    if reasoning_effort:
        preset["reasoningEffort"] = reasoning_effort

    model_presets: dict[str, dict[str, str]] = {
        role: preset
    }

    config_data = {
        "providers": providers,
        "agents": {
            "defaults": {
                "modelPreset": role,
                "disabledSkills": ["github"],
                **(
                    {"maxToolIterations": _ROLE_MAX_TOOL_ITERATIONS[role]}
                    if role in _ROLE_MAX_TOOL_ITERATIONS
                    else {}
                ),
            }
        },
        "modelPresets": model_presets,
        "tools": {
            "restrictToWorkspace": True,
            "mcpServers": {
                "flow": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--directory",
                        repo_dir,
                        "python",
                        "-m",
                        "flow_agent.tools_mcp.server",
                        "--role",
                        role,
                    ],
                    "cwd": str(client_ws),
                    "env": {
                        **{
                            k: v for k, v in os.environ.items()
                            if k in (
                                "CONTROL_PANEL_URL",
                                "CP_URL",
                                "FLOW_RUN_ID",
                                "FLOW_RUN_CREDENTIAL",
                                "FLOW_ROLE",
                                "FLOW_TENANT",
                                "FLOW_PR",
                                "FLOW_HEAD_SHA",
                                "TARGET_REPO",
                                "ISSUE_ID",
                                "INSTANCE_ID",
                                "GITHUB_TOKEN",
                                "WORKSPACE_DIR",
                                "PYTHONPATH",
                                "PATH",
                                "HOME",
                                "DISPLAY",
                            )
                        },
                        "WORKSPACE_DIR": str(client_ws),
                        "DISPLAY": os.environ.get("DISPLAY", ":0"),
                    },
                    **({"enabledTools": sorted(allowed_tools)} if allowed_tools else {}),
                }
            },
        },
    }

    config_path = run_root / "config.json"
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

    return config_path, nanobot_home
