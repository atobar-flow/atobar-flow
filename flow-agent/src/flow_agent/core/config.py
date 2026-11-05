import json
import os
import pathlib
from typing import Any, Dict

# Factory-side model for repo onboarding only; role models come from the Control Panel.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# Hidden comment marking a fresh flow:* trigger; loop state before it is ignored.
LOOP_EPOCH_MARKER = "<!-- flow:loop-epoch -->"

DEFAULT_FLOW_CONFIG: Dict[str, Any] = {
    "max_stdout_truncation_chars": 4000,
    "max_ci_retries": 3,
    "max_wall_clock_minutes": 60,
    "wip_state_branch_prefix": "flow-wip/",
    "bot_logins": {
        "planner": "atobar-flow-agent[bot]",
        "developer": "atobar-flow-agent[bot]",
        "evaluator": "atobar-flow-agent[bot]",
        "arbitrator": "atobar-flow-agent[bot]",
        "conductor": "atobar-flow-agent[bot]",
    },
}


def get_flow_config(custom_path: str = "") -> Dict[str, Any]:
    """Unified flow.config.json loader honoring:

    1. Explicit custom_path (if passed and exists)
    2. FLOW_CONFIG_PATH environment variable
    3. Engine repo root (relative to src/flow_engine/core/config.py)
    4. CWD / flow.config.json
    5. In-memory DEFAULT_FLOW_CONFIG fallback
    """
    candidates = []
    if custom_path:
        candidates.append(pathlib.Path(custom_path))

    env_path = os.environ.get("FLOW_CONFIG_PATH")
    if env_path:
        candidates.append(pathlib.Path(env_path))

    for parent in pathlib.Path(__file__).resolve().parents:
        candidates.append(parent / "flow.config.json")

    # CWD fallback
    candidates.append(pathlib.Path.cwd() / "flow.config.json")

    config = dict(DEFAULT_FLOW_CONFIG)
    for path in candidates:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                    config.update(user_config)
                    return config
            except Exception:
                continue

    return config


def get_bot_logins(custom_path: str = "") -> dict[str, str]:
    """Returns the role -> bot login mapping from config/env."""
    config = get_flow_config(custom_path)
    configured = config.get("bot_logins", {})
    single_bot = os.environ.get("FLOW_APP_BOT_LOGIN")
    return {
        "planner": single_bot or configured.get("planner", configured.get("developer", "atobar-flow-agent[bot]")),
        "developer": single_bot or configured.get("developer", "atobar-flow-agent[bot]"),
        "evaluator": single_bot or configured.get("evaluator", "atobar-flow-agent[bot]"),
        "arbitrator": single_bot or configured.get("arbitrator", "atobar-flow-agent[bot]"),
        "conductor": single_bot or configured.get("conductor", "atobar-flow-agent[bot]"),
    }

