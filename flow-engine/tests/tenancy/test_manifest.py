import json

import pytest
from pydantic import ValidationError

from flow_engine.tenancy.manifest import TENANTS_DIR, TenantManifest, load_manifest
from flow_engine.tenancy.render import render_into


def _base(**kw):
    d = {
        "repo": "o/r",
        "owner_github_login": "o",
        "metrics": [{"key": "m", "run": "echo 1", "comparator": ">=", "threshold": 1}],
    }
    d.update(kw)
    return d


def test_pilot_manifest_is_valid_and_matches_confirmed_caps():
    m = load_manifest("arsenyspb/ollama-monitor")
    assert m.arbitration_mode is None
    assert (m.loop.max_review_rounds, m.loop.max_repeated_finding) == (4, 2)
    assert (m.loop.max_pr_wall_clock_minutes, m.loop.max_pr_cost_usd) == (30, 25)
    assert m.loop.agent_runtime == "nanobot"


def test_manifest_agent_runtime_parsing():
    m_default = TenantManifest.model_validate(_base())
    assert m_default.loop.agent_runtime == "nanobot"

    m_nanobot = TenantManifest.model_validate(_base(loop={"agent_runtime": "nanobot"}))
    assert m_nanobot.loop.agent_runtime == "nanobot"

    with pytest.raises(ValidationError):
        TenantManifest.model_validate(_base(loop={"agent_runtime": "invalid_runtime"}))


def test_every_tenant_manifest_validates():
    for p in TENANTS_DIR.glob("*.json"):
        TenantManifest.model_validate_json(p.read_text())


def test_rejects_unknown_fields_and_duplicate_keys():
    with pytest.raises(ValidationError):
        TenantManifest.model_validate(_base(surprise=1))
    dup = [{"key": "m", "run": "x", "comparator": ">=", "threshold": 1}] * 2
    with pytest.raises(ValidationError):
        TenantManifest.model_validate(_base(metrics=dup))
    with pytest.raises(ValidationError):
        TenantManifest.model_validate(_base(repo="no-slash"))


def test_sha_is_stable():
    a = TenantManifest.model_validate(_base())
    b = TenantManifest.model_validate(json.loads(json.dumps(_base())))
    assert a.sha256() == b.sha256()


def test_load_rejects_mismatched_repo(tmp_path):
    (tmp_path / "o__r.json").write_text(json.dumps(_base(repo="o/other")))
    with pytest.raises(ValueError):
        load_manifest("o/r", tmp_path)


def test_render_pilot(tmp_path):
    m = load_manifest("arsenyspb/ollama-monitor")
    paths = render_into(m, "https://cp.example/", tmp_path)
    assert ".github/workflows/flow-trigger.yml" in paths
    trig = (tmp_path / ".github/workflows/flow-trigger.yml").read_text()
    assert "https://cp.example/v1/events" in trig and "FLOW_DISPATCH_TOKEN" not in trig
    assert "${{ github.event.label.name }}" not in trig.split("run:")[1]  # no expression injection in shell
    met = (tmp_path / ".github/workflows/flow-metrics.yml").read_text()
    assert "steps.e2e_install_time_seconds.outputs.value" in met and "https://cp.example/v1/metrics" in met
