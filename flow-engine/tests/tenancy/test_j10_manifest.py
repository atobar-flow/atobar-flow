from flow_engine.tenancy.manifest import TenantManifest


def test_j10_ac2_canonical_arbitration_mode():
    data_legacy = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "arbitration_mode": "shadow",
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}]
    }
    m1 = TenantManifest(**data_legacy)
    assert m1.arbitration.mode == "shadow"

    data_canonical = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "arbitration": {"mode": "auto_merge"},
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}]
    }
    m2 = TenantManifest(**data_canonical)
    assert m2.arbitration.mode == "auto_merge"
    assert m2.arbitration_mode is None

