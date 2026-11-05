"""Compatibility re-export of TenantManifest and FlowMetricsReport from atobar_contracts."""

import sys

from atobar_contracts.manifest import (
    REPO_RE,
    SHA_RE,
    TENANTS_DIR,
    ArbitrationPolicy,
    FlowMetricItem,
    FlowMetricsReport,
    LoopLimits,
    Metric,
    MetricSource,
    MetricsRuntime,
    RoleLLMConfig,
    TenantLLMConfig,
    TenantManifest,
    canonical_sha,
    load_manifest,
    slug_for,
    validate_all,
)

__all__ = [
    "ArbitrationPolicy",
    "FlowMetricItem",
    "FlowMetricsReport",
    "LoopLimits",
    "Metric",
    "MetricSource",
    "MetricsRuntime",
    "REPO_RE",
    "RoleLLMConfig",
    "SHA_RE",
    "TENANTS_DIR",
    "TenantLLMConfig",
    "TenantManifest",
    "canonical_sha",
    "load_manifest",
    "slug_for",
    "validate_all",
]

if __name__ == "__main__":
    try:
        repos = validate_all()
        print(f"Validated {len(repos)} tenant manifests: {', '.join(repos)}")
    except Exception as e:
        print(f"Error validating tenant manifests: {e}", file=sys.stderr)
        sys.exit(1)
