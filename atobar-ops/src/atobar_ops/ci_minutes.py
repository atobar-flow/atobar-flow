"""Pure billed-minutes and 60s-cliff aggregation for `atobar-ops ci --minutes` (J45 WP-5)."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Iterable


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def bill_job_seconds(sec: int, *, conclusion: str | None = None) -> int:
    """GitHub Actions bills each non-skipped job rounded up to the nearest whole minute."""
    if (conclusion or "").lower() == "skipped" or sec <= 0:
        return 0
    return int(math.ceil(sec / 60.0))


def percentile(values: list[int], pct: int) -> int:
    """Deterministic nearest-rank percentile on a list of integers (safe for n=0, 1, 2)."""
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return int(ordered[idx])


def summarize(
    jobs_payloads: Iterable[Any],
    *,
    truncated: bool = False,
    requested_runs: int | None = None,
) -> dict[str, Any]:
    """Summarizes per-job durations, billed minutes, 55-60s danger band, >60s cliff runs, and slowest step."""
    payload_list = list(jobs_payloads)
    by_job: dict[str, dict[str, Any]] = {}
    total_incomplete = 0

    for item in payload_list:
        if isinstance(item, dict):
            jobs = item.get("jobs") if "jobs" in item else [item]
        elif isinstance(item, list):
            jobs = item
        else:
            continue

        for raw_job in jobs or []:
            if not isinstance(raw_job, dict):
                continue
            name = str(raw_job.get("name") or "unknown")
            bucket = by_job.setdefault(
                name,
                {
                    "durations_s": [],
                    "billed_per_run": [],
                    "skipped": 0,
                    "incomplete": 0,
                    "steps": {},
                },
            )
            conclusion = str(raw_job.get("conclusion") or "").lower()
            started_raw = raw_job.get("started_at")
            completed_raw = raw_job.get("completed_at")

            if conclusion == "skipped":
                bucket["skipped"] += 1
                bucket["billed_per_run"].append(0)
                continue
            elif not started_raw or not completed_raw:
                bucket["incomplete"] += 1
                total_incomplete += 1
                continue
            else:
                try:
                    sec = max(0, int(round((_parse_iso(str(completed_raw)) - _parse_iso(str(started_raw))).total_seconds())))
                except Exception:
                    bucket["incomplete"] += 1
                    total_incomplete += 1
                    continue

            billed = bill_job_seconds(sec, conclusion=conclusion)
            bucket["durations_s"].append(sec)
            bucket["billed_per_run"].append(billed)

            for step in raw_job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if str(step.get("conclusion") or "").lower() == "skipped":
                    continue
                s_start = step.get("started_at")
                s_end = step.get("completed_at")
                if not s_start or not s_end:
                    continue
                try:
                    s_sec = max(0, int(round((_parse_iso(str(s_end)) - _parse_iso(str(s_start))).total_seconds())))
                except Exception:
                    continue
                s_name = str(step.get("name") or "unknown")
                bucket["steps"].setdefault(s_name, []).append(s_sec)

    jobs_out: dict[str, dict[str, Any]] = {}
    total_billed = 0
    total_danger = 0
    total_cliff = 0
    warn_candidates: list[tuple[str, int]] = []

    for name, bucket in by_job.items():
        durations: list[int] = bucket["durations_s"]
        billed_list: list[int] = bucket["billed_per_run"]
        billed_sum = sum(billed_list)
        danger = sum(1 for d in durations if 55 <= d <= 60)
        cliff = sum(1 for d in durations if d > 60)
        p50 = percentile(durations, 50)
        p90 = percentile(durations, 90)
        max_s = max(durations) if durations else 0

        slowest_step: dict[str, Any] | None = None
        for s_name, s_vals in bucket["steps"].items():
            s_med = percentile(s_vals, 50)
            if slowest_step is None or s_med > slowest_step["median_s"]:
                slowest_step = {"name": s_name, "median_s": s_med}

        jobs_out[name] = {
            "name": name,
            "n": len(durations),
            "skipped": bucket["skipped"],
            "p50_s": p50,
            "p90_s": p90,
            "max_s": max_s,
            "billed_minutes": billed_sum,
            "billed_per_run": billed_list,
            "durations_s": durations,
            "danger_band": danger,
            "over_60s": cliff,
            "cliff": cliff,
            "incomplete": bucket["incomplete"],
            "slowest_step": slowest_step,
        }
        total_billed += billed_sum
        total_danger += danger
        total_cliff += cliff
        if p90 > 50:
            warn_candidates.append((name, p90))

    total_executed_jobs = sum(j["n"] for j in jobs_out.values())
    if total_cliff > 0:
        next_hint = f"CLIFF: {total_cliff} runs billed 2 min"
    elif warn_candidates:
        w_name, w_p90 = warn_candidates[0]
        next_hint = f"WARN: {w_name} p90 > 50s ({w_p90}s), see J45 WP-6"
    elif total_executed_jobs == 0:
        next_hint = "NO_DATA: no completed job runs analyzed"
    else:
        next_hint = "OK"

    return {
        "runs_analyzed": len(payload_list),
        "requested_runs": requested_runs if requested_runs is not None else len(payload_list),
        "truncated": bool(truncated),
        "incomplete": total_incomplete,
        "total_billed_minutes": total_billed,
        "danger_band": total_danger,
        "over_60s": total_cliff,
        "cliff": total_cliff,
        "jobs": jobs_out,
        "next": next_hint,
    }


def format_summary(summary: dict[str, Any], *, repo: str = "", workflow: str | None = None) -> list[str]:
    wf_suffix = f" (workflow={workflow})" if workflow else ""
    header_target = f" for {repo}{wf_suffix}" if repo else wf_suffix
    lines = [
        f"CI billed-minutes summary{header_target}: runs={summary['runs_analyzed']} "
        f"total_billed_min={summary['total_billed_minutes']} incomplete={summary['incomplete']}"
    ]
    if summary.get("truncated"):
        lines.append(
            f"TRUNCATED: analyzed {summary['runs_analyzed']}/{summary['requested_runs']} runs before timeout"
        )
    for name, j in summary.get("jobs", {}).items():
        slow = j.get("slowest_step")
        slow_str = f"{slow['name']} ({slow['median_s']}s)" if slow else "n/a"
        lines.append(
            f"  {name}: n={j['n']} p50={j['p50_s']}s p90={j['p90_s']}s max={j['max_s']}s "
            f"billed_min={j['billed_minutes']} danger_55_60s={j['danger_band']} "
            f"over_60s={j['over_60s']} slowest_step={slow_str}"
        )
    lines.append(f"NEXT: {summary['next']}")
    return lines
