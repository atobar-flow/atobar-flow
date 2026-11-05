import ast
import pathlib

import pytest

from flow_agent.roles.arbitrator.gate import (
    Observation,
    RunInfo,
    arbitrate,
    verify_provenance,
)
from atobar_contracts.manifest import Metric, TenantManifest

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def make_manifest(metrics: list[Metric], protected_paths: list[str] | None = None) -> TenantManifest:
    return TenantManifest(
        repo="owner/repo",
        owner_github_login="owner",
        protected_paths=protected_paths if protected_paths is not None else [".github/workflows/**", ".atobar/**"],
        metrics_workflow_path=".github/workflows/flow-metrics.yml",
        metrics=metrics,
    )


# --- AC-1: Table tests for comparators and baseline modes ---

COMPARATOR_CASES = [
    # (comparator, threshold, pr_val, expected_pass)
    (">=", 1.0, 1.0, True),
    (">=", 1.0, 1.2, True),
    (">=", 1.0, 0.99, False),
    ("<=", 100.0, 50.0, True),
    ("<=", 100.0, 100.0, True),
    ("<=", 100.0, 105.0, False),
    (">", 0.0, 0.1, True),
    (">", 0.0, 0.0, False),
    ("<", 50.0, 49.9, True),
    ("<", 50.0, 50.0, False),
    ("==", 42.0, 42.0, True),
    ("==", 42.0, 42.5, False),
]


@pytest.mark.parametrize("comparator,threshold,pr_val,expected_pass", COMPARATOR_CASES)
def test_ac1_absolute_comparators(comparator, threshold, pr_val, expected_pass):
    metric = Metric(
        key="test_metric",
        comparator=comparator,
        threshold=threshold,
        baseline_mode="absolute",
        run="echo 1",
    )
    manifest = make_manifest([metric])
    obs = [Observation(key="test_metric", value=pr_val, commit_sha=VALID_SHA)]

    decision = arbitrate(manifest, pr_observations=obs, base_observations=[], changed_paths=[])
    expected_conclusion = "pass" if expected_pass else "fail"
    assert decision.conclusion == expected_conclusion
    assert decision.per_metric["test_metric"].passed == expected_pass


def test_ac1_no_regression_baseline_mode():
    metric = Metric(
        key="latency_ms",
        comparator="<=",
        threshold=100.0,
        baseline_mode="no_regression",
        tolerance_pct=5.0,
        min_samples=2,
        run="echo 1",
    )
    manifest = make_manifest([metric])

    # Insufficient samples in base observations
    base_obs_insufficient = [Observation(key="latency_ms", value=50.0, commit_sha=BASE_SHA)]
    pr_obs = [Observation(key="latency_ms", value=51.0, commit_sha=VALID_SHA)]
    dec_insufficient = arbitrate(
        manifest,
        pr_observations=pr_obs,
        base_observations=base_obs_insufficient,
        changed_paths=[],
    )
    assert dec_insufficient.conclusion == "insufficient"

    # Sufficient base samples: baseline = (50 + 50) / 2 = 50. Tolerance 5% -> max allowed = 52.5
    base_obs_sufficient = [
        Observation(key="latency_ms", value=50.0, commit_sha=BASE_SHA),
        Observation(key="latency_ms", value=50.0, commit_sha=BASE_SHA),
    ]

    # PR latency = 52.0 <= 52.5 -> pass
    dec_pass = arbitrate(
        manifest,
        pr_observations=[Observation(key="latency_ms", value=52.0, commit_sha=VALID_SHA)],
        base_observations=base_obs_sufficient,
        changed_paths=[],
    )
    assert dec_pass.conclusion == "pass"

    # PR latency = 55.0 > 52.5 -> fail (regression)
    dec_fail = arbitrate(
        manifest,
        pr_observations=[Observation(key="latency_ms", value=55.0, commit_sha=VALID_SHA)],
        base_observations=base_obs_sufficient,
        changed_paths=[],
    )
    assert dec_fail.conclusion == "fail"


# --- AC-2: Provenance checks ---

