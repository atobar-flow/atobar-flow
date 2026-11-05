"""Deterministic Spec-Sufficiency Gate for flow:* issue ingress (pure Python, zero LLM imports)."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

FAST_TRACK_LABELS = frozenset({"flow:feature", "flow:fix"})

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<[^>\n]+>")
_SECTION_HEADER_RE = re.compile(r"^#{2,3}\s+(.*?)\s*$", re.MULTILINE)

# Backticked repo path: either contains a directory slash (e.g. `src/foo.py`, `tests/bar`)
# or ends with a source/config file extension (e.g. `app.py`, `dashboard.tsx`).
_BACKTICK_PATH_RE = re.compile(
    r"`(?:"
    r"(?:src|tests|test|infra|docs|\.github|app|lib|pkg|scripts|frontend|backend|ui|api|cmd|internal)/[A-Za-z0-9_./-]+"
    r"|[A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]+"
    r"|[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|json|yml|yaml|md|sh|toml|sql|css|html|go|rs|tpl)"
    r")`"
)

_CHECKLIST_ITEM_RE = re.compile(r"^\s*[-*]\s+\[\s\]\s+(?!AC-\d+\s*:?\s*$)\S+", re.MULTILINE | re.IGNORECASE)
_AC_ITEM_RE = re.compile(r"\bAC-\d+\b\s*:?\s*[^\s<]", re.IGNORECASE)


class SufficiencyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["developer", "planner"]
    reason: str


def _extract_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s+(.*?)\s*$", line)
        if m:
            current_section = m.group(1).strip().lower()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            sections[current_section].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _strip_placeholders(text: str) -> str:
    cleaned = _PLACEHOLDER_RE.sub("", text)
    lines = []
    for line in cleaned.splitlines():
        stripped = re.sub(r"^[-*]\s*(\[\s\])?\s*(AC-\d+\s*:?)?\s*$", "", line.strip(), flags=re.IGNORECASE)
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()


def _has_scope_anchors(clean_text: str, sections: dict[str, str]) -> bool:
    for header, body in sections.items():
        if header in ("scope", "target files", "files", "files to change", "affected files"):
            if _strip_placeholders(body):
                return True
    if _BACKTICK_PATH_RE.search(clean_text):
        return True
    return False


def _has_acceptance_or_repro(clean_text: str, sections: dict[str, str]) -> bool:
    for header, body in sections.items():
        if header.startswith("acceptance criteria") or header in ("verify", "verification", "test plan"):
            if _strip_placeholders(body):
                return True

    has_repro = any(
        (h.startswith("steps to reproduce") or h.startswith("reproduction") or h == "repro")
        and bool(_strip_placeholders(b))
        for h, b in sections.items()
    )
    has_expected = any(
        h.startswith("expected") and bool(_strip_placeholders(b))
        for h, b in sections.items()
    )
    if has_repro and has_expected:
        return True

    stripped_global = _PLACEHOLDER_RE.sub("", clean_text)
    if _CHECKLIST_ITEM_RE.search(stripped_global) or _AC_ITEM_RE.search(stripped_global):
        return True

    return False


def assess_issue_sufficiency(issue_text: str, label: str) -> SufficiencyDecision:
    """Deterministically classifies whether an issue is ready for fast-track coding or needs Planner decomposition."""
    norm_label = (label or "").strip().lower()
    if norm_label == "flow:plan":
        return SufficiencyDecision(
            route="planner",
            reason="Explicit flow:plan label requires Planner phase.",
        )

    if norm_label not in FAST_TRACK_LABELS:
        return SufficiencyDecision(
            route="planner",
            reason=f"Label {label!r} is not a fast-track implementation label; routing to Planner.",
        )

    clean_text = _HTML_COMMENT_RE.sub("", issue_text or "").strip()
    if not clean_text:
        return SufficiencyDecision(
            route="planner",
            reason="Issue body is empty; routing to Planner for localization and scope definition.",
        )

    sections = _extract_sections(clean_text)
    has_scope = _has_scope_anchors(clean_text, sections)
    has_ac = _has_acceptance_or_repro(clean_text, sections)

    if not has_scope and not has_ac:
        return SufficiencyDecision(
            route="planner",
            reason="Issue lacks both explicit file/scope anchors and acceptance criteria.",
        )
    if not has_scope:
        return SufficiencyDecision(
            route="planner",
            reason="Issue lacks explicit scope section or repo file path anchors.",
        )
    if not has_ac:
        return SufficiencyDecision(
            route="planner",
            reason="Issue lacks explicit acceptance criteria, verification steps, or reproduction steps.",
        )

    return SufficiencyDecision(
        route="developer",
        reason="Issue contains both explicit file/scope anchors and verifiable acceptance criteria.",
    )
