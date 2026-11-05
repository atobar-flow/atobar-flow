"""One-time migration helper to import tenant manifest metrics into Supabase (#301 WP-1)."""

import argparse
import sys
from typing import Any

from atobar_contracts.manifest import TenantManifest, canonical_sha, load_manifest
from flow_engine.store import get_store


def import_tenant_manifest(repo: str, store: Any = None) -> dict[str, Any]:
    st: Any = store or get_store()
    manifest: TenantManifest = load_manifest(repo)
    tenant = st.tenant_by_repo(repo)
    if tenant is None:
        tenant = st.create_tenant(repo, manifest.owner_github_login)
    for m in manifest.metrics:
        st.upsert_metric_definition(
            tenant.id,
            {
                "key": m.key,
                "run": m.run,
                "comparator": m.comparator,
                "threshold": m.threshold,
                "baseline_mode": m.baseline_mode,
                "min_samples": m.min_samples,
                "status": "active",
            },
        )
    manifest_dict = manifest.model_dump(mode="json")
    sha = canonical_sha(manifest_dict)
    st.update_tenant_manifest(tenant.id, manifest_dict, sha)
    return {"tenant_id": tenant.id, "repo": repo, "manifest_sha": sha, "metrics": [m.key for m in manifest.metrics]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import tenant manifest into Supabase")
    parser.add_argument("--repo", default="arsenyspb/ollama-monitor")
    args = parser.parse_args()
    res = import_tenant_manifest(args.repo)
    print(f"Imported {res['repo']}: manifest_sha={res['manifest_sha']} metrics={res['metrics']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
