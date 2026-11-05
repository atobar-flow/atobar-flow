"""atobar-ops: one CLI consolidating atobar-flow's ops skills (watch/rerun/ssm/db/cp/ci/ship/jumbo).

Exit codes: 0 = changed or terminal state reached, 2 = timed out with no change, 1 = error.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from atobar_contracts.issue_states import FLOW_LABEL_CATALOG

from . import bootstrap, ci_minutes, creds, db, gh, output, resolve, sessions, state, timebox, verify

EXIT_OK, EXIT_ERROR, EXIT_TIMEOUT = timebox.EXIT_OK, timebox.EXIT_ERROR, timebox.EXIT_TIMEOUT


# --------------------------------------------------------------------------- watch


def _has_arbitration_verdict(events: list[dict[str, Any]]) -> bool:
    # The arbitrator is a deterministic gate, not an LLM loop — it never writes a
    # loop_rounds row, so "latest loop_rounds role" can never be "arbitrator". The
    # one unambiguous signal that the whole pipeline reached its last phase is a
    # run_events row for the `flow-arbitration` envelope: kind="verdict" is reused
    # by both evaluator and arbitrator, disambiguated by payload.envelope (see
    # src/flow_engine/roles/{evaluator/runner.py,arbitrator/run.py}).
    return any(ev.get("kind") == "verdict" and (ev.get("payload") or {}).get("envelope") == "arbitration" for ev in events)


def _is_terminal(status: str, r: resolve.Resolved, events: list[dict[str, Any]]) -> bool:
    s = status.lower()
    if any(pr.state == "MERGED" for pr in r.prs):
        return True
    if "halted" in s or "exception" in s:
        return True
    if _has_arbitration_verdict(events):
        return True
    return False


def _next_hint(r: resolve.Resolved, status: str) -> str:
    s = status.lower()
    if "halted" in s or "exception" in s:
        return (
            f"investigate halt cause: `atobar-ops ssm {r.repo} {r.issue} agent-log` "
            f"or `atobar-ops db events {r.repo} --issue {r.issue}`"
        )
    if any(pr.state == "MERGED" for pr in r.prs):
        return "run converged and merged; nothing to do"
    open_prs = [pr for pr in r.prs if pr.state == "OPEN"]
    if open_prs:
        return f"PR #{open_prs[0].number} is open; watch for the evaluator verdict / check runs"
    if r.instances:
        return "agent instance still running; keep watching"
    if "complete" in s:
        return "phase complete; keep watching for the next role dispatch"
    return "no instance or PR yet; confirm the trigger label was applied"


def _format_snapshot(r: resolve.Resolved, new_events: list[dict[str, Any]], rounds: list[dict[str, Any]], status: str) -> list[str]:
    lines = [f"{r.repo}#{r.issue}"]
    tail = f" ({r.telemetry.tail})" if r.telemetry else ""
    lines.append(f"telemetry: {status}{tail}")
    if r.instances:
        for inst in r.instances:
            role = inst.active_role or inst.role
            launched = f" (launched as {inst.role})" if role != inst.role else ""
            lines.append(f"ec2[{role}]: {inst.instance_id} {inst.state}{launched}")
    else:
        lines.append("ec2: none running")
    if r.prs:
        for pr in r.prs:
            lines.append(f"pr #{pr.number} [{pr.state}] {pr.head_ref}: {pr.title}")
    else:
        lines.append("pr: none linked")
    if new_events:
        lines.append(f"run_events (+{len(new_events)} new):")
        for ev in new_events[-10:]:
            lines.append(f"  [{ev.get('seq')}] {str(ev.get('ts'))[:19]} {ev.get('kind')}")
    if rounds:
        latest = rounds[0]
        lines.append(
            f"loop_rounds latest: pr#{latest.get('pr_number')} round={latest.get('round')} "
            f"role={latest.get('role')} tokens={latest.get('tokens')}"
        )
    lines.append(f"NEXT: {_next_hint(r, status)}")
    return lines


def cmd_watch(args: argparse.Namespace) -> int:
    """Polls internally until something changes vs. the last `watch` call (bookmarked
    on disk), a terminal state is reached, or the deadline expires.

    `changed` is always measured against that fixed baseline, so a single internal
    poll that already differs from last time's bookmark is real news and returns
    immediately — there is no "first poll is trivially new" special case.
    """
    timeout = timebox.resolve_timeout(args.timeout)
    deadline = timebox.Deadline(timeout)
    until = args.until
    baseline_status = state.load(args.repo, args.issue).get("last_status")
    any_change_seen = False
    lines: list[str] = []
    exit_code = EXIT_TIMEOUT

    while True:
        budget = max(5, min(20, deadline.remaining() or 5))
        r = resolve.resolve(args.repo, args.issue, timeout=budget)
        events: list[dict[str, Any]] = []
        rounds: list[dict[str, Any]] = []
        if r.tenant:
            try:
                if r.run_id:
                    events = db.run_events(r.run_id, limit=30)
                # pr_number here is really the dispatch/issue id (see dispatch.py's
                # FLOW_PR/ISSUE_ID) — filter on it so another issue's more recent
                # activity on the same tenant can't be mistaken for this one's.
                rounds = db.loop_rounds(tenant_id=r.tenant["id"], pr_number=args.issue, limit=5)
            except Exception:
                pass
            try:
                roles = db.session_roles_by_instance([i.instance_id for i in r.instances])
                for inst in r.instances:
                    inst.active_role = roles.get(inst.instance_id)
            except Exception:
                pass
        new_events = state.diff_new_events(args.repo, args.issue, events) if events else []

        status = r.telemetry.status if r.telemetry else "no telemetry"
        terminal = _is_terminal(status, r, events)
        changed = bool(new_events) or status != baseline_status
        any_change_seen = any_change_seen or changed

        st = state.load(args.repo, args.issue)
        st["last_status"] = status
        state.save(args.repo, args.issue, st)

        lines = _format_snapshot(r, new_events, rounds, status)

        if terminal or (until == "change" and changed) or deadline.expired():
            exit_code = EXIT_OK if (terminal or any_change_seen) else EXIT_TIMEOUT
            break
        deadline.sleep(15)
        if deadline.expired():
            exit_code = EXIT_OK if any_change_seen else EXIT_TIMEOUT
            break

    output.emit(args, lines, data={"repo": args.repo, "issue": args.issue, "lines": lines, "exit_code": exit_code})
    return exit_code


# --------------------------------------------------------------------------- rerun


def _discover_wip_branches(repo: str, issue: int) -> set[str]:
    refs = gh.json_or_empty(["api", f"repos/{repo}/git/matching-refs/heads/flow-wip/"])
    wip: set[str] = set()
    if not isinstance(refs, list):
        return wip
    suffixes = (f"/{issue}", f"-{issue}")
    for item in refs:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/") :]
            if branch.startswith("flow-wip/") and branch.endswith(suffixes):
                wip.add(branch)
    return wip


def cmd_rerun(args: argparse.Namespace) -> int:
    repo, issue = args.repo, args.issue
    no_relabel = bool(getattr(args, "no_relabel", False))
    r = resolve.resolve(repo, issue, with_tenant=True)
    keep = {int(x) for x in (args.keep_pr or [])}
    stale_prs = [pr for pr in r.prs if pr.number not in keep]

    comments = gh.json_or_empty(["api", f"repos/{repo}/issues/{issue}/comments", "--paginate"])
    plan: list[str] = [f"rerun plan for {repo}#{issue}:", f"  delete {len(comments)} issue comment(s)"]
    if stale_prs:
        for pr in stale_prs:
            plan.append(f"  close+archive PR #{pr.number} ({pr.head_ref}) [{pr.state}]")
    else:
        plan.append("  close+archive PRs: none")
    wip_branches = _discover_wip_branches(repo, issue)
    branches = sorted({pr.head_ref for pr in stale_prs} | wip_branches)
    plan.append(f"  delete branches: {', '.join(branches) if branches else 'none'}")

    # loop_rounds/arbitration_decisions.pr_number is the conductor's dispatch id,
    # i.e. the GitHub *issue* number (see dispatch.py's FLOW_PR/ISSUE_ID), not the
    # PR number(s) that got opened — prune keys on `issue`, not on `stale_prs`.
    prune_counts: dict[str, int] = {}
    if r.tenant and stale_prs:
        try:
            prune_counts = db.prune_plan(r.tenant["id"], [issue])
            plan.append(f"  prune supabase rows for issue #{issue}: {prune_counts}")
        except Exception as exc:
            plan.append(f"  prune supabase rows: could not plan ({type(exc).__name__})")
    else:
        plan.append("  prune supabase rows: none (no tenant row or no stale PRs)")

    labels = gh.json_or_empty(["api", f"repos/{repo}/issues/{issue}/labels"])
    flow_labels = [lbl["name"] for lbl in labels if str(lbl.get("name", "")).startswith("flow:")]
    plan.append(f"  clear labels: {flow_labels or 'none'}")
    plan.append("  re-apply label: skipped (--no-relabel)" if no_relabel else f"  re-apply label: {args.label}")

    if not args.write:
        output.emit(args, ["[DRY-RUN]", *plan])
        return EXIT_OK

    deleted_comments = 0
    for c in comments:
        cid = c.get("id")
        if not cid:
            continue
        res = gh.run(["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{cid}"])
        if res.returncode == 0:
            deleted_comments += 1

    closed_prs: list[int] = []
    for pr in stale_prs:
        if pr.state == "OPEN":
            res = gh.run(["pr", "close", str(pr.number), "-R", repo, "--delete-branch"])
            if res.returncode != 0:
                gh.run(["pr", "close", str(pr.number), "-R", repo])
        # GitHub has no PR-deletion API; the repo's own convention is to rename
        # closed flow* PRs to "[archived]" so they stop cluttering PR lists.
        if pr.title != "[archived]":
            gh.run(["pr", "edit", str(pr.number), "-R", repo, "--title", "[archived]"])
        closed_prs.append(pr.number)

    for branch in branches:
        gh.run(["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"])

    for lbl in flow_labels:
        gh.run(["issue", "edit", str(issue), "-R", repo, "--remove-label", lbl])

    if r.tenant and stale_prs:
        try:
            prune_counts = db.prune_execute(r.tenant["id"], [issue])
        except Exception:
            pass

    if not no_relabel:
        gh.run(["issue", "edit", str(issue), "-R", repo, "--add-label", args.label])

    output.emit(
        args,
        [
            "[DONE]",
            f"deleted_comments={deleted_comments}",
            f"closed_prs={closed_prs}",
            f"deleted_branches={branches}",
            f"pruned={prune_counts}",
            f"relabeled={'none (--no-relabel)' if no_relabel else args.label}",
        ],
    )
    if no_relabel:
        return EXIT_OK

    watch_args = argparse.Namespace(
        repo=repo,
        issue=issue,
        timeout=args.timeout,
        until="change",
        json=getattr(args, "json", False),
        verbose=getattr(args, "verbose", False),
        max_chars=getattr(args, "max_chars", None),
    )
    return cmd_watch(watch_args)


# --------------------------------------------------------------------------- ssm

SSM_RECIPES = {
    "agent-log": "tail -n 200 /home/ubuntu/agent.log 2>/dev/null || echo '(no agent.log)'",
    "cloud-init": "tail -n 200 /var/log/cloud-init-output.log 2>/dev/null || echo '(no cloud-init log)'",
    "git-rev": "git -C /home/ubuntu/atobar-flow rev-parse HEAD 2>/dev/null || echo '(no checkout)'",
    "display": (
        "DISPLAY=:0 import -window root /tmp/ops_capture.png 2>/dev/null "
        "&& base64 -w0 /tmp/ops_capture.png || echo NO_DISPLAY"
    ),
}


def cmd_ssm(args: argparse.Namespace) -> int:
    timeout = timebox.resolve_timeout(args.timeout)
    deadline = timebox.Deadline(timeout)

    instances = resolve.find_instances(args.repo, args.issue, timeout=max(5, min(20, deadline.remaining())))
    if args.role:
        instances = [i for i in instances if i.role == args.role]
    running = [i for i in instances if i.state == "running"]
    if not running:
        output.emit(args, [f"no running instance for {args.repo}#{args.issue}" + (f" role={args.role}" if args.role else "")])
        return EXIT_ERROR
    if len(running) > 1:
        output.emit(args, [f"multiple instances match; pass --role: {[(i.role, i.instance_id) for i in running]}"])
        return EXIT_ERROR
    inst = running[0]

    value = args.recipe_or_cmd
    if not value:
        output.emit(args, [f"pass a recipe name ({sorted(SSM_RECIPES)}) or `-- \"<raw shell command>\"`"])
        return EXIT_ERROR
    shell_cmd = SSM_RECIPES.get(value, value)
    recipe_label = value if value in SSM_RECIPES else "cmd"

    send = timebox.run(
        [
            "aws",
            "ssm",
            "send-command",
            "--region",
            "us-west-2",
            "--document-name",
            "AWS-RunShellScript",
            "--targets",
            f"Key=InstanceIds,Values={inst.instance_id}",
            "--parameters",
            json.dumps({"commands": [shell_cmd]}),
            "--query",
            "Command.CommandId",
            "--output",
            "text",
        ],
        timeout=max(5, min(20, deadline.remaining())),
    )
    if send.returncode != 0 or not send.stdout.strip():
        output.emit(args, [f"ssm send-command failed: {send.stderr.strip()[:300]}"])
        return EXIT_ERROR
    cid = send.stdout.strip()

    while not deadline.expired():
        deadline.sleep(4)
        inv = timebox.run(
            [
                "aws",
                "ssm",
                "get-command-invocation",
                "--region",
                "us-west-2",
                "--command-id",
                cid,
                "--instance-id",
                inst.instance_id,
                "--query",
                "{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}",
                "--output",
                "json",
            ],
            timeout=max(1, min(15, deadline.remaining())),
        )
        if inv.returncode != 0:
            continue
        try:
            data = json.loads(inv.stdout)
        except Exception:
            continue
        status = data.get("Status")
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            out_text = (data.get("Out") or "").strip()
            err_text = (data.get("Err") or "").strip()
            lines = [f"[{status}] {recipe_label} on {inst.instance_id} ({inst.role})"]
            if recipe_label == "display" and out_text and out_text != "NO_DISPLAY" and len(out_text) > 100:
                png_path = Path(f"ops_display_{args.repo.replace('/', '__')}_{args.issue}.png")
                try:
                    png_path.write_bytes(base64.b64decode(out_text))
                    lines.append(f"display capture saved to {png_path}")
                except Exception:
                    lines.append("display capture returned but failed to decode")
            elif out_text:
                lines.append(out_text)
            if err_text:
                lines.append(f"stderr: {err_text}")
            output.emit(args, lines)
            return EXIT_OK if status == "Success" else EXIT_ERROR

    output.emit(args, [f"timed out after {timeout}s waiting for SSM command {cid}"])
    return EXIT_TIMEOUT


# --------------------------------------------------------------------------- db


def cmd_db_tenant(args: argparse.Namespace) -> int:
    row = db.tenant_row(args.repo)
    if not row:
        output.emit(args, [f"tenant not found: {args.repo}"])
        return EXIT_ERROR
    tid = row["id"]
    msha = str(row.get("manifest_sha") or "")[:12] or "NULL"
    active_metrics: int | str
    try:
        active_metrics = int(db.active_metric_count(tid))
    except Exception:
        active_metrics = "?"
    lines = [
        f"TENANT: repo={row['repo']} id={tid} status={row['status']} "
        f"arb_mode={row.get('arbitration_mode')} branch={row.get('default_branch')} "
        f"manifest_sha={msha} active_metrics={active_metrics}"
    ]
    roles = {r["role"]: r for r in db.role_models(tid)}
    lines.append("ROLE MODELS:")
    for role in ("planner", "developer", "evaluator", "arbitrator"):
        r = roles.get(role)
        if r:
            lines.append(f"  - {role:<10} provider={r['provider']:<10} model={r['model']:<28} effort={r['reasoning_effort']}")
        else:
            lines.append(f"  - {role:<10} <UNCONFIGURED>")
    keys = db.llm_keys_rows(tid)
    kek = creds.kek_bytes()
    lines.append("LLM KEYS:")
    if not keys:
        lines.append("  (none)")
    for k in keys:
        decrypt_status = "kek_unavailable"
        if kek is not None and k.get("ciphertext"):
            try:
                plain = creds.decrypt_local_kek(kek, tid, k["provider"], k["ciphertext"])
                decrypt_status = "ok" if plain else "empty"
            except Exception as exc:
                decrypt_status = f"FAIL({type(exc).__name__})"
        lines.append(f"  - provider={k['provider']:<10} last4=...{k.get('key_last4', '????')} decrypt={decrypt_status}")
    output.emit(
        args,
        lines,
        data={"tenant": row, "roles": roles, "keys": keys, "active_metrics": active_metrics},
    )
    return EXIT_OK


def cmd_db_set_model(args: argparse.Namespace) -> int:
    try:
        from atobar_contracts.providers import canonical_provider

        provider = canonical_provider(args.provider)
    except Exception as exc:
        output.emit(args, [str(exc)])
        return EXIT_ERROR
    try:
        result = db.set_role_model(args.repo, args.role, provider, args.model, args.effort)
    except LookupError as exc:
        output.emit(args, [str(exc)])
        return EXIT_ERROR
    output.emit(args, [f"[OK] {args.repo} role={result['role']} -> provider={result['provider']} model={result['model']}"])
    return EXIT_OK


def cmd_db_events(args: argparse.Namespace) -> int:
    # `loop_rounds.pr_number` is populated from the conductor's dispatch identifier,
    # which is the GitHub *issue* number (see FLOW_PR/ISSUE_ID in dispatch.py), not
    # the eventual PR number — so filtering uses args.issue directly.
    tenant_id = None
    pr_number = args.issue
    run_id = args.run
    if args.repo:
        row = db.tenant_row(args.repo)
        tenant_id = row["id"] if row else None
    if not run_id and tenant_id:
        run_id = db.latest_run_id(tenant_id)
    if not run_id:
        output.emit(args, ["no run_id resolved (pass --run or --repo with a known tenant)"])
        return EXIT_ERROR

    events = db.run_events(run_id, limit=args.limit)
    lines = [f"RUN_EVENTS run_id={run_id}" + (f" issue={args.issue}" if args.issue else "") + f" (limit={args.limit}):"]
    if not events:
        lines.append("  (none)")
    for ev in events:
        lines.append(f"  [{ev.get('seq'):>3}] {str(ev.get('ts'))[:19]} {ev.get('kind'):<18} {json.dumps(ev.get('payload', {}), default=str)[:160]}")

    rounds = db.loop_rounds(tenant_id=tenant_id, pr_number=pr_number, limit=min(args.limit, 15))
    lines.append(f"LOOP_ROUNDS (latest {len(rounds)}):")
    for r in rounds:
        usage_raw = r.get("usage_detail")
        usage: dict[str, Any] = dict(usage_raw) if isinstance(usage_raw, dict) else {}
        runtime = str(usage.get("agent_runtime") or "nanobot")
        calls_raw = usage.get("calls")
        calls: list[Any] = list(calls_raw) if isinstance(calls_raw, list) else []
        wc = float(r.get("wall_clock_minutes") or 0.0)
        lines.append(
            f"  PR#{r.get('pr_number')} r={r.get('round')} role={str(r.get('role') or ''):<10} "
            f"runtime={runtime} tok={r.get('tokens')} cost=${float(r.get('cost_usd') or 0):.4f} "
            f"wc={wc:.2f}m calls={len(calls)}"
        )

    arb_rows: list[dict[str, Any]] = []
    if (
        tenant_id
        and hasattr(db, "arbitration_decisions")
        and (
            getattr(db.arbitration_decisions, "__module__", "") != "atobar_ops.db"
            or getattr(db.loop_rounds, "__module__", "") == "atobar_ops.db"
        )
    ):
        try:
            arb_rows = db.arbitration_decisions(tenant_id=tenant_id, pr_number=pr_number, limit=5)
        except Exception:
            arb_rows = []
    if arb_rows:
        lines.append(f"ARBITRATION_DECISIONS (latest {len(arb_rows)}):")
        for a in arb_rows:
            msha = str(a.get("manifest_sha") or "")
            lines.append(
                f"  PR#{a.get('pr_number')} mode={a.get('mode')} decision={a.get('decision')} "
                f"manifest_sha={msha[:12] or 'NULL'}"
            )
    output.emit(
        args,
        lines,
        data={"run_id": run_id, "events": events, "loop_rounds": rounds, "arbitration_decisions": arb_rows},
    )
    return EXIT_OK


def cmd_db_sql(args: argparse.Namespace) -> int:
    eff_timeout = timebox.resolve_timeout(args.timeout)
    try:
        rc, out, err = db.run_sql(args.sql, timeout=eff_timeout)
    except db.SqlRejected as exc:
        output.emit(args, [f"REFUSED: {exc}"])
        return EXIT_ERROR
    lines = [out.strip() or "(no output)"]
    if err.strip():
        lines.append(f"stderr: {err.strip()}")
        if rc != 0:
            try:
                hint = db.format_sql_error_hint(args.sql, err, timeout=min(10.0, float(eff_timeout)))
            except Exception:
                hint = ""
            if hint:
                lines.append(hint)
    output.emit(args, lines)
    return EXIT_OK if rc == 0 else EXIT_ERROR


def cmd_db_schema(args: argparse.Namespace) -> int:
    eff_timeout = timebox.resolve_timeout(args.timeout)
    table = getattr(args, "table", None)
    try:
        if table:
            rc, out, err = db.schema_columns(str(table), timeout=eff_timeout)
        else:
            rc, out, err = db.schema_tables(timeout=eff_timeout)
    except db.SqlRejected as exc:
        output.emit(args, [f"REFUSED: {exc}"])
        return EXIT_ERROR
    lines = [out.strip() or "(no output)"]
    if err.strip():
        lines.append(err.strip())
    output.emit(args, lines)
    return EXIT_OK if rc == 0 else EXIT_ERROR


def cmd_db_prune(args: argparse.Namespace) -> int:
    row = db.tenant_row(args.repo)
    if not row:
        output.emit(args, [f"tenant not found: {args.repo}"])
        return EXIT_ERROR
    r = resolve.resolve(args.repo, args.issue, with_tenant=False)
    if not r.prs:
        output.emit(args, [f"no PRs found for {args.repo}#{args.issue}; nothing to prune"])
        return EXIT_OK
    # loop_rounds/arbitration_decisions.pr_number is the dispatch id (the issue
    # number), not the linked PR's own number — see dispatch.py's FLOW_PR/ISSUE_ID.
    if not args.write:
        counts = db.prune_plan(row["id"], [args.issue])
        output.emit(args, [f"[DRY-RUN] would prune rows scoped to issue #{args.issue}: {counts}"])
        return EXIT_OK
    counts = db.prune_execute(row["id"], [args.issue])
    output.emit(args, [f"[DONE] pruned rows scoped to issue #{args.issue}: {counts}"])
    return EXIT_OK


def cmd_db_migrate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        output.emit(args, [f"migration file not found: {path}"])
        return EXIT_ERROR
    if not args.write:
        output.emit(args, [f"[DRY-RUN] would apply migration {path} ({path.stat().st_size} bytes)"])
        return EXIT_OK
    rc, out, err = db.run_migration_file(str(path), timeout=min(60, timebox.resolve_timeout(args.timeout)))
    lines = [f"[MIGRATE] {path} rc={rc}"]
    if out.strip():
        lines.append(out.strip())
    if err.strip():
        lines.append(f"stderr: {err.strip()}")
    output.emit(args, lines)
    return EXIT_OK if rc == 0 else EXIT_ERROR


# --------------------------------------------------------------------------- cp

CP_SSH_HOST = f"root@{creds.DEFAULT_CP_HOSTNAME}"
CP_SSH_KEY = str(Path.home() / ".ssh" / "cp.key")
CP_BASE_URL = creds.DEFAULT_CP_URL


def _cp_ssh(remote_cmd: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    ssh_host = creds.cp_ssh_host()
    return timebox.run(
        ["ssh", "-i", CP_SSH_KEY, "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", ssh_host, remote_cmd],
        timeout=timeout,
    )


def cmd_cp_status(args: argparse.Namespace) -> int:
    timeout = timebox.resolve_timeout(args.timeout)
    remote = (
        "git -C /opt/atobar-flow rev-parse HEAD 2>/dev/null; echo ---; "
        "systemctl is-active atobar-cp-refresh.timer 2>/dev/null; echo ---; "
        "journalctl -u atobar-cp-refresh.service -n 20 --no-pager 2>/dev/null; echo ---; "
        "docker ps --format '{{.Names}}\\t{{.Status}}' 2>/dev/null"
    )
    res = _cp_ssh(remote, timeout=max(10, min(60, timeout)))
    if res.returncode != 0:
        output.emit(args, [f"ssh to control panel failed: {res.stderr.strip()[:300]}"])
        return EXIT_ERROR
    parts = res.stdout.split("---")
    deployed_sha = parts[0].strip() if parts else ""

    remote_sha_res = timebox.run(["git", "ls-remote", f"https://github.com/{creds.factory_repo()}", "main"], timeout=20)
    origin_sha = remote_sha_res.stdout.split()[0] if remote_sha_res.returncode == 0 and remote_sha_res.stdout.strip() else "?"

    lines = [
        f"deployed_sha={deployed_sha or '?'}",
        f"origin/main={origin_sha}",
        f"drift={'NONE' if deployed_sha and deployed_sha == origin_sha else 'DRIFT or unknown'}",
    ]
    if len(parts) > 1:
        lines.append(f"refresh.timer={parts[1].strip()}")
    if len(parts) > 2:
        lines.append("recent journal:")
        lines.append(parts[2].strip())
    if len(parts) > 3:
        lines.append("docker ps:")
        lines.append(parts[3].strip())
    lines.append("drift check is best-effort; compare docker ps above against infra/terraform/control-panel-hetzner/main.tf")
    output.emit(args, lines)
    return EXIT_OK


def cmd_cp_probe(args: argparse.Namespace) -> int:
    import httpx

    routes = [
        "/",
        "/login",
        "/healthz",
        "/docs",
        "/openapi.json",
        f"/tenants/{args.repo}",
        f"/tenants/{args.repo}/llm-keys",
        "/v1/runs/nonexistent-run/screenshots/nonexistent.png",
    ]
    lines = []
    flags = []
    timeout = max(5, min(15, timebox.resolve_timeout(args.timeout) / 4))
    base_url = CP_BASE_URL if CP_BASE_URL != creds.DEFAULT_CP_URL else creds.cp_base_url()
    for route in routes:
        try:
            resp = httpx.get(f"{base_url}{route}", timeout=timeout, follow_redirects=False)
            suffix = f" -> {resp.headers.get('location')}" if resp.is_redirect else ""
            if route == "/healthz":
                body_text = resp.text.strip()
                suffix = f" {body_text}"
                try:
                    data = resp.json()
                except Exception:
                    data = None
                if resp.status_code != 200 or data != {"status": "ok"}:
                    flags.append(f"FLAG: /healthz returned {resp.status_code} {body_text} (expected 200 {{'status': 'ok'}})")
            elif route in ("/docs", "/openapi.json") and resp.status_code != 404:
                flags.append(f"FLAG: {route} returned {resp.status_code} (expected 404)")
            elif "screenshots" in route and resp.status_code not in (401, 403):
                flags.append(f"FLAG: {route} returned {resp.status_code} (should require auth 401/403)")
            lines.append(f"{resp.status_code} {route}{suffix}")
        except Exception as exc:
            lines.append(f"ERR {route}: {type(exc).__name__}")
            flags.append(f"FLAG: {route} request failed ({type(exc).__name__})")
    if flags:
        lines.extend(flags)
    output.emit(args, lines)
    return EXIT_ERROR if flags else EXIT_OK


def cmd_cp_keycheck(args: argparse.Namespace) -> int:
    import os

    key = os.environ.get("KEY", "").strip()
    if not key:
        output.emit(args, ["no KEY env var set; export KEY=<value> and retry (never pass it as an argument)"])
        return EXIT_ERROR
    from atobar_contracts.providers import PROVIDERS

    provider = PROVIDERS.get(args.provider)
    if not provider:
        output.emit(args, [f"unknown provider: {args.provider} (known: {sorted(PROVIDERS)})"])
        return EXIT_ERROR
    try:
        ok, models = provider.validate(key)
        output.emit(args, [f"provider={args.provider} valid={ok} models_seen={len(models)}"])
        return EXIT_OK if ok else EXIT_ERROR
    except Exception as exc:
        output.emit(args, [f"provider={args.provider} error_class={type(exc).__name__}"])
        return EXIT_ERROR


# --------------------------------------------------------------------------- ci


def cmd_ci(args: argparse.Namespace) -> int:
    timeout = timebox.resolve_timeout(args.timeout)
    deadline = timebox.Deadline(timeout)
    workflow = getattr(args, "workflow", None)
    if getattr(args, "minutes", False):
        raw_limit = getattr(args, "limit", None)
        limit = max(1, min(int(raw_limit if raw_limit is not None else 30), 100))
        gh_args = [
            "run",
            "list",
            "-R",
            args.repo,
            "--status",
            "completed",
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion,name,createdAt",
        ]
        if workflow:
            gh_args.extend(["--workflow", workflow])
        runs = gh.json_or_empty(
            gh_args,
            timeout=max(5, min(20, deadline.remaining())),
        )
        if not isinstance(runs, list):
            runs = []
        if getattr(args, "run_id", None):
            runs = [r for r in runs if str(r.get("databaseId")) == str(args.run_id)]

        payloads: list[Any] = []
        truncated = False
        for r in runs:
            if deadline.expired():
                truncated = True
                break
            rid = r.get("databaseId") if isinstance(r, dict) else None
            if not rid:
                continue
            job_payload = gh.json_or_empty(
                ["api", f"repos/{args.repo}/actions/runs/{rid}/jobs?per_page=100"],
                timeout=max(5, min(15, deadline.remaining())),
            )
            if not isinstance(job_payload, dict) or "jobs" not in job_payload:
                truncated = True
                continue
            jobs_list = list(job_payload.get("jobs") or [])
            total_count = int(job_payload.get("total_count") or len(jobs_list))
            page = 2
            while len(jobs_list) < total_count:
                if deadline.expired():
                    truncated = True
                    break
                next_page = gh.json_or_empty(
                    ["api", f"repos/{args.repo}/actions/runs/{rid}/jobs?per_page=100&page={page}"],
                    timeout=max(5, min(15, deadline.remaining())),
                )
                page_jobs = next_page.get("jobs") if isinstance(next_page, dict) else None
                if not page_jobs:
                    truncated = True
                    break
                jobs_list.extend(page_jobs)
                page += 1
            payloads.append({"jobs": jobs_list})
        summary = ci_minutes.summarize(payloads, truncated=truncated, requested_runs=len(runs))
        lines = ci_minutes.format_summary(summary, repo=args.repo, workflow=workflow)
        output.emit(args, lines, data=summary)
        return EXIT_OK

    limit = getattr(args, "limit", None) or 15
    gh_args = [
        "run",
        "list",
        "-R",
        args.repo,
        "--limit",
        str(limit),
        "--json",
        "databaseId,status,conclusion,name,displayTitle,headBranch,createdAt",
    ]
    if workflow:
        gh_args.extend(["--workflow", workflow])
    runs = gh.json_or_empty(
        gh_args,
        timeout=max(5, min(20, deadline.remaining())),
    )
    if args.run_id:
        runs = [r for r in runs if str(r.get("databaseId")) == str(args.run_id)]

    lines = [f"CI runs for {args.repo}:"]
    for r in runs[: max(10, limit)]:
        lines.append(
            f"  #{r.get('databaseId')} {r.get('name')} [{r.get('status')}/{r.get('conclusion')}] "
            f"{r.get('headBranch')} {str(r.get('createdAt'))[:19]}"
        )

    failed = [r for r in runs if r.get("conclusion") == "failure"]
    for r in failed[:3]:
        rid = r["databaseId"]
        detail = gh.run(["run", "view", str(rid), "-R", args.repo, "--log-failed"], timeout=max(10, min(60, deadline.remaining())))
        text = detail.stdout or detail.stderr
        tail = "\n".join(text.splitlines()[-40:])
        lines.append(f"--- failed run #{rid} (last 40 log lines) ---")
        lines.append(tail)

    output.emit(args, lines, data={"runs": runs})
    return EXIT_ERROR if failed else EXIT_OK


# --------------------------------------------------------------------------- ship


_FIXES_RE = re.compile(r"(?:fixes|closes|resolves|refs)\s+#\d+", re.IGNORECASE)
PR_TEMPLATE_SECTIONS = (
    "## Adversarial Self-Review",
    "### 1. Per-Detail Corner-Case Audit",
    "### 2. Assumption & Simplification Audit",
)


def build_pr_body(body: str | None, issue: int | None) -> str:
    """Raises ValueError unless the body links an issue (`Fixes #N` / `Refs #N`) and, when structured, follows the template.

    A structured body (one with `## ` headings) must carry the `.github/PULL_REQUEST_TEMPLATE.md` audit sections.
    """
    body = body or ""
    if issue and not _FIXES_RE.search(body):
        body = f"Fixes #{issue}\n\n{body}".strip()
    if not _FIXES_RE.search(body):
        raise ValueError("PR body must contain 'Fixes #<issue_number>' (or 'Refs #<issue_number>')")
    if re.search(r"^## ", body, re.MULTILINE):
        missing = [s for s in PR_TEMPLATE_SECTIONS if s not in body]
        if missing:
            raise ValueError(f"PR body is missing .github/PULL_REQUEST_TEMPLATE.md sections: {missing}")
    return body


def _resolve_raw_pr_body(args: argparse.Namespace) -> str | None:
    body_arg = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    if body_arg is not None and body_file is not None:
        raise ValueError("--body and --body-file are mutually exclusive")
    if body_file is not None:
        if str(body_file).strip() == "-":
            stdin_body = sys.stdin.read()
            if not stdin_body.strip():
                raise ValueError("--body-file - received empty stdin")
            return stdin_body
        p = Path(str(body_file))
        if not p.is_file():
            raise ValueError(f"--body-file not found: {p}")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"--body-file is not valid UTF-8: {p} ({exc})") from exc
    return body_arg


def _is_jumbo_bookkeeping_path(path: str) -> bool:
    clean = path.strip().strip('"')
    if clean == "JUMBO_STATUS.md":
        return True
    if clean.startswith("issues/J") and clean.endswith(".md") and "/" not in clean[len("issues/") :]:
        return True
    return False


def _partition_porcelain_status(stdout: str) -> tuple[list[str], list[str]]:
    """Splits `git status --porcelain` lines into `(blocking_lines, nonblocking_jumbo_lines)`."""
    blocking: list[str] = []
    jumbo_nonblocking: list[str] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        index_flag = raw[0] if len(raw) >= 2 else ""
        path = raw[3:].strip() if len(raw) > 3 else raw.strip()
        if index_flag in (" ", "?") and _is_jumbo_bookkeeping_path(path):
            jumbo_nonblocking.append(raw)
        else:
            blocking.append(raw)
    return blocking, jumbo_nonblocking


def _wait_ci_run_for_head(
    deadline: timebox.Deadline,
    head_sha: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Waits on the `ci.yml` workflow run for `head_sha` only; returns `(ci_result, info_lines)`."""
    list_cmd = [
        "gh",
        "run",
        "list",
        "--commit",
        head_sha,
        "--workflow",
        "ci.yml",
        "--json",
        "databaseId,status,conclusion,name",
    ]
    last_err_res: subprocess.CompletedProcess[str] | None = None
    res = timebox.run(list_cmd, timeout=max(5.0, min(20.0, deadline.remaining())))
    while not deadline.expired():
        if res.returncode != 0:
            last_err_res = res
        else:
            try:
                runs = json.loads(res.stdout) if res.stdout.strip() else []
            except Exception:
                runs = []
            if isinstance(runs, list) and runs:
                # Prefer active or non-cancelled run if multiple exist for the same SHA
                chosen = next(
                    (
                        r
                        for r in runs
                        if isinstance(r, dict) and str(r.get("conclusion") or "").lower() != "cancelled"
                    ),
                    runs[0] if isinstance(runs[0], dict) else None,
                )
                if isinstance(chosen, dict):
                    status = str(chosen.get("status") or "").lower()
                    conclusion = str(chosen.get("conclusion") or "").lower()
                    run_id = chosen.get("databaseId")
                    run_name = chosen.get("name") or "ci.yml"
                    if status == "completed":
                        rc = 0 if conclusion == "success" else 1
                        summary = (
                            f"{run_name} #{run_id}: {conclusion}"
                            if rc == 0
                            else f"{run_name} #{run_id}: {conclusion} (expected success)"
                        )
                        info_lines = _collect_informational_checks(deadline, ci_run_id=run_id) if rc == 0 else []
                        return subprocess.CompletedProcess(list_cmd, rc, stdout=summary, stderr=""), info_lines
        deadline.sleep(3.0)
        if deadline.expired():
            break
        res = timebox.run(list_cmd, timeout=max(5.0, min(20.0, deadline.remaining())))
    if last_err_res is not None:
        return last_err_res, []
    return (
        subprocess.CompletedProcess(list_cmd, 124, stdout="", stderr="timed out waiting for ci.yml workflow run"),
        [],
    )


