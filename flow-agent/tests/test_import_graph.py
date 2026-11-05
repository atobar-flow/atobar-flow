"""Behavioral cold-start import graph tests for flow_agent entry points and modules (#349)."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FLOW_AGENT_SRC = REPO_ROOT / "flow-agent" / "src"
CONTRACTS_SRC = REPO_ROOT / "contracts" / "src"

ENTRYPOINTS: tuple[str, ...] = (
    "flow_agent.__main__",
    "flow_agent.main",
    "flow_agent.tools_mcp.server",
    "flow_agent.knowledge.onboard_repo",
    "flow_agent.roles.base",
    "flow_agent.roles.developer.runner",
    "flow_agent.roles.developer.spec",
    "flow_agent.roles.evaluator.runner",
    "flow_agent.roles.evaluator.spec",
    "flow_agent.roles.arbitrator.run",
    "flow_agent.roles.arbitrator.gate",
    "flow_agent.roles.planner.runner",
    "flow_agent.roles.planner.spec",
    "flow_agent.roles.metric_compiler.runner",
    "flow_agent.roles.onboarder",
    "flow_agent.roles.onboarder.runner",
    "flow_agent.runtime",
    "flow_agent.runtime.llm_loop",
    "flow_agent.agents.nanobot_runtime",
    "flow_agent.tools.ci_tools",
    "flow_agent.tools.git_tools",
    "flow_agent.tools.vision_tools",
    "flow_agent.core.untrusted",
)


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    paths = [str(FLOW_AGENT_SRC), str(CONTRACTS_SRC)]
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_cold_subprocess_import_entrypoint(entrypoint: str) -> None:
    """Every entry point must import cleanly in a fresh Python interpreter regardless of import order."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {entrypoint}"],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Cold import of {entrypoint!r} failed (rc={proc.returncode}):\n{proc.stderr}"
    )


def test_core_modules_do_not_load_higher_layer_packages_at_runtime() -> None:
    """Importing flow_agent.core modules in a fresh interpreter must not pull higher-level flow_agent packages."""
    script = """
import sys
import flow_agent.core.config
import flow_agent.core.untrusted
import flow_agent.core.watchdog

forbidden_prefixes = (
    "flow_agent.agents",
    "flow_agent.knowledge",
    "flow_agent.llm",
    "flow_agent.roles",
    "flow_agent.runtime",
    "flow_agent.telemetry",
    "flow_agent.tools",
    "flow_agent.tools_mcp",
    "flow_agent.main",
)
loaded = [
    m for m in sorted(sys.modules)
    if any(m == p or m.startswith(p + ".") for p in forbidden_prefixes)
]
assert not loaded, f"flow_agent.core loaded higher-level modules: {loaded}"
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"Runtime leaf check failed:\n{proc.stderr}"


def test_every_flow_agent_module_cold_imports_independently() -> None:
    """Every module in flow_agent must import cleanly when it is the first flow_agent module loaded."""
    script = r"""
import importlib
import pkgutil
import sys

pkg = importlib.import_module("flow_agent")
mods = ["flow_agent"] + sorted(m.name for m in pkgutil.walk_packages(pkg.__path__, "flow_agent."))
failures = []
for mod in mods:
    for key in [k for k in list(sys.modules) if k == "flow_agent" or k.startswith("flow_agent.")]:
        del sys.modules[key]
    try:
        importlib.import_module(mod)
    except Exception as exc:
        failures.append(f"{mod}: {type(exc).__name__}: {exc}")
assert not failures, "Cold module import failures:\n" + "\n".join(failures)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"Independent module import sweep failed:\n{proc.stderr}"
