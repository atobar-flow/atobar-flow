"""<owner/repo> issue -> tenant row, EC2 instance, telemetry status, linked PRs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from . import db, gh, timebox

ROLES = ("planner", "developer", "evaluator", "arbitrator")
TELEMETRY_MARKER = "flow Agent Telemetry (Live)"
STATUS_RE = re.compile(r"\*Status:\s*(.*?)\s*\|\s*Time Elapsed", re.DOTALL)

_PR_HEAD_ISSUE_PATTERNS = [
    r"(?:^|[-_/])issue[-_/]?{issue}(?:$|[-_/])",
    r"(?:^|[-_/])developer/{issue}(?:$|[-_/])",
    r"(?:^|[-_/]){issue}(?:$|[-_/])",
]


@dataclass
class TelemetryInfo:
    comment_id: int
    status: str
    tail: str


@dataclass
class InstanceInfo:
    instance_id: str
    role: str  # role from the EC2 Name tag, i.e. the role the VM was launched for
    name: str
    state: str
    # Role of the latest session heartbeat from this instance. A warm VM rotates
    # planner -> developer -> evaluator without relaunching, so the tag goes stale.
    active_role: Optional[str] = None


@dataclass
class PRInfo:
    number: int
    state: str
    head_ref: str
    title: str


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def matches_issue_pr(pr: dict[str, Any], issue: int) -> bool:
    head = str(pr.get("headRefName") or "")
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    if not head.lower().startswith("flow"):
        return False
    if any(re.search(pat.format(issue=issue), head, re.IGNORECASE) for pat in _PR_HEAD_ISSUE_PATTERNS):
        return True
    ref_pat = rf"#{issue}\b"
    return bool(re.search(ref_pat, title) or re.search(ref_pat, body))


def find_instances(repo: str, issue: int, *, region: str = "us-west-2", timeout: float = 20) -> list[InstanceInfo]:
    targets = {f"flow-{role}-{slug(repo)}-{issue}": role for role in ROLES}
    cmd = [
        "aws",
        "ec2",
        "describe-instances",
        "--region",
        region,
        "--filters",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        "--query",
        "Reservations[*].Instances[*].[InstanceId,State.Name,Tags[?Key==`Name`].Value|[0]]",
        "--output",
        "json",
    ]
    res = timebox.run(cmd, timeout=timeout)
    if res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout or "[]")
    except Exception:
        return []
    out: list[InstanceInfo] = []
    for reservation in data:
        for inst in reservation:
            iid, state, name = inst[0], inst[1], inst[2]
            if name in targets:
                out.append(InstanceInfo(instance_id=iid, role=targets[name], name=name, state=state))
    return out


def find_telemetry(repo: str, issue: int, *, timeout: float = 20) -> Optional[TelemetryInfo]:
    comments = gh.json_or_empty(["api", f"repos/{repo}/issues/{issue}/comments", "--paginate"], timeout=timeout)
    best: dict[str, Any] | None = None
    for c in comments:
        if TELEMETRY_MARKER in str(c.get("body") or ""):
            best = c
    if not best:
        return None
    body = str(best.get("body") or "")
    m = STATUS_RE.search(body)
    status = m.group(1).strip() if m else "unknown"
    tail_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return TelemetryInfo(comment_id=int(best["id"]), status=status, tail=" / ".join(tail_lines[-3:]))


def find_prs(repo: str, issue: int, *, state: str = "all", timeout: float = 20) -> list[PRInfo]:
    prs = gh.json_or_empty(
        ["pr", "list", "-R", repo, "--state", state, "--json", "number,headRefName,title,body,state"],
        timeout=timeout,
    )
    out = []
    for pr in prs:
        if matches_issue_pr(pr, issue):
            out.append(
                PRInfo(
                    number=int(pr["number"]),
                    state=str(pr.get("state", "")),
                    head_ref=str(pr.get("headRefName", "")),
                    title=str(pr.get("title", "")),
                )
            )
    return out


@dataclass
class Resolved:
    repo: str
    issue: int
    tenant: dict[str, Any] | None
    telemetry: TelemetryInfo | None
    instances: list[InstanceInfo]
    prs: list[PRInfo]
    run_id: str | None


def resolve(repo: str, issue: int, *, with_tenant: bool = True, timeout: float = 20) -> Resolved:
    tenant = None
    run_id = None
    if with_tenant:
        try:
            tenant = db.tenant_row(repo)
            if tenant:
                run_id = db.latest_run_id(tenant["id"])
        except Exception:
            tenant = None
    return Resolved(
        repo=repo,
        issue=issue,
        tenant=tenant,
        telemetry=find_telemetry(repo, issue, timeout=timeout),
        instances=find_instances(repo, issue, timeout=timeout),
        prs=find_prs(repo, issue, timeout=timeout),
        run_id=run_id,
    )
