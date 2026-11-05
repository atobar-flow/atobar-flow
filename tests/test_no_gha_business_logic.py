"""Guard against GitHub Actions business-logic workflows (#301 WP-0 / AC-1)."""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

FORBIDDEN_PATHS = [
    ".github/workflows/flow_conductor.yml",
    ".github/workflows/flow_dispatcher.yml",
    ".github/workflows/flow_gc.yml",
    ".github/workflows/onboard-tenant.yml",
    ".github/workflows/supabase-pinger.yml",
    "scripts/notify_dispatch.py",
    "flow-engine/src/flow_engine/tenancy/spec.py",
    "flow-engine/src/flow_engine/tenancy/onboard_spec.py",
    "tenants",
]


def test_superseded_workflows_and_files_removed() -> None:
    existing = [rel for rel in FORBIDDEN_PATHS if (REPO_ROOT / rel).exists()]
    assert existing == [], f"Superseded files/dirs still exist: {existing}"


def test_remaining_workflows_never_invoke_flow_engine_business_logic() -> None:
    wf_dir = REPO_ROOT / ".github" / "workflows"
    violations: list[str] = []
    for wf in sorted(wf_dir.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if "python -m flow_engine" in text:
            violations.append(f"{wf.name} invokes python -m flow_engine")
    assert violations == [], f"Workflows invoking flow_engine: {violations}"
