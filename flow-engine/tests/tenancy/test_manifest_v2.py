import pathlib

import pytest
from pydantic import ValidationError

from flow_engine.tenancy.manifest import TenantManifest, load_manifest
from flow_engine.tenancy.schema import generate_schemas


def test_ac1_existing_manifests_load_unchanged():
    manifest = load_manifest("arsenyspb/ollama-monitor")
    assert manifest.repo == "arsenyspb/ollama-monitor"
    assert manifest.required_checks == []
    assert manifest.protected_paths == [".github/workflows/**", ".atobar/**"]
    assert manifest.metrics_workflow_path == ".github/workflows/flow-metrics.yml"
    assert manifest.arbitration.mode == "shadow"
    assert manifest.arbitration.merge_method == "squash"
    assert manifest.arbitration.require_human_label is None
    assert manifest.llm.developer is None
    assert manifest.llm.evaluator is None
    assert manifest.llm.arbitrator is None


def test_ac2_unknown_provider_rejected():
    base_data = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "metrics": [
            {
                "key": "m1",
                "run": "echo 1",
                "comparator": ">=",
                "threshold": 1.0,
            }
        ],
        "llm": {
            "developer": {
                "provider": "unknown_provider",
                "model": "some-model",
            }
        },
    }
    with pytest.raises(ValidationError):
        TenantManifest.model_validate(base_data)


def test_ac2_unknown_field_rejected():
    base_data = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "metrics": [
            {
                "key": "m1",
                "run": "echo 1",
                "comparator": ">=",
                "threshold": 1.0,
            }
        ],
        "arbitration": {
            "mode": "shadow",
            "unknown_arbitration_field": True,
        },
    }
    with pytest.raises(ValidationError):
        TenantManifest.model_validate(base_data)


def test_ac3_schema_files_byte_identical():
    schemas_dir = pathlib.Path(__file__).resolve().parents[3] / "contracts" / "schemas"
    tenant_schema_path = schemas_dir / "tenant.schema.json"
    metrics_schema_path = schemas_dir / "flow-metrics.schema.json"

    assert tenant_schema_path.exists()
    assert metrics_schema_path.exists()

    generated = generate_schemas()
    assert tenant_schema_path.read_text() == generated["tenant"]
    assert metrics_schema_path.read_text() == generated["flow-metrics"]
