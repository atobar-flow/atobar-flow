"""Harness shim, skill-table parity, and path-existence guards (J41 #333 WP-9 & J47 #359 WP-7)."""

import pathlib
import re

from atobar_ops import cli

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / ".pi" / "skills" / "atobar-ops"

# Devin CLI discovery verified against Devin CLI v3000.11.92 docs:
# - `share/devin/docs/extensibility/rules.mdx` & `share/devin/docs/reference/configuration/read-config-from.mdx`:
#   Devin natively loads root `AGENTS.md` (`read_config_from.agents_standard`) and imports
#   `.claude/skills/**/SKILL.md` (`read_config_from.claude`), resolving `.claude/skills/atobar-ops`.
REQUIRED_SYMLINK_SHIMS: tuple[tuple[str, str], ...] = (
    ("CLAUDE.md", "AGENTS.md"),
    (".cursorrules", "AI.md"),
    (".claude/skills/atobar-ops", ".pi/skills/atobar-ops"),
    (".agents/skills/atobar-ops", ".pi/skills/atobar-ops"),
)

_KNOWN_PATH_EXTS: tuple[str, ...] = (
    ".py",
    ".md",
    ".tf",
    ".yml",
    ".yaml",
    ".toml",
    ".sql",
    ".sh",
    ".json",
    ".tpl",
)

_TABLE_BEGIN = "<!-- BEGIN GENERATED COMMAND TABLE -->"
_TABLE_END = "<!-- END GENERATED COMMAND TABLE -->"


def test_ac9_harness_shims_are_symlinks_to_canonical_files() -> None:
    for shim_rel, canonical_rel in REQUIRED_SYMLINK_SHIMS:
        shim_path = REPO_ROOT / shim_rel
        canonical_path = (REPO_ROOT / canonical_rel).resolve()
        assert shim_path.is_symlink(), f"{shim_rel} must be a symlink to {canonical_rel}, not a regular file copy"
        assert shim_path.resolve() == canonical_path, (
            f"{shim_rel} resolves to {shim_path.resolve()}, expected {canonical_path}"
        )
        assert canonical_path.exists(), f"Canonical target {canonical_rel} does not exist"


def test_ac9_root_agents_md_size_and_path_hygiene() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    lines = agents_md.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 92, f"Root AGENTS.md must stay <= 92 lines (got {len(lines)})"
    text = "\n".join(lines)
    assert "atobar-ops verify" in text
    assert "docs/CROSS_SERVICE_CHANGES.md" in text

    cross_doc = REPO_ROOT / "docs" / "CROSS_SERVICE_CHANGES.md"
    assert cross_doc.is_file()
    cross_lines = cross_doc.read_text(encoding="utf-8").splitlines()
    assert len(cross_lines) <= 120, f"docs/CROSS_SERVICE_CHANGES.md must stay <= 120 lines (got {len(cross_lines)})"


def test_wp7_skill_md_command_table_matches_help_markdown_byte_for_byte() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert _TABLE_BEGIN in skill_md and _TABLE_END in skill_md, (
        f"SKILL.md must contain {_TABLE_BEGIN!r} and {_TABLE_END!r} markers"
    )
    embedded = skill_md.split(_TABLE_BEGIN, 1)[1].split(_TABLE_END, 1)[0].strip()
    generated = cli.render_markdown_command_table().strip()
    assert embedded == generated, (
        "SKILL.md command table drifted from `atobar-ops help --markdown`; regenerate with `uv run --no-sync atobar-ops help --markdown`"
    )


def test_wp7_skill_docs_repo_paths_exist_on_disk() -> None:
    missing: list[str] = []
    checked = 0
    for md_path in sorted(SKILL_DIR.rglob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        for match in re.finditer(r"`([^`\n]+)`", text):
            token = match.group(1).strip()
            if "/" not in token or " " in token or not token.endswith(_KNOWN_PATH_EXTS):
                continue
            if token.startswith((".venv/", "~/", "/")) or any(ch in token for ch in ("*", "<", ">", "{", "}")):
                continue
            checked += 1
            repo_candidate = REPO_ROOT / token
            skill_candidate = SKILL_DIR / token
            if not (repo_candidate.exists() or skill_candidate.exists()):
                missing.append(f"{md_path.relative_to(REPO_ROOT)}: {token!r}")
    assert checked >= 8
    assert missing == [], f"Stale/non-existent repo paths in skill docs: {missing}"


def test_wp7_all_uv_run_atobar_ops_invocations_use_no_sync() -> None:
    bare_re = re.compile(r"\buv\s+run\s+(?!--no-sync\b)atobar-ops\b")
    violations: list[str] = []
    for md_path in [REPO_ROOT / "AGENTS.md", REPO_ROOT / "docs" / "AGENTS.md", *sorted(SKILL_DIR.rglob("*.md"))]:
        text = md_path.read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), 1):
            if bare_re.search(line):
                violations.append(f"{md_path.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
    assert violations == [], f"Found `uv run atobar-ops` without `--no-sync`: {violations}"
