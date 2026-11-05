"""Canonical SotaWiki schema, Mermaid diagram validation, and storage-row normalization."""

import re
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator

WikiRefreshStatus = Literal["idle", "queued", "running", "failed"]
WIKI_REFRESH_STATUSES: tuple[str, ...] = ("idle", "queued", "running", "failed")
DEFAULT_WIKI_REFRESH_INTERVAL_MERGES = 5

MERMAID_HEADERS: tuple[str, ...] = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "C4Context",
    "C4Container",
    "C4Component",
    "C4Dynamic",
    "C4Deployment",
    "mindmap",
    "timeline",
    "journey",
    "gantt",
    "pie",
    "gitGraph",
    "quadrantChart",
    "requirementDiagram",
    "block-beta",
    "architecture-beta",
)

_OUTER_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL)
_UNQUOTED_RECT_LABEL_RE = re.compile(r"\b[A-Za-z0-9_]+\[(?![\"(\[/\\])([^\]]+)\]")


def strip_mermaid_fences(raw: str) -> str:
    """Strips accidental outer markdown code fences and any residual triple-backticks."""
    text = (raw or "").strip()
    match = _OUTER_FENCE_RE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    elif text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text.replace("```", "").strip()


def validate_mermaid_diagram(raw: str) -> str:
    """Validates that `raw` is a non-empty Mermaid diagram with a valid declaration and body."""
    cleaned = strip_mermaid_fences(raw)
    if not cleaned:
        raise ValueError("architecture_mermaid must not be empty")
    active_lines = [
        ln.strip()
        for ln in cleaned.splitlines()
        if ln.strip() and not ln.strip().startswith("%%")
    ]
    if not active_lines:
        raise ValueError("architecture_mermaid must contain a Mermaid diagram")
    first_line = active_lines[0]
    if not any(first_line == hdr or first_line.startswith(hdr + " ") or first_line.startswith(hdr + "\t") or first_line.startswith(hdr + ";") for hdr in MERMAID_HEADERS):
        raise ValueError(
            f"architecture_mermaid must start with a valid Mermaid diagram declaration "
            f"(e.g. 'flowchart TD' or 'sequenceDiagram'); got {first_line[:60]!r}"
        )
    if len(active_lines) < 2 and ";" not in first_line:
        raise ValueError("architecture_mermaid must include at least one node or edge definition after the header")
    if first_line.startswith(("flowchart", "graph")):
        subgraph_count = 0
        end_count = 0
        for ln in active_lines[1:]:
            if ln == "subgraph" or ln.startswith(("subgraph ", "subgraph\t")):
                subgraph_count += 1
                rest = ln[len("subgraph") :].strip()
                if '"' not in rest and any(ch in rest for ch in "(){}"):
                    raise ValueError(
                        "architecture_mermaid subgraph title with parentheses or braces must be quoted "
                        '(e.g. subgraph id["Title (detail)"])'
                    )
            elif ln == "end" or ln.startswith(("end ", "end\t", "end;")):
                end_count += 1
            for match in _UNQUOTED_RECT_LABEL_RE.finditer(ln):
                label = match.group(1)
                if any(ch in label for ch in "(){}[]"):
                    raise ValueError(
                        "architecture_mermaid node label with parentheses or braces inside [...] must be "
                        f'double-quoted (e.g. A["{label}"]); got {match.group(0)!r}'
                    )
        if subgraph_count != end_count:
            raise ValueError(
                f"architecture_mermaid has unbalanced subgraph/end blocks ({subgraph_count} subgraph vs {end_count} end)"
            )
    elif first_line.startswith("erDiagram"):
        body = re.sub(r"(?:[o|]\{|\}[o|])", "", "\n".join(active_lines[1:]))
        if body.count("{") != body.count("}"):
            raise ValueError("architecture_mermaid erDiagram has unbalanced braces")
    return cleaned


