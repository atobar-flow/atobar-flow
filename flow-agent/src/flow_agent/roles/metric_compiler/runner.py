"""Execution entrypoint for the METRIC_COMPILER role (#301 WP-6).

The LLM only proposes a `Metric` spec. The measured value is produced by this runner executing the
proposed command on the default-branch checkout, so the dry-run reported to the Flow Engine never
depends on what the model claims it measured.
"""

import asyncio
import json
import math
import os
import re
import signal
import subprocess
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from atobar_contracts.manifest import Metric, validate_compiled_metric_command
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.knowledge.onboard_repo import _gather_context
from flow_agent.llm.factory import get_client
from flow_agent.llm.protocol import LLMClient, Message
from flow_agent.roles.base import TurnOutcome, format_wiki_section
from flow_agent.roles.metric_compiler.prompt import METRIC_COMPILER_SYSTEM_PROMPT
from flow_agent.runtime.key_delivery import fetch_run_llm_key, require_model
from flow_agent.telemetry.event_sink import emit
from flow_agent.telemetry.session_registry import SessionRegistry

_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\n(.*?)\n```", re.DOTALL)
_SECRET_ENV_RE = re.compile(r"(TOKEN|SECRET|CREDENTIAL|PASSWORD|API_KEY|PRIVATE_KEY)", re.IGNORECASE)
STDOUT_TAIL_CHARS = 2000


class MetricCompilationError(RuntimeError):
    """The metric could not be compiled into a dry-run-verified spec."""


def _json_candidates(text: str) -> list[str]:
    """`json`-tagged fences first, then untagged ones (last first), then the bare reply."""
    fences = list(_FENCE_RE.finditer(text))
    tagged = [m.group(2) for m in reversed(fences) if m.group(1).lower() == "json"]
    untagged = [m.group(2) for m in reversed(fences) if not m.group(1)]
    return tagged + untagged + [text.strip()]


def parse_compiled_metric(text: str, key: str) -> dict[str, Any]:
    """Extracts and validates the model's proposed Metric spec; the key is always the bound one."""
    data: Any = None
    for raw in _json_candidates(text or ""):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            break
    if not isinstance(data, dict):
        raise MetricCompilationError("reply contains no JSON object")
    data = {k: v for k, v in data.items() if v is not None and k != "key"}
    data["key"] = key
    try:
        metric = Metric.model_validate(data)
    except ValidationError as exc:
        raise MetricCompilationError(f"invalid metric spec: {exc}") from exc
    if not (metric.run or "").strip():
        raise MetricCompilationError("metric spec has an empty `run` command")
    if not math.isfinite(metric.threshold):
        raise MetricCompilationError("threshold must be finite")
    if metric.tolerance_pct is not None and not (math.isfinite(metric.tolerance_pct) and metric.tolerance_pct >= 0):
        raise MetricCompilationError("tolerance_pct must be a finite number >= 0")
    return metric.model_dump(exclude_none=True, exclude={"source"})


def _scrubbed_env() -> dict[str, str]:
    """The dry-run executes model-authored shell: never hand it run credentials or LLM/GitHub keys."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def _git(workspace: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=False)


def _restore_worktree(workspace: str) -> list[str]:
    """Reverts tracked changes and deletes new untracked files; returns the tracked paths it reverted.

    Ignored files (setup outputs such as `.venv/`) and `.atobar/` are kept so retries don't redo setup.
    """
    status = _git(workspace, "status", "--porcelain", "--untracked-files=no").stdout
    dirty = [line[3:] for line in status.splitlines() if line.strip()]
    if dirty:
        _git(workspace, "checkout", "--force", "HEAD", "--", ".")
    _git(workspace, "clean", "-fd", "--exclude=.atobar")
    return dirty


def dry_run_metric(run_cmd: str, workspace: str, timeout_s: float = 300.0) -> tuple[float, str]:
    """Runs the command like the generated flow-metrics.yml step (`bash -o pipefail`, last stdout line).

    The command runs in its own process group so a timeout kills every child, and the worktree is restored
    afterwards: a command that modified tracked files is rejected as not read-only.
    """
    proc = subprocess.Popen(
        ["bash", "-o", "pipefail", "-c", run_cmd],
        cwd=workspace,
        env=_scrubbed_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise MetricCompilationError(f"command timed out after {timeout_s:g}s") from exc
    finally:
        mutated = _restore_worktree(workspace)
    if mutated:
        raise MetricCompilationError(f"command modified tracked files (must be read-only): {', '.join(mutated[:5])}")
    tail = ((stdout or "") + (stderr or ""))[-STDOUT_TAIL_CHARS:]
    if proc.returncode != 0:
        raise MetricCompilationError(f"command exited {proc.returncode}: {tail[-500:]}")
    lines = (stdout or "").splitlines()
    last = lines[-1].strip() if lines else ""
    try:
        value = float(last)
    except ValueError as exc:
        raise MetricCompilationError(f"last stdout line {last[:80]!r} is not a number") from exc
    if not math.isfinite(value):
        raise MetricCompilationError(f"last stdout line {last[:80]!r} is not finite")
    return value, tail


def _head_sha(workspace: str) -> str:
    res = _git(workspace, "rev-parse", "HEAD")
    sha = res.stdout.strip()
    if res.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise MetricCompilationError(f"cannot resolve the checkout commit in {workspace}")
    return sha


def _build_context(tenant: str, key: str, workspace: str, wiki: Optional[dict[str, Any]]) -> str:
    _head_sha_val, readme, _agents_md, tree, deps = _gather_context(workspace)
    sections = [f"TENANT: {tenant}\nMETRIC KEY: {key}"]
    if wiki:
        sections.append(format_wiki_section(wiki))
    sections.append(
        wrap_untrusted(f"README.md:\n{readme}\n\nFILES:\n{tree[:6000]}\n\nDEPENDENCIES:\n{deps}", source="repository")
    )
    return "\n\n".join(sections)


async def run_metric_compiler(
    tenant: str,
    key: str,
    intent: str,
    workspace: str,
    wiki: Optional[dict[str, Any]] = None,
    protected_paths: Optional[list[str]] = None,
    registry: Optional[SessionRegistry] = None,
    llm: Optional[LLMClient] = None,
    max_attempts: int = 3,
    dry_run_timeout_s: float = 300.0,
) -> TurnOutcome:
    """Compiles `intent` for metric `key`, dry-runs it on the checkout and submits it to the Control Panel."""
    cp_url = (os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_URL") or "").rstrip("/")
    run_id = os.environ.get("FLOW_RUN_ID", "")
    cred = os.environ.get("FLOW_RUN_CREDENTIAL", "")
    if not (cp_url and run_id and cred):
        raise MetricCompilationError("missing CONTROL_PANEL_URL / FLOW_RUN_ID / FLOW_RUN_CREDENTIAL")
    if not key or not intent.strip():
        raise MetricCompilationError("missing metric key or intent for this compiler run")

    if llm is None:
        key_info = await asyncio.to_thread(fetch_run_llm_key, cp_url, run_id, cred)
        llm = get_client(key_info.provider, require_model(key_info), key_info.api_key)

    sha = await asyncio.to_thread(_head_sha, workspace)
    context = await asyncio.to_thread(_build_context, tenant, key, workspace, wiki)
    messages = [
        Message(role="user", content=context),
        Message(role="user", content=wrap_untrusted(intent, source="metric_intent")),
    ]
    last_error = "no attempts made"
    for attempt in range(1, max_attempts + 1):
        if registry is not None:
            await registry.heartbeat(current_todo=f"compile:{key} attempt {attempt}")
        reply = await asyncio.to_thread(llm.chat, list(messages), None, METRIC_COMPILER_SYSTEM_PROMPT)
        text = reply.text or ""
        messages.append(Message(role="assistant", content=text))
        try:
            compiled = parse_compiled_metric(text, key)
            problems = validate_compiled_metric_command(compiled["run"], protected_paths=protected_paths)
            if problems:
                raise MetricCompilationError("; ".join(problems))
            if registry is not None:
                await registry.heartbeat(current_todo=f"compile:{key} dry-run {attempt}")
            value, tail = await asyncio.to_thread(dry_run_metric, compiled["run"], workspace, dry_run_timeout_s)
            resp = await asyncio.to_thread(
                httpx.post,
                f"{cp_url}/v1/runs/{run_id}/metric-compilation",
                headers={"Authorization": f"Bearer {cred}"},
                json={"key": key, "compiled": compiled, "value": value, "sha": sha, "stdout_tail": tail},
                timeout=30.0,
            )
            if resp.status_code == 422:
                raise MetricCompilationError(f"rejected by the Flow Engine: {resp.text[:500]}")
            resp.raise_for_status()
        except MetricCompilationError as exc:
            last_error = str(exc)
            emit("metric_compiler.attempt", key=key, attempt=attempt, ok=False, error=last_error[:500])
            messages.append(
                Message(role="user", content=f"Attempt {attempt} failed: {last_error}\nReply with a corrected JSON block.")
            )
            continue
        emit("metric_compiler.attempt", key=key, attempt=attempt, ok=True, value=value, sha=sha)
        return TurnOutcome(envelope_text="", head_sha=sha)
    raise MetricCompilationError(f"metric '{key}' not compiled after {max_attempts} attempts: {last_error}")
