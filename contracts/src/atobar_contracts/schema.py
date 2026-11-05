"""JSON schema generator and validator for tenant manifest and flow-metrics contracts."""

import argparse
import json
import pathlib
import sys

from atobar_contracts.manifest import FlowMetricsReport, TenantManifest

SCHEMAS_DIR = pathlib.Path(__file__).resolve().parents[2] / "schemas"


def generate_schemas() -> dict[str, str]:
    """Generates formatted JSON schemas for TenantManifest and FlowMetricsReport."""
    tenant_schema = TenantManifest.model_json_schema()
    metrics_schema = FlowMetricsReport.model_json_schema()

    return {
        "tenant": json.dumps(tenant_schema, indent=2) + "\n",
        "flow-metrics": json.dumps(metrics_schema, indent=2) + "\n",
    }


def write_schemas(schemas_dir: pathlib.Path = SCHEMAS_DIR) -> None:
    """Writes JSON schemas to disk."""
    schemas_dir.mkdir(parents=True, exist_ok=True)
    generated = generate_schemas()
    (schemas_dir / "tenant.schema.json").write_text(generated["tenant"])
    (schemas_dir / "flow-metrics.schema.json").write_text(generated["flow-metrics"])


def check_schemas(schemas_dir: pathlib.Path = SCHEMAS_DIR) -> bool:
    """Returns True if on-disk schemas match generator output byte-for-byte."""
    tenant_path = schemas_dir / "tenant.schema.json"
    metrics_path = schemas_dir / "flow-metrics.schema.json"

    if not tenant_path.exists() or not metrics_path.exists():
        return False

    generated = generate_schemas()
    return (
        tenant_path.read_text() == generated["tenant"]
        and metrics_path.read_text() == generated["flow-metrics"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or verify tenancy JSON schemas.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify on-disk schemas match generated output without modifying them.",
    )
    args = parser.parse_args()

    if args.check:
        if not check_schemas():
            print(
                "ERROR: On-disk JSON schemas are out of sync with models. "
                "Run 'python -m flow_engine.tenancy.schema'.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("OK: JSON schemas match generator output.")
    else:
        write_schemas()
        print(f"Wrote JSON schemas to {SCHEMAS_DIR}")


if __name__ == "__main__":
    main()
