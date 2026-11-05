import os
import pathlib
import re
from typing import List, Optional, TypedDict

_OVERFLOW_LOG_RE = re.compile(r"^/tmp/overflow_[A-Za-z0-9_-]+\.log$")
_ALLOWED_OVERFLOW_PATHS: set[str] = set()


def register_overflow_path(path: str) -> str:
    """Records an overflow log file created by the current run so read_file/search_codebase can read it."""
    _ALLOWED_OVERFLOW_PATHS.add(path)
    _ALLOWED_OVERFLOW_PATHS.add(os.path.realpath(path))
    return path


class EditDict(TypedDict):
    old_string: str
    new_string: str


def _resolve_path_in_workspace(
    file_path: str,
    workspace_dir: Optional[str] = None,
    allow_overflow: bool = True,
) -> str:
    if allow_overflow and _OVERFLOW_LOG_RE.fullmatch(file_path):
        tmp_real = os.path.realpath("/tmp")
        overflow_real = os.path.realpath(file_path)
        run_id = re.sub(r"[^A-Za-z0-9_-]", "_", os.environ.get("FLOW_RUN_ID", "").strip())
        owned_by_run = (
            file_path in _ALLOWED_OVERFLOW_PATHS
            or overflow_real in _ALLOWED_OVERFLOW_PATHS
            or bool(run_id and f"_{run_id}_" in os.path.basename(file_path))
        )
        if owned_by_run and overflow_real == os.path.join(tmp_real, os.path.basename(file_path)):
            return overflow_real
        raise PermissionError(f"Path escapes workspace boundary ({file_path} -> {overflow_real})")

    ws = (workspace_dir if workspace_dir is not None else os.environ.get("WORKSPACE_DIR", "")).strip()
    if ws and (workspace_dir is not None or os.path.isdir(ws)):
        ws_real = os.path.realpath(ws)
        candidate = file_path if os.path.isabs(file_path) else os.path.join(ws, file_path)
        real = os.path.realpath(candidate)
        try:
            common = os.path.commonpath([ws_real, real])
        except ValueError as exc:
            raise PermissionError(f"Path escapes workspace boundary ({file_path} -> {real})") from exc
        if common != ws_real:
            raise PermissionError(f"Path escapes workspace boundary ({file_path} -> {real})")
        return real

    parts = pathlib.PurePath(file_path).parts
    if ".." in parts:
        raise PermissionError(f"Path escapes workspace boundary: {file_path}")
    real_unscoped = os.path.realpath(file_path)
    for forbidden_prefix in ("/proc", "/sys", "/dev/shm", "/etc", "/home/ubuntu/agent.log"):
        if real_unscoped == forbidden_prefix or real_unscoped.startswith(forbidden_prefix + "/"):
            raise PermissionError(f"Path escapes workspace boundary ({file_path} -> {real_unscoped})")
    if not os.path.isabs(file_path):
        cwd_real = os.path.realpath(os.getcwd())
        if os.path.commonpath([cwd_real, real_unscoped]) != cwd_real:
            raise PermissionError(f"Path escapes workspace boundary ({file_path} -> {real_unscoped})")
    return real_unscoped


def multi_edit(file_path: str, edits: List[EditDict]) -> str:
    """
    Applies exact string replacements to a file using an Optimistic Lock.

    This function adheres to SOTA'26 Axiom 1 (File Editing). Diff-based patching and
    AST manipulations are prohibited as they are fragile when confronted with LLM hallucinations.
    Instead, this tool requires the agent to pass the exact `old_string` it wants to replace.
    If the `old_string` is not globally unique within the file, the tool rejects the edit
    entirely to prevent code corruption.
    """
    file_path = _resolve_path_in_workspace(file_path, allow_overflow=False)
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    for edit in edits:
        old_str, new_str = edit['old_string'], edit['new_string']

        # --- The Optimistic Lock ---
        # We count occurrences to ensure the LLM has provided enough context to uniquely
        # identify the block of code it wants to change. This prevents accidental changes
        # to similar lines of code elsewhere in the file.
        occurrences = content.count(old_str)
        if occurrences == 0:
            raise Exception("Edit Failed: old_string not found in file. Ensure exact whitespace and indentation match.")
        if occurrences > 1:
            raise Exception(
                f"Edit Failed: old_string is not unique. Found {occurrences} times. "
                "Provide more surrounding context to make it globally unique."
            )

        # Apply the replacement sequentially. Since we confirmed uniqueness, this is safe.
        content = content.replace(old_str, new_str)

    # Atomic save: We only write to disk if ALL edits in the list passed the lock.
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return f"SUCCESS: {len(edits)} edits applied atomically."

def read_file(file_path: str, offset_line: int = 1, limit_lines: int = 1000) -> str:
    """
    Reads a file with windowing support to prevent LLM context bloat.

    Adheres to SOTA'26 Axiom 4 (Context Ingestion). Large files (e.g., bundled JS or large
    configuration files) will blow out the token context. This function forces the agent to
    read files in chunks.
    """
    current_role = os.environ.get("FLOW_ROLE", "").strip().lower()
    if current_role in ("evaluator", "planner"):
        normalized = file_path.replace("\\", "/")
        if (
            "trace.jsonl" in normalized
            or ("developer" in normalized and ".atobar" in normalized)
            or "/worktrees/developer" in normalized
        ):
            raise PermissionError("Evaluator cannot access developer traces")

    file_path = _resolve_path_in_workspace(file_path, allow_overflow=True)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    total_lines = len(lines)

    # Calculate start and end indices based on 1-indexed offset_line
    start_index = max(0, offset_line - 1)
    end_index = start_index + limit_lines

    # Extract the requested window of lines
    content = "".join(lines[start_index:end_index])

    # If there are more lines beyond the limit, append a truncation notice
    if total_lines > end_index:
        content += "\n<notice> File truncated. Use offset_line to read further.</notice>"

    return content

def write_file(file_path: str, content: str) -> str:
    """
    Creates a new file with the provided content.

    This tool is strictly for creating NEW files. If the file already exists, it forces
    the agent to use the safer `multi_edit` tool, maintaining the integrity of existing code.
    """
    file_path = _resolve_path_in_workspace(file_path, allow_overflow=False)
    if os.path.exists(file_path):
        raise Exception("Edit Failed: File already exists. Use multi_edit to modify existing files.")

    # Ensure parent directories exist before writing
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return f"SUCCESS: File created at {file_path}"
