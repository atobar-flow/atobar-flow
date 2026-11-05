"""Deterministic repository context scanner and SotaWiki synthesizer."""

import json
import os
import pathlib
import re
import subprocess
from typing import Optional

import httpx
from pydantic import ValidationError

from atobar_contracts.wiki import SotaWiki
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.llm.factory import get_client
from flow_agent.llm.protocol import LLMClient, Message

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_EXCLUDED_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})


def upsert_wiki_to_control_plane(repo_name: str, wiki: SotaWiki) -> bool:
    """Publish structured SotaWiki to the Control Plane using the active run credential."""
    cp_url = (os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL") or "").rstrip("/")
    run_id = os.environ.get("FLOW_RUN_ID", "")
    run_cred = os.environ.get("FLOW_RUN_CREDENTIAL", "")
    if not (cp_url and run_id and run_cred):
        return False
    row = wiki.to_storage_row(repo_name)
    try:
        resp = httpx.post(
            f"{cp_url}/v1/runs/{run_id}/wiki",
            headers={"Authorization": f"Bearer {run_cred}"},
            json=row,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


def _fallback_tree(root: pathlib.Path, max_depth: int = 3) -> str:
    """Fast pure-Python directory listing fallback up to `max_depth` without spawning extra subprocesses."""
    rel_paths: list[str] = []

    def _walk(curr: pathlib.Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(curr.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            if entry.name in _EXCLUDED_DIRS:
                continue
            try:
                rel = str(entry.relative_to(root))
            except ValueError:
                continue
            if entry.is_dir():
                _walk(entry, depth + 1)
            else:
                rel_paths.append(rel)

    _walk(root, 1)
    return "\n".join(rel_paths)


def _gather_context(repo_path: str) -> tuple[str, str, str, str, str]:
    """Single-pass repository context gatherer returning `(head_sha, readme, agents_md, tree, deps)`."""
    root = pathlib.Path(repo_path)

    head_sha = ""
    try:
        git_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        cand = git_res.stdout.strip().lower()
        if git_res.returncode == 0 and _SHA_RE.fullmatch(cand):
            head_sha = cand
    except OSError:
        head_sha = ""

    readme_content = ""
    readme_path = root / "README.md"
    if readme_path.is_file():
        readme_content = readme_path.read_text(encoding="utf-8", errors="ignore")[:10000]

    agents_parts: list[str] = []
    for doc_rel in ("AGENTS.md", "CLAUDE.md", "docs/AGENTS.md"):
        doc_path = root / doc_rel
        if doc_path.is_file():
            agents_parts.append(f"--- {doc_rel} ---\n{doc_path.read_text(encoding='utf-8', errors='ignore')[:3000]}")
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        doc_files = sorted(p.name for p in docs_dir.glob("*.md"))[:30]
        if doc_files:
            agents_parts.append("--- docs/*.md ---\n" + "\n".join(doc_files))
    agents_content = "\n".join(agents_parts)[:5000]

    try:
        rg_cmd = [
            "rg",
            "--files",
            "--max-depth",
            "3",
            "-g",
            "!node_modules",
            "-g",
            "!.git",
            "-g",
            "!.venv",
            "-g",
            "!__pycache__",
        ]
        tree_output = subprocess.check_output(rg_cmd, cwd=repo_path, text=True, stderr=subprocess.DEVNULL)[:10000]
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        tree_output = _fallback_tree(root, max_depth=3)[:10000]

    deps_content = ""
    for dep_file in ("requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"):
        p = root / dep_file
        if p.is_file():
            deps_content += f"\n--- {dep_file} ---\n{p.read_text(encoding='utf-8', errors='ignore')[:5000]}\n"
    return head_sha, readme_content, agents_content, tree_output, deps_content


def _llm_from_env() -> LLMClient:
    """The model the Control Panel delivered for the current role (`export_llm_keys`); there is no default."""
    provider = os.environ.get("FLOW_LLM_PROVIDER", "")
    model = os.environ.get("FLOW_ROLE_MODEL", "")
    api_key = os.environ.get("FLOW_LLM_API_KEY", "")
    if not (provider and model and api_key):
        raise RuntimeError("No LLM delivered for this role; configure its model in the Control Panel")
    return get_client(provider, model, api_key)


_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\n(.*?)\n```", re.DOTALL)


def _json_candidates(text: str) -> list[str]:
    """`json`-tagged fences anywhere in the reply first, then untagged ones (last first), then the bare reply."""
    fences = list(_FENCE_RE.finditer(text or ""))
    tagged = [m.group(2) for m in reversed(fences) if m.group(1).lower() == "json"]
    untagged = [m.group(2) for m in reversed(fences) if not m.group(1)]
    return tagged + untagged + [(text or "").strip()]


def _parse_wiki(text: str) -> SotaWiki:
    last: Exception = ValueError("Received an empty response from the model.")
    for raw in _json_candidates(text):
        try:
            return SotaWiki.model_validate_json(raw)
        except (ValidationError, ValueError) as e:
            last = e
    raise last


WIKI_ARCHITECT_SYSTEM_PROMPT = (
    "You are an expert AI software architect. Synthesize high-signal repository architecture context "
    "and a valid Mermaid diagram for downstream autonomous engineering agents."
)


def synthesize_wiki(
    repo_path: str,
    repo_name: str,
    llm: Optional[LLMClient] = None,
    max_retries: int = 3,
) -> SotaWiki:
    """Deterministic context gathering + schema-validated synthesis with the CP-configured model. Raises on failure."""
    head_sha, readme_content, agents_content, tree_output, deps_content = _gather_context(repo_path)
    client = llm or _llm_from_env()
    wrapped_readme = wrap_untrusted(readme_content, source="readme")
    wrapped_agents = wrap_untrusted(agents_content, source="agents_docs")
    wrapped_tree = wrap_untrusted(tree_output, source="tree")
    wrapped_deps = wrap_untrusted(deps_content, source="deps")
    prompt = f"""Analyze the following repository context and reply with ONLY a JSON object with these keys:
- "purpose" (string): Concise summary of the repository's core domain and purpose.
- "technologies" (list of strings): Primary languages, frameworks, data stores, and libraries.
- "structure" (string): Top-level directory and module map explaining where entrypoints, domain logic, state stores, and tests live.
- "concepts" (list of strings): Key architectural invariants, design patterns, and domain concepts.
- "architecture_mermaid" (string): A valid Mermaid diagram (start with `flowchart TD`, `flowchart LR`, or `sequenceDiagram`, followed by node and edge lines) depicting entrypoints, module/service boundaries, state stores, and data/control flow. Do NOT wrap the Mermaid string in markdown backtick fences.

Repository Name: {repo_name}

--- README.md ---
{wrapped_readme}

--- AGENTS / Architecture Docs ---
{wrapped_agents}

--- Directory Tree ---
{wrapped_tree}

--- Dependencies ---
{wrapped_deps}
"""
    messages = [Message(role="user", content=prompt)]
    last_error: Exception = RuntimeError("no attempts made")
    for _ in range(max_retries):
        reply = client.chat(list(messages), system_instruction=WIKI_ARCHITECT_SYSTEM_PROMPT)
        text = reply.text or ""
        try:
            if not text.strip():
                raise ValueError("Received an empty response from the model.")
            wiki = _parse_wiki(text)
            if head_sha:
                wiki = wiki.model_copy(update={"commit_sha": head_sha})
            return wiki
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = e
            messages.append(Message(role="assistant", content=text))
            messages.append(Message(role="user", content=f"That failed validation:\n{e}\nReply with corrected JSON only."))
    raise RuntimeError(f"Max retries exceeded for schema validation: {last_error}")


__all__ = [
    "SotaWiki",
    "WIKI_ARCHITECT_SYSTEM_PROMPT",
    "_gather_context",
    "synthesize_wiki",
    "upsert_wiki_to_control_plane",
]
