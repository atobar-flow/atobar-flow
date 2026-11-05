from flow_engine.tenancy.onboard import changed_tenants


def test_changed_tenants_empty():
    assert changed_tenants([]) == []


def test_changed_tenants_ignores_non_tenant_files():
    files = [
        "README.md",
        "src/flow_engine/tenancy/manifest.py",
        "docs/ROADMAP.md",
        "tenants/README.txt",
        "other/tenants/a__b.json",
    ]
    assert changed_tenants(files) == []


def test_changed_tenants_extracts_and_deduplicates_repos():
    files = [
        "tenants/arsenyspb__ollama-monitor.json",
        "docs/ROADMAP.md",
        "tenants/alice__cool_repo.json",
        "tenants/arsenyspb__ollama-monitor.json",
    ]
    assert changed_tenants(files) == ["alice/cool_repo", "arsenyspb/ollama-monitor"]