def test_ac2_provenance_failures():
    manifest = make_manifest([Metric(key="m", comparator=">=", threshold=1.0, run="echo 1")])

    valid_run = RunInfo(
        actor="developer-user",
        event="pull_request",
        workflow_path=".github/workflows/flow-metrics.yml",
        head_sha=VALID_SHA,
    )
    valid_obs = Observation(
        key="m",
        value=1.0,
        commit_sha=VALID_SHA,
        workflow_path=".github/workflows/flow-metrics.yml",
    )

    # Valid case -> empty reasons
    assert verify_provenance(valid_obs, valid_run, manifest, VALID_SHA) == []

    # 1. Commit mismatch
    obs_bad_sha = Observation(
        key="m",
        value=1.0,
        commit_sha="wrong_sha",
        workflow_path=".github/workflows/flow-metrics.yml",
    )
    reasons = verify_provenance(obs_bad_sha, valid_run, manifest, VALID_SHA)
    assert any("commit" in r.lower() for r in reasons)

    # 2. Workflow path mismatch
    obs_bad_wf = Observation(key="m", value=1.0, commit_sha=VALID_SHA, workflow_path=".github/workflows/other.yml")
    reasons = verify_provenance(obs_bad_wf, valid_run, manifest, VALID_SHA)
    assert any("workflow_path" in r.lower() for r in reasons)

    # 3. Flow bot actor
    run_bot = RunInfo(
        actor="atobar-flow-agent[bot]",
        event="pull_request",
        workflow_path=".github/workflows/flow-metrics.yml",
        head_sha=VALID_SHA,
    )
    reasons = verify_provenance(valid_obs, run_bot, manifest, VALID_SHA)
    assert any("bot" in r.lower() for r in reasons)

    # 4. Untrusted event (e.g. workflow_dispatch)
    run_bad_event = RunInfo(
        actor="developer-user",
        event="workflow_dispatch",
        workflow_path=".github/workflows/flow-metrics.yml",
        head_sha=VALID_SHA,
    )
    reasons = verify_provenance(valid_obs, run_bad_event, manifest, VALID_SHA)
    assert any("event" in r.lower() for r in reasons)


# --- AC-3: Protected path change blocks regardless of metrics ---

def test_ac3_protected_path_change_blocks_requires_human():
    metric = Metric(key="m", comparator=">=", threshold=1.0, run="echo 1")
    manifest = make_manifest([metric], protected_paths=[".github/workflows/**", ".atobar/**"])
    obs = [Observation(key="m", value=2.0, commit_sha=VALID_SHA)]

    # Harmless path change -> passes
    dec_clean = arbitrate(
        manifest,
        pr_observations=obs,
        base_observations=[],
        changed_paths=["src/app.py", "README.md"],
    )
    assert dec_clean.conclusion == "pass"

    # Modifying workflow -> blocked_requires_human
    dec_blocked_wf = arbitrate(
        manifest,
        pr_observations=obs,
        base_observations=[],
        changed_paths=[".github/workflows/flow-metrics.yml"],
    )
    assert dec_blocked_wf.conclusion == "blocked_requires_human"
    assert any(".github/workflows" in r for r in dec_blocked_wf.reasons)

    # Modifying .atobar directory -> blocked_requires_human
    dec_blocked_atobar = arbitrate(
        manifest,
        pr_observations=obs,
        base_observations=[],
        changed_paths=[".atobar/metrics/test.sh"],
    )
    assert dec_blocked_atobar.conclusion == "blocked_requires_human"


# --- AC-4: No LLM imports in gate.py ---

def test_ac4_no_llm_imports_in_gate():
    gate_path = pathlib.Path(__file__).resolve().parents[2] / "src" / "flow_agent" / "roles" / "arbitrator" / "gate.py"
    tree = ast.parse(gate_path.read_text(), filename=str(gate_path))

    forbidden = ["google.genai", "openai", "anthropic", "litellm", "requests", "urllib", "httpx"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for f in forbidden:
                    assert f not in alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for f in forbidden:
                    assert f not in node.module