class SotaWiki(BaseModel):
    """Canonical repository architecture wiki synthesized by the ONBOARDER role."""

    purpose: str = Field(min_length=1, description="Purpose of the repository")
    technologies: list[str] = Field(
        validation_alias=AliasChoices("technologies", "key_technologies"),
        description="List of key technologies",
    )
    structure: str = Field(
        min_length=1,
        validation_alias=AliasChoices("structure", "top_level_structure"),
        description="Description of top-level directory structure and module responsibilities",
    )
    concepts: list[str] = Field(
        validation_alias=AliasChoices("concepts", "key_concepts"),
        description="List of key architectural concepts and invariants",
    )
    architecture_mermaid: str = Field(
        min_length=1,
        description="Valid Mermaid architectural diagram source",
    )
    commit_sha: str = Field(
        default="",
        description="Git commit SHA the wiki was synthesized from",
    )

    @field_validator("architecture_mermaid", mode="before")
    @classmethod
    def _validate_mermaid(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("architecture_mermaid must be a string")
        return validate_mermaid_diagram(v)

    @property
    def key_technologies(self) -> list[str]:
        return self.technologies

    @property
    def top_level_structure(self) -> str:
        return self.structure

    @property
    def key_concepts(self) -> list[str]:
        return self.concepts

    def to_storage_row(self, repo_name: str) -> dict[str, Any]:
        return {
            "repo_name": repo_name,
            "purpose": self.purpose,
            "technologies": list(self.technologies),
            "structure": self.structure,
            "concepts": list(self.concepts),
            "architecture_mermaid": self.architecture_mermaid,
            "commit_sha": self.commit_sha,
        }


def normalize_wiki_row(
    row: dict[str, Any],
    *,
    tenant_id: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> dict[str, Any]:
    """Normalizes a `repository_wikis` row with canonical keys and lifecycle defaults."""
    raw_tech = row.get("technologies") if row.get("technologies") is not None else row.get("key_technologies")
    raw_concepts = row.get("concepts") if row.get("concepts") is not None else row.get("key_concepts")
    raw_struct = row.get("structure") if row.get("structure") is not None else row.get("top_level_structure")
    raw_status = str(row.get("refresh_status") or "idle").strip().lower()
    status_val = raw_status if raw_status in WIKI_REFRESH_STATUSES else "idle"
    raw_interval = row.get("refresh_interval_merges")
    interval_val = int(raw_interval) if raw_interval is not None else DEFAULT_WIKI_REFRESH_INTERVAL_MERGES
    out: dict[str, Any] = {
        "tenant_id": str(tenant_id or row.get("tenant_id") or ""),
        "repo_name": str(repo_name or row.get("repo_name") or ""),
        "purpose": str(row.get("purpose") or ""),
        "technologies": list(raw_tech) if isinstance(raw_tech, list) else [],
        "structure": raw_struct if isinstance(raw_struct, dict) else str(raw_struct or ""),
        "concepts": list(raw_concepts) if isinstance(raw_concepts, list) else [],
        "architecture_mermaid": strip_mermaid_fences(str(row.get("architecture_mermaid") or "")),
        "commit_sha": str(row.get("commit_sha") or ""),
        "refresh_status": status_val,
        "refresh_queued": bool(row.get("refresh_queued", False)),
        "refresh_run_id": str(row["refresh_run_id"]) if row.get("refresh_run_id") else None,
        "refresh_started_at": str(row["refresh_started_at"]) if row.get("refresh_started_at") else None,
        "last_error": str(row["last_error"]) if row.get("last_error") else None,
        "last_observed_sha": str(row.get("last_observed_sha") or row.get("commit_sha") or ""),
        "merges_since_refresh": max(0, int(row.get("merges_since_refresh") or 0)),
        "refresh_interval_merges": max(0, min(100, interval_val)),
        "last_updated": str(row.get("last_updated") or ""),
    }
    return out
