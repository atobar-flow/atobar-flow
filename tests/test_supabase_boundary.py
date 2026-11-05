"""Zero-Supabase boundary guard for workflows, cloud-init, and flow-agent (#301 WP-0 / AC-1)."""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"SUPABASE_|\bsupabase\b", re.IGNORECASE)


def test_workflows_never_reference_supabase() -> None:
    wf_dir = REPO_ROOT / ".github" / "workflows"
    violations: list[str] = []
    for wf in sorted(wf_dir.glob("*.yml")):
        if wf.name == "supabase-migrate.yml":
            continue
        for idx, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), start=1):
            if PATTERN.search(line):
                violations.append(f"{wf.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
    assert violations == [], "Workflows referencing Supabase:\n" + "\n".join(violations)


def test_cloud_init_never_references_supabase() -> None:
    tpl = REPO_ROOT / "infra" / "terraform" / "control-panel" / "scripts" / "cloud_init.sh.tpl"
    violations: list[str] = []
    for idx, line in enumerate(tpl.read_text(encoding="utf-8").splitlines(), start=1):
        if PATTERN.search(line):
            violations.append(f"{tpl.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
    assert violations == [], "cloud_init.sh.tpl references Supabase:\n" + "\n".join(violations)


def test_flow_agent_never_references_supabase() -> None:
    agent_dir = REPO_ROOT / "flow-agent" / "src"
    violations: list[str] = []
    for p in sorted(agent_dir.rglob("*.py")):
        if p.name == "pty_manager.py":
            continue
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            if PATTERN.search(line):
                violations.append(f"{p.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
    assert violations == [], "flow-agent references Supabase:\n" + "\n".join(violations)
