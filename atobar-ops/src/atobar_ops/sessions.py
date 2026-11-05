"""Read-only session transcript auditor (`atobar-ops sessions audit`) for J47 / #359 WP-8."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any

from . import output
from .timebox import EXIT_ERROR, EXIT_OK

_WHOLE_FILE_END = 1_000_000_000

_SLEEP_RE = re.compile(r"\bsleep\s+(\d+(?:\.\d+)?)\b")
_SEQ_LOOP_RE = re.compile(r"\bfor\s+\w+\s+in\s+(?:\$\(\s*seq\s+(?:\d+\s+)?(\d+)\s*\)|\{1\.\.(\d+)\})")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\b", re.DOTALL)
_GH_PR_COMMENTS_RE = re.compile(r"\bgh\s+api\b[^\n]*?/pulls/\d+/comments\b")
_DB_SQL_RE = re.compile(r"\batobar-ops\s+db\s+sql\b")
_SHIP_CMD_RE = re.compile(r"\batobar-ops\s+ship\b")
_CODEMODE_READ_RE = re.compile(
    r'tools\.read\(\s*\{\s*[^}]*?\bpath\s*:\s*["\']([^"\']+)["\']'
    r'(?:[^}]*?\boffset\s*:\s*(\d+|null))?'
    r'(?:[^}]*?\blimit\s*:\s*(\d+|null))?',
    re.DOTALL,
)
_CODEMODE_MUTATE_RE = re.compile(
    r'tools\.(?:edit|write)\(\s*\{\s*[^}]*?\bpath\s*:\s*["\']([^"\']+)["\']',
    re.DOTALL,
)
_CODEMODE_TOOL_CALL_RE = re.compile(r"\btools\.(read|bash|edit|write)\s*\(")


def pi_sessions_dir(cwd: Path | None = None) -> Path:
    resolved = (cwd or Path.cwd()).resolve()
    slug = "--" + str(resolved).strip("/").replace("/", "-") + "--"
    return Path.home() / ".pi" / "agent" / "sessions" / slug


def claude_sessions_dir(cwd: Path | None = None) -> Path:
    resolved = (cwd or Path.cwd()).resolve()
    slug = "-" + str(resolved).strip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug


@dataclass
class SessionMetrics:
    session: str
    harness: str = "pi"
    turns: int = 0
    total_tokens: int = 0
    cache_read: int = 0
    uncached_in: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    cold_turns: int = 0
    cold_cost_usd: float = 0.0
    tool_errors: int = 0
    single_op_reads: int = 0
    repeat_reads: int = 0
    long_sleeps: int = 0
    raw_gh_comments: int = 0
    failed_db_sql: int = 0
    ship_refusals: int = 0
    read_skill_md: bool = False
    malformed_lines: int = 0


def _extract_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _to_pos_int(val: Any) -> int | None:
    if isinstance(val, int) and val > 0:
        return val
    if isinstance(val, str) and val.isdigit() and int(val) > 0:
        return int(val)
    return None


def _record_read(
    path: str,
    offset: Any,
    limit: Any,
    seen_intervals: dict[str, list[tuple[int, int]]],
    metrics: SessionMetrics,
) -> None:
    if not path:
        return
    if path.endswith("SKILL.md"):
        metrics.read_skill_md = True
    start = _to_pos_int(offset) or 1
    lim = _to_pos_int(limit)
    end = (start + lim - 1) if lim is not None else _WHOLE_FILE_END
    intervals = seen_intervals.setdefault(path, [])
    if any(max(start, x) <= min(end, y) for x, y in intervals):
        metrics.repeat_reads += 1
    intervals.append((start, end))


def _record_mutation(path: str, seen_intervals: dict[str, list[tuple[int, int]]]) -> None:
    if path:
        seen_intervals.pop(path, None)


def _inspect_bash_command(cmd: str, metrics: SessionMetrics) -> dict[str, bool]:
    flags = {"is_db_sql": False, "is_ship": False}
    if not cmd:
        return flags
    stripped = _HEREDOC_RE.sub("", cmd)
    loop_mult = 1
    m_loop = _SEQ_LOOP_RE.search(stripped)
    if m_loop:
        try:
            loop_mult = max(1, int(m_loop.group(1) or m_loop.group(2) or 1))
        except ValueError:
            loop_mult = 1
    total_sleep = 0.0
    for m in _SLEEP_RE.finditer(stripped):
        try:
            total_sleep += float(m.group(1))
        except ValueError:
            pass
    if total_sleep * loop_mult > 240.0:
        metrics.long_sleeps += 1
    if _GH_PR_COMMENTS_RE.search(stripped):
        metrics.raw_gh_comments += 1
    if _DB_SQL_RE.search(stripped):
        flags["is_db_sql"] = True
    if _SHIP_CMD_RE.search(stripped):
        flags["is_ship"] = True
    return flags


def audit_transcript_file(path: Path, *, default_harness: str = "pi") -> SessionMetrics:
    """Audits a single Pi or Claude Code JSONL session file without emitting prompt/tool content."""
    stem = path.stem
    session_label = stem.split("_", 1)[0][:19] if "_" in stem else stem[:19]
    metrics = SessionMetrics(session=session_label, harness=default_harness)
    seen_intervals: dict[str, list[tuple[int, int]]] = {}
    tool_call_meta: dict[str, dict[str, bool]] = {}
    had_warm_cache = False

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        metrics.malformed_lines += 1
        return metrics

    for raw_line in raw_line_iter(raw_text):
        try:
            obj = json.loads(raw_line)
        except Exception:
            metrics.malformed_lines += 1
            continue
        if not isinstance(obj, dict):
            continue

        if not re.match(r"^\d{4}-\d{2}-\d{2}", metrics.session):
            ts_raw = str(obj.get("timestamp") or "")[:19].replace(":", "-")
            if re.match(r"^\d{4}-\d{2}-\d{2}", ts_raw):
                metrics.session = ts_raw

        top_type = str(obj.get("type") or "")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
        if top_type in ("assistant", "user") and msg is not None:
            metrics.harness = "claude"
        elif top_type != "message" or msg is None:
            continue

        role = str(msg.get("role") or top_type)

        if role == "assistant":
            metrics.turns += 1
            usage = msg.get("usage")
            if isinstance(usage, dict):
                # Support both Pi (totalTokens/cacheRead/input/output) and Claude Code (*_tokens)
                cache_r = int(usage.get("cacheRead") or usage.get("cache_read_input_tokens") or 0)
                cache_w = int(usage.get("cacheWrite") or usage.get("cache_creation_input_tokens") or 0)
                uncached = int(usage.get("input") or usage.get("input_tokens") or 0)
                out_tok = int(usage.get("output") or usage.get("output_tokens") or 0)
                tot_tok = int(usage.get("totalTokens") or (cache_r + cache_w + uncached + out_tok))
                cost_obj = usage.get("cost")
                turn_cost = float(cost_obj.get("total") or 0.0) if isinstance(cost_obj, dict) else 0.0

                metrics.total_tokens += tot_tok
                metrics.cache_read += cache_r
                metrics.uncached_in += uncached
                metrics.out_tokens += out_tok
                metrics.cost_usd += turn_cost

                if had_warm_cache and cache_r == 0 and uncached > 50_000:
                    metrics.cold_turns += 1
                    metrics.cold_cost_usd += turn_cost
                if cache_r > 0:
                    had_warm_cache = True

            tool_calls = [
                c
                for c in (msg.get("content") or [])
                if isinstance(c, dict) and c.get("type") in ("toolCall", "tool_use")
            ]
            if len(tool_calls) == 1:
                tc = tool_calls[0]
                tname = str(tc.get("name") or "").lower()
                raw_args = tc.get("arguments") if "arguments" in tc else tc.get("input")
                tc_args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
                if tname == "read":
                    metrics.single_op_reads += 1
                elif tname == "codemode":
                    code = str(tc_args.get("code") or "")
                    inner = _CODEMODE_TOOL_CALL_RE.findall(code)
                    if inner == ["read"]:
                        metrics.single_op_reads += 1

            for tc in tool_calls:
                tc_id = str(tc.get("id") or "")
                tname = str(tc.get("name") or "").lower()
                raw_args = tc.get("arguments") if "arguments" in tc else tc.get("input")
                tc_args = raw_args if isinstance(raw_args, dict) else {}
                target_path = str(tc_args.get("path") or tc_args.get("file_path") or "")
                if tname == "read":
                    _record_read(
                        target_path,
                        tc_args.get("offset"),
                        tc_args.get("limit"),
                        seen_intervals,
                        metrics,
                    )
                elif tname in ("edit", "write", "multiedit"):
                    _record_mutation(target_path, seen_intervals)
                elif tname == "bash":
                    flags = _inspect_bash_command(str(tc_args.get("command") or ""), metrics)
                    if tc_id:
                        tool_call_meta[tc_id] = flags
                elif tname == "codemode":
                    code = str(tc_args.get("code") or "")
                    for m_read in _CODEMODE_READ_RE.finditer(code):
                        _record_read(m_read.group(1), m_read.group(2), m_read.group(3), seen_intervals, metrics)
                    for m_mut in _CODEMODE_MUTATE_RE.finditer(code):
                        _record_mutation(m_mut.group(1), seen_intervals)
                    flags = _inspect_bash_command(code, metrics)
                    if tc_id:
                        tool_call_meta[tc_id] = flags

        elif role == "toolResult":
            is_err = bool(msg.get("isError"))
            if is_err:
                metrics.tool_errors += 1
            tc_id = str(msg.get("toolCallId") or "")
            meta = tool_call_meta.get(tc_id, {"is_db_sql": False, "is_ship": False})
            result_text = _extract_result_text(msg.get("content"))
            if meta.get("is_db_sql") and (is_err or "ERROR:" in result_text or "REFUSED:" in result_text):
                metrics.failed_db_sql += 1
            if meta.get("is_ship") and "REFUSED:" in result_text:
                metrics.ship_refusals += 1

        elif role == "user":
            # Claude Code places tool_result blocks inside user messages
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                is_err = bool(block.get("is_error"))
                if is_err:
                    metrics.tool_errors += 1
                tc_id = str(block.get("tool_use_id") or "")
                meta = tool_call_meta.get(tc_id, {"is_db_sql": False, "is_ship": False})
                result_text = _extract_result_text(block.get("content"))
                if meta.get("is_db_sql") and (is_err or "ERROR:" in result_text or "REFUSED:" in result_text):
                    metrics.failed_db_sql += 1
                if meta.get("is_ship") and "REFUSED:" in result_text:
                    metrics.ship_refusals += 1

    return metrics


def raw_line_iter(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _matches_since(path: Path, since: str | None) -> bool:
    if not since:
        return True
    clean_since = since.strip()
    stem_date = path.stem[: len(clean_since)]
    if re.match(r"^\d{4}-\d{2}-\d{2}", stem_date):
        return stem_date >= clean_since
    try:
        for line in raw_line_iter(path.read_text(encoding="utf-8", errors="replace"))[:5]:
            obj = json.loads(line)
            ts = str(obj.get("timestamp") or "")[: len(clean_since)]
            if ts:
                return ts >= clean_since
    except Exception:
        pass
    return True


def audit_directory(
    directory: Path,
    *,
    since: str | None = None,
    default_harness: str = "pi",
) -> list[SessionMetrics]:
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl"))
    results: list[SessionMetrics] = []
    for f in files:
        if not _matches_since(f, since):
            continue
        m = audit_transcript_file(f, default_harness=default_harness)
        if m.turns > 0 or m.malformed_lines > 0:
            results.append(m)
    return sorted(results, key=lambda r: (r.session, r.harness))


def cmd_sessions_audit(args: argparse.Namespace) -> int:
    dir_arg = getattr(args, "dir", None)
    harness = str(getattr(args, "harness", None) or "all").lower()
    since = getattr(args, "since", None)

    rows: list[SessionMetrics] = []
    if dir_arg:
        target_dir = Path(dir_arg).expanduser()
        if not target_dir.is_dir():
            output.emit(args, [f"sessions directory not found: {target_dir}"])
            return EXIT_ERROR
        default_h = "claude" if harness == "claude" else "pi"
        rows = audit_directory(target_dir, since=since, default_harness=default_h)
        dir_label = target_dir.name
    else:
        targets: list[tuple[str, Path]] = []
        if harness in ("pi", "all"):
            targets.append(("pi", pi_sessions_dir()))
        if harness in ("claude", "all"):
            targets.append(("claude", claude_sessions_dir()))
        existing = [(h, d) for h, d in targets if d.is_dir()]
        if not existing:
            output.emit(
                args,
                [f"no session directories found for harness={harness} (checked {[str(d) for _, d in targets]})"],
            )
            return EXIT_ERROR
        for h, d in existing:
            rows.extend(audit_directory(d, since=since, default_harness=h))
        dir_label = ",".join(f"{h}:{d.name}" for h, d in existing)

    header = f"SESSIONS AUDIT harness={harness} dir={dir_label} since={since or 'all'} count={len(rows)}"
    table_header = (
        "session             | h      | turns |   tokens |   cost | cold | err | 1-read | rep-read | sleep>240 | gh-api | sql-err | ship-ref | skill"
    )
    sep = "-" * len(table_header)
    lines = [header, table_header, sep]

    totals = SessionMetrics(session="TOTAL", harness=harness)
    for r in rows:
        totals.turns += r.turns
        totals.total_tokens += r.total_tokens
        totals.cache_read += r.cache_read
        totals.uncached_in += r.uncached_in
        totals.out_tokens += r.out_tokens
        totals.cost_usd += r.cost_usd
        totals.cold_turns += r.cold_turns
        totals.cold_cost_usd += r.cold_cost_usd
        totals.tool_errors += r.tool_errors
        totals.single_op_reads += r.single_op_reads
        totals.repeat_reads += r.repeat_reads
        totals.long_sleeps += r.long_sleeps
        totals.raw_gh_comments += r.raw_gh_comments
        totals.failed_db_sql += r.failed_db_sql
        totals.ship_refusals += r.ship_refusals
        totals.malformed_lines += r.malformed_lines
        lines.append(
            f"{r.session:<19} | {r.harness:<6} | {r.turns:>5} | {r.total_tokens / 1e6:>7.2f}M | "
            f"${r.cost_usd:>5.2f} | {r.cold_turns:>4} | {r.tool_errors:>3} | {r.single_op_reads:>6} | "
            f"{r.repeat_reads:>8} | {r.long_sleeps:>9} | {r.raw_gh_comments:>6} | "
            f"{r.failed_db_sql:>7} | {r.ship_refusals:>8} | {'yes' if r.read_skill_md else 'no'}"
        )

    lines.append(sep)
    lines.append(
        f"{totals.session:<19} | {totals.harness:<6} | {totals.turns:>5} | {totals.total_tokens / 1e6:>7.2f}M | "
        f"${totals.cost_usd:>5.2f} | {totals.cold_turns:>4} | {totals.tool_errors:>3} | {totals.single_op_reads:>6} | "
        f"{totals.repeat_reads:>8} | {totals.long_sleeps:>9} | {totals.raw_gh_comments:>6} | "
        f"{totals.failed_db_sql:>7} | {totals.ship_refusals:>8} | -"
    )
    if totals.malformed_lines:
        lines.append(f"note: skipped {totals.malformed_lines} malformed transcript line(s)")

    output.emit(
        args,
        lines,
        data={
            "sessions": [asdict(r) for r in rows],
            "totals": asdict(totals),
        },
    )
    return EXIT_OK
