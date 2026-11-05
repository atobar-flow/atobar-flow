"""Service & package folder README completeness guard (#300 WP-0)."""

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SERVICE_ROOTS = [
    "contracts",
    "control-panel",
    "flow-engine",
    "flow-agent",
    "atobar-ops",
]

MANDATORY_SERVICE_SECTIONS = [
    "## Purpose",
    "## Runtime",
    "## APIs",
    "## Consumes",
    "## Data",
    "## Credentials",
    "## Allowed imports",
    "## Develop & test",
    "## Troubleshooting",
    "## Future repo",
]

SUBFOLDER_READMES = [
    "contracts/src/atobar_contracts",
    "control-panel/src/control_panel",
    "flow-engine/src/flow_engine/conductor",
    "flow-engine/src/flow_engine/tenancy",
    "flow-agent/src/flow_agent/agents",
    "flow-agent/src/flow_agent/core",
    "flow-agent/src/flow_agent/llm",
    "flow-agent/src/flow_agent/roles",
    "flow-agent/src/flow_agent/runtime",
    "flow-agent/src/flow_agent/telemetry",
    "flow-agent/src/flow_agent/tools",
    "flow-agent/src/flow_agent/tools_mcp",
    "flow-agent/src/flow_agent/knowledge",
    "atobar-ops/src/atobar_ops",
    "deploy/helm",
    "infra/packer",
    "infra/supabase",
    "infra/docker",
    "infra/terraform/control-panel-hetzner",
    "templates",
    "scripts",
    "docs",
    "tests",
]


@pytest.mark.parametrize("service_dir", SERVICE_ROOTS)
def test_service_root_readme_has_mandatory_sections(service_dir: str) -> None:
    readme = REPO_ROOT / service_dir / "README.md"
    assert readme.is_file(), f"Missing service README: {readme}"
    text = readme.read_text(encoding="utf-8")
    missing = [sec for sec in MANDATORY_SERVICE_SECTIONS if sec not in text]
    assert missing == [], f"{service_dir}/README.md missing sections: {missing}"


@pytest.mark.parametrize("rel_dir", SUBFOLDER_READMES)
def test_subfolder_readme_exists_and_non_empty(rel_dir: str) -> None:
    readme = REPO_ROOT / rel_dir / "README.md"
    assert readme.is_file(), f"Missing subfolder README: {readme}"
    text = readme.read_text(encoding="utf-8").strip()
    assert len(text) >= 40 and text.startswith("#"), f"README in {rel_dir} is empty or missing title heading"