def _collect_informational_checks(deadline: timebox.Deadline, *, ci_run_id: Any = None) -> list[str]:
    """Queries `gh pr checks --json name,state,bucket,link` once without blocking."""
    if deadline.expired():
        return []
    res = timebox.run(
        ["gh", "pr", "checks", "--json", "name,state,bucket,link"],
        timeout=max(3.0, min(10.0, deadline.remaining())),
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        items = json.loads(res.stdout)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    run_marker = f"/actions/runs/{ci_run_id}" if ci_run_id else ""
    info: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        link = str(item.get("link") or "").strip()
        if run_marker and link and run_marker in link:
            continue
        if not link and name.lower() in ("test", "python ci"):
            continue
        state_val = str(item.get("state") or "").strip().lower()
        bucket = str(item.get("bucket") or "").strip().lower()
        if bucket == "pending" or state_val in ("pending", "in_progress", "queued", "waiting"):
            info.append(f"{name}: pending — run 'atobar-ops review <pr> --wait' if you want it")
        else:
            info.append(f"{name}: {state_val or bucket} (informational)")
    return info


def _wait_pr_checks(deadline: timebox.Deadline, head_sha: str) -> subprocess.CompletedProcess[str]:
    ci_res, info_lines = _wait_ci_run_for_head(deadline, head_sha)
    if info_lines:
        merged_out = "\n".join([ci_res.stdout.strip(), *info_lines]).strip()
        return subprocess.CompletedProcess(ci_res.args, ci_res.returncode, stdout=merged_out, stderr=ci_res.stderr)
    return ci_res


def _append_failed_ci_tail(lines: list[str], deadline: timebox.Deadline, repo: str = "atobar-flow/atobar-flow") -> None:
    runs = gh.json_or_empty(
        [
            "run",
            "list",
            "-R",
            repo,
            "--limit",
            "5",
            "--json",
            "databaseId,status,conclusion,name",
        ],
        timeout=max(5.0, min(15.0, deadline.remaining())),
    )
    if not isinstance(runs, list):
        return
    failed = [r for r in runs if isinstance(r, dict) and r.get("conclusion") == "failure"]
    for r in failed[:2]:
        rid = r.get("databaseId")
        if not rid:
            continue
        detail = gh.run(
            ["run", "view", str(rid), "-R", repo, "--log-failed"],
            timeout=max(5.0, min(30.0, deadline.remaining())),
        )
        text = detail.stdout or detail.stderr
        tail = "\n".join(text.splitlines()[-40:])
        lines.append(f"--- failed run #{rid} ({r.get('name')}) (last 40 log lines) ---")
        if tail:
            lines.append(tail)


def cmd_ship(args: argparse.Namespace) -> int:
    try:
        raw_body = _resolve_raw_pr_body(args)
    except ValueError as exc:
        output.emit(args, [f"REFUSED: {exc}"])
        return EXIT_ERROR

    jumbo_note: str | None = None
    status_res = timebox.run(["git", "status", "--porcelain", "-uno"], timeout=15)
    if status_res.returncode == 0 and status_res.stdout.strip():
        blocking_dirty, jumbo_dirty = _partition_porcelain_status(status_res.stdout)
        if blocking_dirty:
            output.emit(
                args,
                [
                    "REFUSED: uncommitted tracked changes in working tree; commit or stash before shipping",
                    *blocking_dirty[:20],
                ],
            )
            return EXIT_ERROR
        if jumbo_dirty:
            jumbo_note = "note: uncommitted Jumbo bookkeeping (not part of this PR)"

    existing = timebox.run(["gh", "pr", "view", "--json", "url,state"], timeout=15)
    existing_compact = existing.stdout.replace(" ", "")
    has_open_pr = bool(
        existing.returncode == 0
        and existing.stdout.strip()
        and '"state":"CLOSED"' not in existing_compact
        and '"state":"MERGED"' not in existing_compact
    )
    has_explicit_body = raw_body is not None
    body = ""
    if not has_open_pr or has_explicit_body:
        try:
            body = build_pr_body(raw_body, args.issue)
        except ValueError as exc:
            output.emit(args, [f"REFUSED: {exc}"])
            return EXIT_ERROR

    verify_rc, verify_lines, _ = verify.run_verify(args, changed=False, require_gitleaks=True)
    lines = list(verify_lines)
    if jumbo_note:
        lines.insert(0, jumbo_note)
    if verify_rc != EXIT_OK:
        output.emit(args, ["REFUSED: verification failing, not shipping", *lines])
        return verify_rc

    push_res = timebox.run(["git", "push", "-u", "origin", "HEAD"], timeout=60)
    lines.append(f"push: rc={push_res.returncode} {push_res.stderr.strip()[-200:]}")
    if push_res.returncode != 0:
        output.emit(args, ["REFUSED: git push failed", *lines])
        return EXIT_ERROR

    rev_res = timebox.run(["git", "rev-parse", "HEAD"], timeout=10)
    head_sha = rev_res.stdout.strip() if rev_res.returncode == 0 else ""
    if len(head_sha) < 7:
        output.emit(args, ["REFUSED: could not resolve HEAD SHA after push", *lines])
        return EXIT_ERROR

    if has_open_pr:
        pr_res = existing
        if args.title or has_explicit_body:
            edit_cmd = ["gh", "pr", "edit"]
            if args.title:
                edit_cmd.extend(["--title", str(args.title)])
            if has_explicit_body:
                edit_cmd.extend(["--body", body])
            edit_res = timebox.run(edit_cmd, timeout=30)
            lines.append(f"PR updated (rc={edit_res.returncode}): {existing.stdout.strip()}")
            if edit_res.returncode != 0:
                pr_res = edit_res
        else:
            lines.append(f"PR already exists: {existing.stdout.strip()}")
    else:
        title = args.title or "atobar-ops change"
        pr_res = timebox.run(
            ["gh", "pr", "create", "--title", title, "--body", body],
            timeout=30,
        )
        lines.append(pr_res.stdout.strip() or pr_res.stderr.strip())

    if pr_res.returncode != 0:
        output.emit(args, lines)
        return EXIT_ERROR

    deadline = timebox.Deadline(timebox.resolve_timeout(args.timeout))
    checks = _wait_pr_checks(deadline, head_sha=head_sha)
    lines.append(f"checks: rc={checks.returncode}")
    if checks.stdout.strip():
        lines.append("\n".join(checks.stdout.strip().splitlines()[-15:]))
    if checks.returncode != 0:
        if checks.returncode == 124 or deadline.expired():
            output.emit(
                args,
                [
                    "PR opened/updated but checks did not complete within timeout"
                    + ("; not merging" if args.merge else ""),
                    *lines,
                ],
            )
            return EXIT_TIMEOUT
        _append_failed_ci_tail(lines, deadline)
        output.emit(args, ["PR checks failed" + ("; not merging" if args.merge else ""), *lines])
        return EXIT_ERROR

    if args.merge:
        merge_res = timebox.run(["gh", "pr", "merge", "--squash", "--delete-branch"], timeout=30)
        lines.append(f"merge: rc={merge_res.returncode} {merge_res.stdout.strip()}")
        if merge_res.returncode != 0:
            output.emit(args, ["REFUSED: gh pr merge failed; PR not merged", *lines, merge_res.stderr.strip()[-300:]])
            return EXIT_ERROR
        timebox.run(["git", "checkout", "main"], timeout=15)
        timebox.run(["git", "pull"], timeout=30)

    output.emit(args, lines)
    return EXIT_OK


# --------------------------------------------------------------------------- review


_REVIEW_GQL = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      number
      title
      state
      url
      headRefOid
      author { login }
      reviews(last: 50) {
        nodes {
          author { login }
          state
          submittedAt
          body
          commit { oid }
        }
      }
      comments(last: 20) {
        nodes {
          author { login }
          body
          createdAt
        }
      }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 20) {
            pageInfo { hasNextPage endCursor }
            nodes {
              author { login }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


_REVIEW_THREAD_COMMENTS_GQL = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""


def _fetch_remaining_thread_comments(thread: dict[str, Any], deadline: timebox.Deadline) -> None:
    """Appends the thread's comments beyond the first GraphQL page into `thread["comments"]["nodes"]` (bounded)."""
    block = thread.get("comments") or {}
    nodes = list(block.get("nodes") or [])
    info = block.get("pageInfo") or {}
    cursor = info.get("endCursor")
    while info.get("hasNextPage") and cursor and thread.get("id") and not deadline.expired():
        res = gh.run(
            [
                "api",
                "graphql",
                "-f",
                f"query={_REVIEW_THREAD_COMMENTS_GQL}",
                "-f",
                f"id={thread['id']}",
                "-f",
                f"cursor={cursor}",
            ],
            timeout=max(5.0, min(20.0, deadline.remaining())),
        )
        if res.returncode != 0:
            break
        try:
            page = json.loads(res.stdout)["data"]["node"]["comments"]
        except Exception:
            break
        nodes.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        cursor = info.get("endCursor")
    thread["comments"] = {"nodes": nodes, "pageInfo": info}


def _compact_snippet(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _strip_review_markup(body: str) -> str:
    """Drops generated review metadata (Devin badge block, HTML comments, <details> wrappers) before budgeting.

    Everything else (e.g. `<script>`, `<picture>` examples, `List<str>`, `<https://...>`) is kept as evidence.
    Falls back to the raw body if nothing would remain.
    """
    raw = body.strip()
    # Markdown code (fenced blocks / inline spans) is quoted evidence and is never stripped.
    parts = re.split(r"(```.*?(?:```|$)|`[^`\n]*`)", body, flags=re.DOTALL)
    for i in range(0, len(parts), 2):
        seg = re.sub(r"<!-- devin-review-badge-begin -->.*?<!-- devin-review-badge-end -->", "", parts[i], flags=re.DOTALL)
        seg = re.sub(r"<!--.*?-->", "", seg, flags=re.DOTALL)
        parts[i] = re.sub(r"</?(?:details|summary)\b[^>]*>", "", seg, flags=re.IGNORECASE)
    body = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    return body or raw


_NON_REVIEW_BOTS = {"terraform-cloud", "github-actions", "github-actions[bot]"}


def _compute_review_round(pr_data: dict[str, Any]) -> int:
    """Computes the review round number from distinct reviewed commit OIDs in submission order."""
    reviews_raw = (pr_data.get("reviews") or {}).get("nodes") or []
    distinct_oids: list[str] = []
    valid_count = 0
    for rev in reviews_raw:
        if not isinstance(rev, dict):
            continue
        state_val = str(rev.get("state") or "")
        body_val = str(rev.get("body") or "").strip()
        if state_val == "PENDING" or (not body_val and state_val not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")):
            continue
        valid_count += 1
        oid = str(((rev.get("commit") or {}).get("oid")) or "").strip()
        if oid and oid not in distinct_oids:
            distinct_oids.append(oid)

    return max(len(distinct_oids), 1 if valid_count else 0)


def _resolve_head_push_timestamp(owner: str, name: str, head_oid: str, deadline: timebox.Deadline) -> str:
    """Resolves the push timestamp of `head_oid` from the earliest check-suite `created_at`."""
    if not head_oid or deadline.expired():
        return ""
    res = gh.run(
        ["api", f"repos/{owner}/{name}/commits/{head_oid}/check-suites"],
        timeout=max(3.0, min(10.0, deadline.remaining())),
    )
    if res.returncode != 0 or not res.stdout.strip():
        return ""
    try:
        payload = json.loads(res.stdout)
    except Exception:
        return ""
    suites = payload.get("check_suites") if isinstance(payload, dict) else None
    if not isinstance(suites, list):
        return ""
    created = [
        str(s.get("created_at") or "").strip()
        for s in suites
        if isinstance(s, dict) and str(s.get("created_at") or "").strip()
    ]
    return min(created) if created else ""


def _resolve_viewer_login(deadline: timebox.Deadline) -> str:
    if deadline.expired():
        return ""
    res = gh.run(["api", "user"], timeout=max(3.0, min(8.0, deadline.remaining())))
    if res.returncode != 0 or not res.stdout.strip():
        return ""
    try:
        payload = json.loads(res.stdout)
    except Exception:
        return ""
    return str(payload.get("login") or "").strip() if isinstance(payload, dict) else ""


def _has_review_since_latest_push(
    pr_data: dict[str, Any],
    *,
    until_reviewer: str | None = None,
    head_push_ts: str = "",
    excluded_authors: set[str] | None = None,
) -> bool:
    """Returns True if a review on `headRefOid` or a post-push comment from a reviewer arrived."""
    raw_filter = (until_reviewer or "").strip()
    allow_any_author = raw_filter == "*"
    needle = "" if allow_any_author else raw_filter.lower()
    excluded = {a.lower() for a in (excluded_authors or set()) if a} | _NON_REVIEW_BOTS

    def _matches_author(author: str) -> bool:
        low = author.strip().lower()
        if not low:
            return False
        if allow_any_author:
            return True
        if needle:
            return needle in low
        return low not in excluded

    head_oid = str(pr_data.get("headRefOid") or "").strip()

    for rev in (pr_data.get("reviews") or {}).get("nodes") or []:
        if not isinstance(rev, dict):
            continue
        author = str(((rev.get("author") or {}).get("login")) or "")
        if not _matches_author(author):
            continue
        rev_oid = str(((rev.get("commit") or {}).get("oid")) or "").strip()
        if head_oid:
            if rev_oid == head_oid:
                return True
        elif rev.get("body") or rev.get("state"):
            return True

    for pr_c in (pr_data.get("comments") or {}).get("nodes") or []:
        if not isinstance(pr_c, dict):
            continue
        author = str(((pr_c.get("author") or {}).get("login")) or "")
        if not _matches_author(author):
            continue
        c_ts = str(pr_c.get("createdAt") or "").strip()
        if head_push_ts and c_ts >= head_push_ts:
            return True
        if not head_oid and not head_push_ts and pr_c.get("body"):
            return True

    for th in (pr_data.get("reviewThreads") or {}).get("nodes") or []:
        if not isinstance(th, dict):
            continue
        for c in ((th.get("comments") or {}).get("nodes")) or []:
            if not isinstance(c, dict):
                continue
            author = str(((c.get("author") or {}).get("login")) or "")
            if not _matches_author(author):
                continue
            c_ts = str(c.get("createdAt") or "").strip()
            if head_push_ts and c_ts >= head_push_ts:
                return True
            if not head_oid and not head_push_ts and c.get("body"):
                return True

    return False


def cmd_review(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None) or creds.factory_repo()
    if "/" not in repo:
        output.emit(args, [f"invalid repo (expected owner/name): {repo}"])
        return EXIT_ERROR
    owner, name = repo.split("/", 1)
    pr_num = int(args.pr)
    unresolved_only = bool(getattr(args, "unresolved", False))
    wait_mode = bool(getattr(args, "wait", False))
    until_reviewer = getattr(args, "until_reviewer", None)
    if wait_mode:
        args.verbose = True
        raw_timeout = getattr(args, "timeout", None)
        timeout = min(240, timebox.resolve_timeout(raw_timeout))
    else:
        timeout = timebox.resolve_timeout(getattr(args, "timeout", None))
    deadline = timebox.Deadline(timeout)

    gql_args = [
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_GQL}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"pr={pr_num}",
    ]

    viewer_login = _resolve_viewer_login(deadline) if wait_mode and not until_reviewer else ""
    cached_push_ts: dict[str, str] = {}

    pr_data: dict[str, Any] | None = None
    while True:
        res = gh.run(gql_args, timeout=max(5.0, min(30.0, deadline.remaining())))
        if res.returncode != 0:
            output.emit(args, [f"gh api graphql failed: {res.stderr.strip()[:300]}"])
            return EXIT_ERROR

        try:
            payload = json.loads(res.stdout)
            pr_data = payload["data"]["repository"]["pullRequest"]
        except Exception as exc:
            output.emit(args, [f"failed to parse PR #{pr_num} review response: {exc}"])
            return EXIT_ERROR

        if not pr_data:
            output.emit(args, [f"PR #{pr_num} not found in {repo}"])
            return EXIT_ERROR

        if not wait_mode:
            break

        head_oid = str(pr_data.get("headRefOid") or "").strip()
        if head_oid and head_oid not in cached_push_ts:
            cached_push_ts[head_oid] = _resolve_head_push_timestamp(owner, name, head_oid, deadline)
        pr_author = str(((pr_data.get("author") or {}).get("login")) or "").strip()
        excluded_authors = {a for a in (pr_author, viewer_login) if a}

        if _has_review_since_latest_push(
            pr_data,
            until_reviewer=until_reviewer,
            head_push_ts=cached_push_ts.get(head_oid, ""),
            excluded_authors=excluded_authors,
        ):
            break
        if deadline.expired():
            output.emit(
                args,
                [
                    f"PR #{pr_num}: timed out after {timeout}s waiting for review"
                    + (f" matching {until_reviewer!r}" if until_reviewer else ""),
                    "NEXT: re-run once with --wait",
                ],
            )
            return EXIT_TIMEOUT
        deadline.sleep(min(15.0, deadline.remaining()))
        if deadline.expired():
            output.emit(
                args,
                [
                    f"PR #{pr_num}: timed out after {timeout}s waiting for review"
                    + (f" matching {until_reviewer!r}" if until_reviewer else ""),
                    "NEXT: re-run once with --wait",
                ],
            )
            return EXIT_TIMEOUT

    reviews_raw = (pr_data.get("reviews") or {}).get("nodes") or []
    pr_comments_raw = (pr_data.get("comments") or {}).get("nodes") or []
    threads_block = pr_data.get("reviewThreads") or {}
    threads_raw = list(threads_block.get("nodes") or [])
    page_info = threads_block.get("pageInfo") or {}
    has_more_threads = bool(page_info.get("hasNextPage"))
    end_cursor = page_info.get("endCursor")
    while len(threads_raw) < 100 and has_more_threads and end_cursor and not deadline.expired():
        next_res = gh.run(
            [*gql_args, "-F", f"cursor={end_cursor}"],
            timeout=max(5.0, min(20.0, deadline.remaining())),
        )
        if next_res.returncode != 0:
            break
        try:
            next_pr = json.loads(next_res.stdout)["data"]["repository"]["pullRequest"]
            next_block = (next_pr or {}).get("reviewThreads") or {}
            threads_raw.extend(next_block.get("nodes") or [])
            page_info = next_block.get("pageInfo") or {}
            has_more_threads = bool(page_info.get("hasNextPage"))
            end_cursor = page_info.get("endCursor")
        except Exception:
            break
    is_truncated = has_more_threads or len(threads_raw) > 100

    latest_state_by_author: dict[str, str] = {}
    top_reviews: list[dict[str, str]] = []
    for rev in reviews_raw:
        if not isinstance(rev, dict):
            continue
        author = str(((rev.get("author") or {}).get("login")) or "unknown")
        state_val = str(rev.get("state") or "COMMENTED")
        body_val = _strip_review_markup(str(rev.get("body") or ""))
        if state_val != "COMMENTED" or author not in latest_state_by_author:
            latest_state_by_author[author] = state_val
        if body_val:
            top_reviews.append(
                {
                    "author": author,
                    "state": state_val,
                    "submittedAt": str(rev.get("submittedAt") or "")[:19],
                    "body": body_val,
                }
            )
    for pr_c in pr_comments_raw:
        if not isinstance(pr_c, dict):
            continue
        author = str(((pr_c.get("author") or {}).get("login")) or "unknown")
        body_val = _strip_review_markup(str(pr_c.get("body") or ""))
        if body_val:
            top_reviews.append(
                {
                    "author": author,
                    "state": "PR_COMMENT",
                    "submittedAt": str(pr_c.get("createdAt") or "")[:19],
                    "body": body_val,
                }
            )

    threads: list[dict[str, Any]] = []
    for t in threads_raw[:100]:
        if not isinstance(t, dict):
            continue
        is_resolved = bool(t.get("isResolved"))
        if unresolved_only and is_resolved:
            continue
        _fetch_remaining_thread_comments(t, deadline)
        comments_nodes = ((t.get("comments") or {}).get("nodes")) or []
        parsed_comments = [
            {
                "author": str(((c.get("author") or {}).get("login")) or "unknown"),
                "body": _strip_review_markup(str(c.get("body") or "")),
                "createdAt": str(c.get("createdAt") or "")[:19],
            }
            for c in comments_nodes
            if isinstance(c, dict) and _strip_review_markup(str(c.get("body") or ""))
        ]
        if not parsed_comments:
            continue
        threads.append(
            {
                "path": str(t.get("path") or ""),
                "line": t.get("line") if t.get("line") is not None else t.get("originalLine"),
                "resolved": is_resolved,
                "outdated": bool(t.get("isOutdated")),
                "comments": parsed_comments,
            }
        )

    round_num = _compute_review_round(pr_data)
    max_chars = output.budget(args)

    def _header(shown: int) -> list[str]:
        omitted = len(threads) - shown
        hdr = [
            f"PR #{pr_data.get('number')} [{pr_data.get('state')}] {pr_data.get('title')}",
        ]
        if round_num >= 2:
            hdr.append(
                f"ROUND {round_num} — you already pushed a fix after review {round_num - 1}; "
                "STOP and report unless the user asked for another round"
            )
        hdr.extend(
            [
                "Reviewers: "
                + (
                    ", ".join(f"{a}={s}" for a, s in latest_state_by_author.items())
                    if latest_state_by_author
                    else "none"
                ),
                (f"Threads: {shown} of {len(threads)} shown" if omitted else f"Threads: {len(threads)} shown")
                + (" (unresolved only)" if unresolved_only else "")
                + (" (truncated)" if is_truncated else "")
                + (f" ({omitted} omitted; rerun with --verbose or --max-chars)" if omitted else ""),
            ]
        )
        return hdr

    header_lines = _header(len(threads))
    full_lines = list(header_lines)
    for r in top_reviews:
        full_lines.append(f"--- Review by @{r['author']} [{r['state']}] ({r['submittedAt']}) ---")
        full_lines.append(r["body"])
    for idx, th in enumerate(threads, 1):
        flags = ["resolved" if th["resolved"] else "unresolved"]
        if th["outdated"]:
            flags.append("outdated")
        loc = f"{th['path']}:{th['line']}" if th["line"] is not None else th["path"]
        first = th["comments"][0]
        full_lines.append(f"[{idx}/{len(threads)}] [{','.join(flags)}] {loc} (@{first['author']}) — {first['body']}")
        for reply in th["comments"][1:]:
            full_lines.append(f"    ↳ @{reply['author']}: {reply['body']}")

    if len("\n".join(full_lines)) <= max_chars:
        lines = full_lines
    else:
        lines = list(header_lines)
        remaining_budget = max(400, max_chars - len("\n".join(lines)) - 350)
        review_share = int(remaining_budget * (0.24 if threads else 0.85))
        thread_share = max(200, remaining_budget - review_share)
        per_review = max(100, review_share // max(1, len(top_reviews)))
        per_thread = max(60, (thread_share // max(1, len(threads))) - 85)
        review_lines: list[str] = []
        for r in top_reviews:
            review_lines.append(f"--- Review by @{r['author']} [{r['state']}] ({r['submittedAt']}) ---")
            review_lines.append(_compact_snippet(r["body"], per_review))
        thread_lines: list[str] = []
        for idx, th in enumerate(threads, 1):
            flags = ["resolved" if th["resolved"] else "unresolved"]
            if th["outdated"]:
                flags.append("outdated")
            loc = f"{th['path']}:{th['line']}" if th["line"] is not None else th["path"]
            joined_body = " | ↳ ".join(
                (c["body"] if i == 0 else f"@{c['author']}: {c['body']}")
                for i, c in enumerate(th["comments"])
            )
            first_author = th["comments"][0]["author"]
            thread_lines.append(
                f"[{idx}/{len(threads)}] [{','.join(flags)}] {loc} (@{first_author}) — "
                f"{_compact_snippet(joined_body, per_thread)}"
            )
        # Worst-case header (most omitted) bounds the budget; redaction slack keeps output.emit from cutting.
        room = max_chars - len("\n".join([*_header(0), *review_lines])) - 64
        shown_lines: list[str] = []
        for tl in thread_lines:
            if len(tl) + 1 > room:
                break
            shown_lines.append(tl)
            room -= len(tl) + 1
        lines = [*_header(len(shown_lines)), *review_lines, *shown_lines]

    output.emit(
        args,
        lines,
        data={
            "pr": pr_num,
            "state": pr_data.get("state"),
            "round": round_num,
            "latest_reviews": latest_state_by_author,
            "reviews": top_reviews,
            "threads": threads,
            "truncated_threads": is_truncated,
        },
    )
    return EXIT_OK


# --------------------------------------------------------------------------- jumbo


JUMBO_TEMPLATE = """# J{number} ({slug}): {title}

## Goal
{title}

## Context
{context}

## Work Packages
- [ ] TBD

## Acceptance
- [ ] TBD

## Risks
- TBD
"""


_JUMBO_NUM_RE = re.compile(r"\bJ(\d+)\b")


def _normalize_jumbo_number(raw: Any) -> str:
    cleaned = str(raw or "").strip().lstrip("Jj").strip()
    if not cleaned.isdigit():
        raise ValueError(f"invalid Jumbo number: {raw!r}")
    return cleaned


def _collect_known_jumbo_numbers(
    issues_dir: Path,
    existing_status: str,
    remote_issues: list[Any],
) -> set[int]:
    nums: set[int] = set()
    if issues_dir.is_dir():
        for p in issues_dir.glob("J*-*.md"):
            m = re.match(r"^J(\d+)-", p.name)
            if m:
                nums.add(int(m.group(1)))
    for m in _JUMBO_NUM_RE.finditer(existing_status):
        nums.add(int(m.group(1)))
    for it in remote_issues:
        if isinstance(it, dict):
            for m in _JUMBO_NUM_RE.finditer(str(it.get("title") or "")):
                nums.add(int(m.group(1)))
    return nums


def _rewrite_jumbo_heading(body: str, num: str) -> str:
    return re.sub(r"^(#\s+J)\d+\b", rf"\g<1>{num}", body, count=1, flags=re.MULTILINE)


def cmd_jumbo_new(args: argparse.Namespace) -> int:
    raw_num_arg = str(getattr(args, "number", "") or "").strip()
    is_auto = raw_num_arg.lower() == "auto"
    num = ""
    if not is_auto:
        try:
            num = _normalize_jumbo_number(raw_num_arg)
        except ValueError as exc:
            output.emit(args, [f"REFUSED: {exc}"])
            return EXIT_ERROR

    clean_title = re.sub(r"^[Jj]\d+\s*:\s*", "", str(args.title or "").strip())
    if not clean_title:
        output.emit(args, ["REFUSED: --title cannot be empty"])
        return EXIT_ERROR

    from_file = getattr(args, "from_file", None)
    custom_body: str | None = None
    if from_file is not None:
        if str(from_file).strip() == "-":
            custom_body = sys.stdin.read()
            if not custom_body.strip():
                output.emit(args, ["REFUSED: --from-file - received empty stdin"])
                return EXIT_ERROR
        else:
            src_path = Path(str(from_file))
            if not src_path.is_file():
                output.emit(args, [f"REFUSED: --from-file not found: {src_path}"])
                return EXIT_ERROR
            try:
                custom_body = src_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                output.emit(args, [f"REFUSED: --from-file is not valid UTF-8: {src_path} ({exc})"])
                return EXIT_ERROR

    issues_dir = Path("issues")
    status_path = Path("JUMBO_STATUS.md")
    existing_status = status_path.read_text(encoding="utf-8") if status_path.is_file() else ""

    target_factory_repo = creds.factory_repo()
    remote_state = "all" if is_auto else "open"
    remote_issues = gh.json_or_empty(
        [
            "issue",
            "list",
            "-R",
            target_factory_repo,
            "--label",
            "jumbo",
            "--state",
            remote_state,
            "--limit",
            "100",
            "--json",
            "number,title,url",
        ],
        timeout=20,
    )
    remote_list = remote_issues if isinstance(remote_issues, list) else []

    if is_auto:
        known = _collect_known_jumbo_numbers(issues_dir, existing_status, remote_list)
        num = str(max(known, default=0) + 1)

    existing_local = sorted(issues_dir.glob(f"J{num}-*.md")) if issues_dir.is_dir() else []
    path = issues_dir / f"J{num}-{args.slug}.md"
    if path.exists() or existing_local:
        hit = existing_local[0] if existing_local else path
        output.emit(args, [f"REFUSED: local Jumbo file for J{num} already exists: {hit}"])
        return EXIT_ERROR

    if re.search(rf"\bJ{re.escape(num)}\b", existing_status):
        output.emit(args, [f"REFUSED: J{num} already appears in JUMBO_STATUS.md"])
        return EXIT_ERROR

    for it in remote_list:
        if isinstance(it, dict) and re.search(rf"\bJ{re.escape(num)}\b", str(it.get("title") or "")):
            output.emit(
                args,
                [f"REFUSED: J{num} already exists in open GitHub issue #{it.get('number')}: {it.get('title')}"],
            )
            return EXIT_ERROR

    def _render_body(n: str) -> str:
        if custom_body is not None:
            return _rewrite_jumbo_heading(custom_body, n) if is_auto else custom_body
        return JUMBO_TEMPLATE.format(number=n, slug=args.slug, title=clean_title, context=args.context or "")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_body(num), encoding="utf-8")

    expected_title = f"J{num}: {clean_title}"
    res = gh.run(
        [
            "issue",
            "create",
            "-R",
            target_factory_repo,
            "--title",
            expected_title,
            "--label",
            "jumbo",
            "--body-file",
            str(path),
        ],
        timeout=30,
    )
    issue_url = res.stdout.strip()
    if res.returncode != 0:
        # A timeout/transport error is ambiguous: GitHub may have created the issue anyway, so reconcile first.
        try:
            found = gh.json_or_raise(
                [
                    "issue",
                    "list",
                    "-R",
                    target_factory_repo,
                    "--label",
                    "jumbo",
                    "--state",
                    "all",
                    "--limit",
                    "100",
                    "--json",
                    "number,title,url",
                ],
                timeout=20,
            )
        except Exception as exc:
            output.emit(
                args,
                [
                    f"gh issue create failed (rc={res.returncode}) and the remote state could not be confirmed ({exc}).",
                    f"Kept {path}; JUMBO_STATUS.md untouched. Check GitHub for J{num}, then add its status line "
                    f"(and run `jumbo sync {num}`) or delete {path} and retry.",
                    res.stderr.strip()[-300:],
                ],
            )
            return EXIT_ERROR
        match = next(
            (
                it
                for it in (found if isinstance(found, list) else [])
                if isinstance(it, dict) and str(it.get("title") or "").strip() == expected_title
            ),
            None,
        )
        if match is None:
            path.unlink(missing_ok=True)
            output.emit(
                args,
                [f"gh issue create failed (rc={res.returncode}); removed {path}, JUMBO_STATUS.md untouched", res.stderr.strip()[-300:]],
            )
            return EXIT_ERROR
        issue_url = str(match.get("url") or f"#{match.get('number')}")

    out_lines: list[str] = []
    # Post-create race arbitration for `auto` mode: lowest GitHub issue number keeps J<num>; loser re-allocates.
    if is_auto:
        m_created = re.search(r"/issues/(\d+)|^#(\d+)$", issue_url)
        created_issue_num = int(m_created.group(1) or m_created.group(2)) if m_created else None
        post_issues = gh.json_or_empty(
            [
                "issue",
                "list",
                "-R",
                target_factory_repo,
                "--label",
                "jumbo",
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,title,url",
            ],
            timeout=20,
        )
        post_list = post_issues if isinstance(post_issues, list) else []
        same_j_nums = sorted(
            int(it["number"])
            for it in post_list
            if isinstance(it, dict)
            and str(it.get("number") or "").isdigit()
            and re.search(rf"\bJ{re.escape(num)}\b", str(it.get("title") or ""))
        )
        if created_issue_num is not None and len(same_j_nums) > 1 and created_issue_num != same_j_nums[0]:
            old_num = num
            known = _collect_known_jumbo_numbers(issues_dir, existing_status, post_list)
            num = str(max(known, default=int(old_num)) + 1)
            new_path = issues_dir / f"J{num}-{args.slug}.md"
            new_path.write_text(_render_body(num), encoding="utf-8")
            path.unlink(missing_ok=True)
            path = new_path
            gh.run(
                [
                    "issue",
                    "edit",
                    str(created_issue_num),
                    "-R",
                    target_factory_repo,
                    "--title",
                    f"J{num}: {clean_title}",
                    "--body-file",
                    str(path),
                ],
                timeout=30,
            )
            out_lines.append(f"re-allocated J{old_num} -> J{num}")
        else:
            out_lines.append(f"allocated: J{num}")

    new_line = f"- J{num}: {clean_title} ({issue_url})\n"
    status_path.write_text(new_line + existing_status, encoding="utf-8")

    out_lines.extend([f"created {path}", f"issue: {issue_url}", "updated JUMBO_STATUS.md"])
    output.emit(args, out_lines)
    return EXIT_OK


def cmd_jumbo_sync(args: argparse.Namespace) -> int:
    try:
        num = _normalize_jumbo_number(args.number)
    except ValueError as exc:
        output.emit(args, [f"REFUSED: {exc}"])
        return EXIT_ERROR

    issues_dir = Path("issues")
    matches = sorted(issues_dir.glob(f"J{num}-*.md")) if issues_dir.is_dir() else []
    if not matches:
        output.emit(args, [f"REFUSED: no local file matching issues/J{num}-*.md"])
        return EXIT_ERROR
    if len(matches) > 1:
        output.emit(args, [f"REFUSED: multiple local files matching issues/J{num}-*.md: {matches}"])
        return EXIT_ERROR
    local_path = matches[0]

    issue_number: str | None = None
    status_path = Path("JUMBO_STATUS.md")
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if re.search(rf"\bJ{re.escape(num)}\b", line):
                m_url = re.search(r"/issues/(\d+)", line)
                m_hash = re.search(r"#(\d+)", line)
                if m_url:
                    issue_number = m_url.group(1)
                    break
                if m_hash:
                    issue_number = m_hash.group(1)
                    break

    if not issue_number:
        text = local_path.read_text(encoding="utf-8")
        m_file = re.search(r"/issues/(\d+)|\[#(\d+)\]", text)
        if m_file:
            issue_number = m_file.group(1) or m_file.group(2)

    target_factory_repo = creds.factory_repo()
    if not issue_number:
        remote_issues = gh.json_or_empty(
            [
                "issue",
                "list",
                "-R",
                target_factory_repo,
                "--label",
                "jumbo",
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,title",
            ],
            timeout=20,
        )
        if isinstance(remote_issues, list):
            for it in remote_issues:
                if isinstance(it, dict) and re.search(rf"\bJ{re.escape(num)}\b", str(it.get("title") or "")):
                    issue_number = str(it.get("number"))
                    break

    if not issue_number:
        output.emit(args, [f"REFUSED: could not resolve GitHub issue number for J{num}"])
        return EXIT_ERROR

    res = gh.run(
        ["issue", "edit", str(issue_number), "-R", target_factory_repo, "--body-file", str(local_path)],
        timeout=30,
    )
    if res.returncode != 0:
        output.emit(args, [f"gh issue edit #{issue_number} failed: {res.stderr.strip()[:300]}"])
        return EXIT_ERROR
    output.emit(args, [f"synced {local_path} -> issue #{issue_number}"])
    return EXIT_OK


# --------------------------------------------------------------------------- argparse wiring


_COMMON_OPTION_FLAGS = {"-h", "--help", "--timeout", "--json", "--verbose", "--max-chars"}


def _format_subparser_signature(cmd_path: str, sp: argparse.ArgumentParser) -> str:
    tokens: list[str] = [cmd_path]
    for action in sp._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if any(opt in _COMMON_OPTION_FLAGS for opt in action.option_strings):
            continue
        if not action.option_strings:
            label = action.metavar or f"<{action.dest}>"
            if action.nargs == "?":
                tokens.append(f"[{label}]")
            else:
                tokens.append(str(label))
        else:
            flag = action.option_strings[-1]
            if action.nargs == 0:
                tokens.append(f"[{flag}]")
            elif action.choices:
                joined = "\\|".join(str(c) for c in action.choices)
                tokens.append(f"[{flag} {joined}]")
            else:
                meta = action.metavar or action.dest.upper()
                tokens.append(f"{flag} {meta}" if action.required else f"[{flag} {meta}]")
    return " ".join(tokens)


def render_markdown_command_table(parser: argparse.ArgumentParser | None = None) -> str:
    """Builds the Markdown command reference table directly from `build_parser()`."""
    root = parser or build_parser()
    rows: list[tuple[str, str]] = []

    def _walk(prefix: str, p: argparse.ArgumentParser) -> None:
        sub_actions = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not sub_actions:
            return
        sub_action = sub_actions[0]
        help_by_name = {ca.dest: (ca.help or "") for ca in sub_action._choices_actions}
        for name, child in sub_action.choices.items():
            full_name = f"{prefix} {name}".strip()
            child_subs = [a for a in child._actions if isinstance(a, argparse._SubParsersAction)]
            if child_subs:
                _walk(full_name, child)
            else:
                sig = _format_subparser_signature(full_name, child)
                desc = (help_by_name.get(name) or child.description or "").strip()
                rows.append((sig, desc))

    _walk("", root)
    out = ["| Command | Use it to |", "|---|---|"]
    for sig, desc in rows:
        safe_sig = re.sub(r"(?<!\\)\|", r"\|", sig)
        safe_desc = re.sub(r"(?<!\\)\|", r"\|", desc)
        out.append(f"| `{safe_sig}` | {safe_desc} |")
    return "\n".join(out)


def cmd_help(args: argparse.Namespace) -> int:
    table = render_markdown_command_table()
    if getattr(args, "markdown", False):
        print(table)
        return EXIT_OK
    output.emit(args, [table])
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atobar-ops", description="Unified ops CLI for atobar-flow")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--timeout", type=int, default=None, help="Hard deadline in seconds")
        output.add_output_args(sp)

    p_watch = sub.add_parser(
        "watch",
        help="Poll telemetry, run_events, loop_rounds, EC2 state, and PRs until change or terminal state",
    )
    p_watch.add_argument("repo", metavar="<owner/repo>")
    p_watch.add_argument("issue", type=int, metavar="<issue>")
    p_watch.add_argument("--until", choices=["terminal", "change"], default="change")
    add_common(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    p_rerun = sub.add_parser(
        "rerun",
        help="Reset an issue (comments, flow* PRs, flow-wip/* branches, DB rows, labels); dry-run without --write",
    )
    p_rerun.add_argument("repo", metavar="<owner/repo>")
    p_rerun.add_argument("issue", type=int, metavar="<issue>")
    p_rerun.add_argument("--write", action="store_true")
    p_rerun.add_argument("--no-relabel", action="store_true")
    p_rerun.add_argument("--keep-pr", type=int, action="append", default=[], metavar="N")
    p_rerun.add_argument("--label", default="flow:feature", metavar="LABEL")
    add_common(p_rerun)
    p_rerun.set_defaults(func=cmd_rerun)

    p_ssm = sub.add_parser(
        "ssm",
        help="Run a bounded recipe (agent-log, cloud-init, git-rev, display) or `-- <cmd>` on active EC2 via SSM",
    )
    p_ssm.add_argument("repo", metavar="<owner/repo>")
    p_ssm.add_argument("issue", type=int, metavar="<issue>")
    p_ssm.add_argument("--role", choices=["planner", "developer", "evaluator", "arbitrator"], default=None)
    p_ssm.add_argument(
        "recipe_or_cmd",
        nargs="?",
        default=None,
        metavar="<recipe>",
        help=f"A recipe name ({sorted(SSM_RECIPES)}) or, after `--`, a raw shell command",
    )
    add_common(p_ssm)
    p_ssm.set_defaults(func=cmd_ssm)

    p_db = sub.add_parser("db", help="Supabase diagnostics, schema introspection, and configuration")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)

    p_db_tenant = db_sub.add_parser(
        "tenant",
        help="Tenant row, manifest_sha, active metric count, role->model config (incl. planner), and BYOK key status",
    )
    p_db_tenant.add_argument("repo", metavar="<owner/repo>")
    add_common(p_db_tenant)
    p_db_tenant.set_defaults(func=cmd_db_tenant)

    p_db_set = db_sub.add_parser("set-model", help="Configure a role's LLM provider, model, and reasoning effort")
    p_db_set.add_argument("repo", metavar="<owner/repo>")
    p_db_set.add_argument("role", choices=["planner", "developer", "evaluator", "arbitrator"], metavar="<role>")
    p_db_set.add_argument("provider", metavar="<provider>")
    p_db_set.add_argument("model", metavar="<model>")
    p_db_set.add_argument(
        "effort",
        nargs="?",
        default="none",
        choices=["none", "low", "medium", "high"],
        metavar="effort",
    )
    add_common(p_db_set)
    p_db_set.set_defaults(func=cmd_db_set_model)

    p_db_events = db_sub.add_parser(
        "events",
        help="Inspect run_events, loop_rounds, and arbitration_decisions for a run or issue",
    )
    p_db_events.add_argument("repo", nargs="?", default=None, metavar="<owner/repo>")
    p_db_events.add_argument("--issue", type=int, default=None, metavar="N")
    p_db_events.add_argument("--run", default=None, metavar="R")
    p_db_events.add_argument("--limit", type=int, default=30, metavar="N")
    add_common(p_db_events)
    p_db_events.set_defaults(func=cmd_db_events)

    p_db_schema = db_sub.add_parser(
        "schema",
        help="List public tables with row estimates, or columns (name, type, nullable) for one table",
    )
    p_db_schema.add_argument("table", nargs="?", default=None, metavar="table")
    add_common(p_db_schema)
    p_db_schema.set_defaults(func=cmd_db_schema)

    p_db_sql = db_sub.add_parser(
        "sql",
        help="Read-only SELECT/WITH query via Supabase pooler (with column/table suggestions on 42P01/42703)",
    )
    p_db_sql.add_argument("sql", metavar="<SELECT ...>")
    add_common(p_db_sql)
    p_db_sql.set_defaults(func=cmd_db_sql)

    p_db_prune = db_sub.add_parser(
        "prune",
        help="Delete loop_rounds and arbitration_decisions rows for one issue (dry-run without --write)",
    )
    p_db_prune.add_argument("repo", metavar="<owner/repo>")
    p_db_prune.add_argument("--issue", type=int, required=True, metavar="N")
    p_db_prune.add_argument("--write", action="store_true")
    add_common(p_db_prune)
    p_db_prune.set_defaults(func=cmd_db_prune)

    p_db_migrate = db_sub.add_parser(
        "migrate",
        help="Apply an idempotent SQL migration file via pooler (dry-run without --write)",
    )
    p_db_migrate.add_argument("file", metavar="<file>")
    p_db_migrate.add_argument("--write", action="store_true")
    add_common(p_db_migrate)
    p_db_migrate.set_defaults(func=cmd_db_migrate)

    p_cp = sub.add_parser("cp", help="Control Panel host status, route probing, provider key check")
    cp_sub = p_cp.add_subparsers(dest="cp_command", required=True)

    p_cp_status = cp_sub.add_parser(
        "status",
        help="Control Panel deployed SHA vs origin/main, refresh timer, journal, and docker status",
    )
    add_common(p_cp_status)
    p_cp_status.set_defaults(func=cmd_cp_status)

    p_cp_probe = cp_sub.add_parser(
        "probe",
        help="Probe Control Panel routes and verify screenshot routes require authentication",
    )
    p_cp_probe.add_argument("--repo", default="arsenyspb/ollama-monitor", metavar="owner/repo")
    add_common(p_cp_probe)
    p_cp_probe.set_defaults(func=cmd_cp_probe)

    p_cp_keycheck = cp_sub.add_parser(
        "keycheck",
        help="Validate an LLM provider key read from the KEY env var (never pass keys on the CLI)",
    )
    p_cp_keycheck.add_argument("provider", metavar="<provider>")
    add_common(p_cp_keycheck)
    p_cp_keycheck.set_defaults(func=cmd_cp_keycheck)

    p_ci = sub.add_parser(
        "ci",
        help="Recent workflow runs + failed job logs; `--minutes` summarizes billed Actions minutes and 60s-cliff risk",
    )
    p_ci.add_argument("repo", metavar="<owner/repo>")
    p_ci.add_argument("run_id", nargs="?", default=None, metavar="run_id")
    p_ci.add_argument("--limit", type=int, default=None, metavar="N")
    p_ci.add_argument("--workflow", default=None, metavar="W")
    p_ci.add_argument(
        "--minutes",
        action="store_true",
        help="Summarize per-job billed Actions minutes, p50/p90/max duration, and 60s-cliff risk",
    )
    add_common(p_ci)
    p_ci.set_defaults(func=cmd_ci)

    p_verify = sub.add_parser(
        "verify",
        help="CI-parity workspace verification (ruff, combined mypy, pytest --tb=short, schemas, gitleaks)",
    )
    p_verify.add_argument("--changed", action="store_true", help="Fast iteration: lint/typecheck/test changed paths")
    add_common(p_verify)
    p_verify.set_defaults(func=verify.cmd_verify)

    p_ship = sub.add_parser(
        "ship",
        help="Full verify, push, open/update PR (Fixes/Refs #N + audit table), wait on CI run only (optional --merge)",
        epilog=(
            "Preconditions & side effects: requires a clean tracked tree (unstaged `issues/J*.md` and "
            "`JUMBO_STATUS.md` are allowed and ignored); PR body must contain `Fixes #N` or `Refs #N` and the "
            "`## Adversarial Self-Review` subsections (`### 1. Per-Detail Corner-Case Audit`, `### 2. Assumption & "
            "Simplification Audit`); waits only on `ci.yml` (`conclusion == success`); `--merge` squash-merges, "
            "deletes the branch, and checks out + pulls `main`."
        ),
    )
    p_ship.add_argument("--issue", type=int, default=None, metavar="N")
    p_ship.add_argument("--title", default=None, metavar="TITLE")
    p_ship.add_argument("--body", default=None, metavar="TEXT")
    p_ship.add_argument("--body-file", default=None, metavar="PATH|-")
    p_ship.add_argument("--merge", action="store_true")
    add_common(p_ship)
    p_ship.set_defaults(func=cmd_ship)

    p_review = sub.add_parser(
        "review",
        help="Fetch PR reviews, inline threads, and review-round counter; `--wait` polls <=240s for new reviews",
        epilog=(
            "Behavior: `--wait` implies `--verbose`, polls every 15s up to 240s, matches reviews by "
            "`review.commit.oid == headRefOid` and comments by head check-suite `created_at`, and excludes the PR "
            "author and current `gh` user unless `--until-reviewer '*'` is passed. Emits a `ROUND k` STOP banner "
            "when `k >= 2`."
        ),
    )
    p_review.add_argument("pr", type=int, metavar="<pr>")
    p_review.add_argument("--repo", default="atobar-flow/atobar-flow", metavar="owner/repo")
    p_review.add_argument("--unresolved", action="store_true", help="Show only unresolved review threads")
    p_review.add_argument("--wait", action="store_true", help="Poll up to 240s for a review after the latest push")
    p_review.add_argument("--until-reviewer", default=None, metavar="SUBSTR", help="Filter wait by reviewer substring")
    add_common(p_review)
    p_review.set_defaults(func=cmd_review)

    p_jumbo = sub.add_parser("jumbo", help="Scaffold or sync a Jumbo issue")
    jumbo_sub = p_jumbo.add_subparsers(dest="jumbo_command", required=True)
    p_jumbo_new = jumbo_sub.add_parser(
        "new",
        help="Create `issues/J<NN>-<slug>.md`, open GitHub issue, update `JUMBO_STATUS.md` (`auto` number, `-` stdin)",
        epilog=(
            "Preconditions & side effects: strips any leading `J<NN>:` prefix from `--title`; in `auto` mode "
            "allocates `max(local, JUMBO_STATUS.md, all GitHub jumbo issues) + 1`, rewrites a leading `# J<NN>` "
            "heading in `--from-file`, and re-allocates after issue creation if a concurrent session claimed the "
            "same J-number with a lower GitHub issue number. Leaves `issues/J<NN>-<slug>.md` and `JUMBO_STATUS.md` "
            "modified in the working tree (non-blocking for `ship`)."
        ),
    )
    p_jumbo_new.add_argument("number", metavar="<NN|auto>")
    p_jumbo_new.add_argument("slug", metavar="<slug>")
    p_jumbo_new.add_argument("--title", required=True, metavar="TITLE")
    p_jumbo_new.add_argument("--context", default=None, metavar="TEXT")
    p_jumbo_new.add_argument("--from-file", default=None, metavar="PATH|-")
    add_common(p_jumbo_new)
    p_jumbo_new.set_defaults(func=cmd_jumbo_new)

    p_jumbo_sync = jumbo_sub.add_parser(
        "sync",
        help="Push `issues/J<NN>-*.md` to the matching GitHub issue body (`gh issue edit --body-file`)",
    )
    p_jumbo_sync.add_argument("number", metavar="<NN>")
    add_common(p_jumbo_sync)
    p_jumbo_sync.set_defaults(func=cmd_jumbo_sync)

    p_labels = sub.add_parser(
        "labels",
        help="Seed canonical Atobar Flow labels on a tenant repo (dry-run without --write)",
    )
    p_labels.add_argument("repo", metavar="<owner/repo>")
    p_labels.add_argument("--write", action="store_true", help="Apply labels via `gh label create --force`")
    add_common(p_labels)
    p_labels.set_defaults(func=cmd_labels)

    p_sessions = sub.add_parser("sessions", help="Audit local Pi and Claude Code session JSONL transcripts")
    sessions_sub = p_sessions.add_subparsers(dest="sessions_command", required=True)
    p_sessions_audit = sessions_sub.add_parser(
        "audit",
        help="Summarize turns, tokens, cost, cold-cache turns, repeat reads, sleeps >240s, and SQL/ship errors",
    )
    p_sessions_audit.add_argument("--harness", choices=["pi", "claude", "all"], default="all")
    p_sessions_audit.add_argument("--since", default=None, metavar="DATE")
    p_sessions_audit.add_argument("--dir", default=None, metavar="PATH")
    add_common(p_sessions_audit)
    p_sessions_audit.set_defaults(func=sessions.cmd_sessions_audit)

    p_init_app = sub.add_parser(
        "init-app",
        help="Verify GitHub App slug release, run GitHub App Manifest flow, and write credentials to .env",
    )
    p_init_app.add_argument("--org", default=None, metavar="ORG")
    p_init_app.add_argument("--slug", default=None, metavar="SLUG")
    p_init_app.add_argument("--cp-url", default=None, metavar="URL")
    p_init_app.add_argument("--env-file", default=None, metavar="PATH")
    p_init_app.add_argument("--force", action="store_true")
    p_init_app.add_argument("--dry-run", action="store_true")
    add_common(p_init_app)
    p_init_app.set_defaults(func=bootstrap.cmd_init_app)

    p_infra = sub.add_parser("infra", help="User-Zero HCP Terraform variable sync and 4-workspace orchestration")
    infra_sub = p_infra.add_subparsers(dest="infra_command", required=True)
    p_infra_sync = infra_sub.add_parser(
        "sync-vars",
        help="Sync .env variables to HCP Terraform varset/workspaces and GitHub Actions (dry-run without --write)",
    )
    p_infra_sync.add_argument("--write", action="store_true")
    p_infra_sync.add_argument("--env-file", default=None, metavar="PATH")
    add_common(p_infra_sync)
    p_infra_sync.set_defaults(func=bootstrap.cmd_infra_sync_vars)

    p_infra_up = infra_sub.add_parser(
        "up",
        help="Apply all 4 Terraform workspaces in dependency order and output server_ipv4 (dry-run without --write)",
    )
    p_infra_up.add_argument("--write", action="store_true")
    p_infra_up.add_argument("--env-file", default=None, metavar="PATH")
    add_common(p_infra_up)
    p_infra_up.set_defaults(func=bootstrap.cmd_infra_up)

    p_infra_destroy = infra_sub.add_parser(
        "destroy",
        help="Destroy all 4 Terraform workspaces in reverse dependency order and purge secrets (dry-run without --write)",
    )
    p_infra_destroy.add_argument("--write", action="store_true")
    p_infra_destroy.add_argument("--env-file", default=None, metavar="PATH")
    add_common(p_infra_destroy)
    p_infra_destroy.set_defaults(func=bootstrap.cmd_infra_destroy)

    p_help = sub.add_parser("help", help="Render the CLI Markdown command table generated from build_parser()")
    p_help.add_argument("--markdown", action="store_true")
    add_common(p_help)
    p_help.set_defaults(func=cmd_help)

    return p


def cmd_labels(args: argparse.Namespace) -> int:
    timeout = timebox.resolve_timeout(getattr(args, "timeout", None))
    lines: list[str] = [f"Atobar Flow label catalog for {args.repo} ({len(FLOW_LABEL_CATALOG)} labels):"]
    for entry in FLOW_LABEL_CATALOG:
        name = entry["name"]
        color = entry["color"]
        desc = entry["description"]
        if getattr(args, "write", False):
            gh.run(
                ["label", "create", name, "--color", color, "--description", desc, "--force", "-R", args.repo],
                timeout=min(20, timeout),
            )
            lines.append(f"  [seeded] {name} (#{color}) — {desc}")
        else:
            lines.append(f"  [dry-run] {name} (#{color}) — {desc}")
    if not getattr(args, "write", False):
        lines.append(f"Pass `--write` to create/update these labels on {args.repo}.")
    output.emit(
        args,
        lines,
        data={"repo": args.repo, "labels": list(FLOW_LABEL_CATALOG), "wrote": bool(getattr(args, "write", False))},
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(creds.redact(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
