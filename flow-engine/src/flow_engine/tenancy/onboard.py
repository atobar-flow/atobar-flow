"""Onboarding steps run by .github/workflows/onboard-tenant.yml. Every step fails loudly."""

import argparse
import os
import pathlib
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import httpx

from flow_engine.tenancy.manifest import TenantManifest, load_manifest
from flow_engine.tenancy.render import render_into


def _db() -> Any:
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tenant_id(db: Any, repo: str) -> str:
    rows = db.table("tenants").select("id").eq("repo", repo).limit(1).execute().data
    if not rows:
        raise SystemExit(f"tenant {repo} is not registered; run the register step first")
    return str(rows[0]["id"])


def register(db: Any, m: TenantManifest) -> str:
    row = {
        "repo": m.repo,
        "default_branch": m.default_branch,
        "manifest": m.model_dump(mode="json"),
        "manifest_sha": m.sha256(),
        "arbitration_mode": m.arbitration.mode,
        "updated_at": _now(),
    }
    db.table("tenants").upsert(row, on_conflict="repo").execute()
    tid = tenant_id(db, m.repo)
    defs = [
        {
            "tenant_id": tid,
            "key": x.key,
            "source": {"kind": "command", "run": x.run},
            "comparator": x.comparator,
            "threshold": x.threshold,
            "baseline_mode": x.baseline_mode,
            "min_samples": x.min_samples,
            "updated_at": _now(),
        }
        for x in m.metrics
    ]
    db.table("metric_definitions").upsert(defs, on_conflict="tenant_id,key").execute()
    keep = [x.key for x in m.metrics]
    db.table("metric_definitions").delete().eq("tenant_id", tid).not_.in_("key", keep).execute()
    return tid


def issue_token(m: TenantManifest, cp_url: str, admin_token: str, out: pathlib.Path, rotate: bool) -> None:
    base = f"{cp_url.rstrip('/')}/v1/admin/tenants/{m.repo}/tokens"
    headers = {"Authorization": f"Bearer {admin_token}"}
    with httpx.Client(timeout=20) as http:
        if rotate:
            http.delete(base, headers=headers).raise_for_status()
        r = http.post(base, headers=headers, json={"owner_github_login": m.owner_github_login, "label": "ci"})
        r.raise_for_status()
    out.touch(mode=0o600)
    out.write_text(r.json()["token"])


def activate(db: Any, m: TenantManifest) -> None:
    tid = tenant_id(db, m.repo)
    missing = []
    for x in m.metrics:
        res = (
            db.table("metric_observations")
            .select("id", count="exact")
            .eq("tenant_id", tid)
            .eq("key", x.key)
            .execute()
        )
        if (res.count or 0) < x.min_samples:
            missing.append(f"{x.key} ({res.count or 0}/{x.min_samples})")
    if missing:
        raise SystemExit("not activating; missing metric observations: " + ", ".join(missing))
    db.table("tenants").update({"status": "active", "updated_at": _now()}).eq("id", tid).execute()


def changed_tenants(changed_files: Iterable[str]) -> list[str]:
    """Given a list of changed file paths, return sorted unique tenant repos whose manifests were changed."""
    tenants = set()
    for path in changed_files:
        p = pathlib.Path(path)
        if len(p.parts) == 2 and p.parts[0] == "tenants" and p.suffix == ".json":
            stem = p.stem
            if "__" in stem:
                owner, name = stem.split("__", 1)
                tenants.add(f"{owner}/{name}")
    return sorted(tenants)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m flow_engine.tenancy.onboard")
    ap.add_argument(
        "step",
        choices=["validate", "register", "issue-token", "render", "activate", "changed-tenants"],
    )
    ap.add_argument("--repo")
    ap.add_argument("--repo-dir", help="Client Repo checkout (render)")
    ap.add_argument("--cp-url", default=os.environ.get("CP_URL", ""))
    ap.add_argument("--out", help="issue-token: file to write the token to (0600)")
    ap.add_argument("--rotate", action="store_true")
    a = ap.parse_args(argv)

    if a.step == "changed-tenants":
        files = sys.stdin.read().splitlines()
        for t in changed_tenants(files):
            print(t)
        return

    if not a.repo:
        ap.error(f"--repo is required for step {a.step}")

    m = load_manifest(a.repo)
    if a.step == "validate":
        print(f"ok {m.repo} sha256={m.sha256()} metrics={[x.key for x in m.metrics]}")
    elif a.step == "register":
        print(f"registered {m.repo} tenant_id={register(_db(), m)}")
    elif a.step == "issue-token":
        issue_token(m, a.cp_url, os.environ["CP_ADMIN_TOKEN"], pathlib.Path(a.out), a.rotate)
        print(f"token issued for {m.repo} (written to {a.out}, not printed)")
    elif a.step == "render":
        for p in render_into(m, a.cp_url, pathlib.Path(a.repo_dir)):
            print(f"rendered {p}")
    elif a.step == "activate":
        activate(_db(), m)
        print(f"{m.repo} is active")


if __name__ == "__main__":
    main(sys.argv[1:])
