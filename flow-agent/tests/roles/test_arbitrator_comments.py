from unittest.mock import MagicMock

import pytest

from flow_agent.roles.arbitrator.gate import Observation
from flow_agent.roles.arbitrator.run import run_arbitrator
from atobar_contracts.manifest import TenantManifest

WF = ".github/workflows/flow-metrics.yml"
HEAD = "a" * 40


def _manifest(mode: str) -> TenantManifest:
    return TenantManifest(
        repo="owner/repo",
        owner_github_login="owner",
        metrics=[{"key": "dependency_count", "comparator": "<=", "threshold": 0.0, "baseline_mode": "no_regression"}],
        arbitration={"mode": mode},
    )


async def _run(mode: str, pr_value: float, base_values: list[float], gh: MagicMock):
    return await run_arbitrator(
        manifest=_manifest(mode),
        pr=33,
        head_sha=HEAD,
        pr_observations=[Observation(key="dependency_count", value=pr_value, workflow_path=WF, commit_sha=HEAD)],
        base_observations=[
            Observation(key="dependency_count", value=v, workflow_path=WF, commit_sha="b" * 40) for v in base_values
        ],
        changed_paths=["src/app.py"],
        gh=gh,
        impl_pr=44,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_value,conclusion", [(9.0, "pass"), (8.0, "pass"), (10.0, "fail")])
async def test_dependency_count_compares_with_main(pr_value, conclusion):
    gh = MagicMock()
    outcome = await _run("shadow", pr_value, [9.0], gh)
    assert outcome.decision.conclusion == conclusion
    targets = [c.kwargs["pr_number"] for c in gh.post_pr_comment.call_args_list]
    assert targets == ([33] if conclusion == "pass" else [33, 44])
    gh.create_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_violation_comment_names_metric_values_and_sha():
    gh = MagicMock()
    await _run("shadow", 10.0, [9.0], gh)
    pr_body = gh.post_pr_comment.call_args_list[1].kwargs["body"]
    assert "`dependency_count` | 10 | 9 | <= main | ❌ violated" in pr_body
    assert "PR #44 @ `aaaaaaa`" in pr_body
    issue_body = gh.post_pr_comment.call_args_list[0].kwargs["body"]
    assert "```flow-arbitration" not in issue_body


@pytest.mark.asyncio
async def test_missing_main_baseline_is_insufficient_not_pass():
    gh = MagicMock()
    outcome = await _run("shadow", 9.0, [], gh)
    assert outcome.decision.conclusion == "insufficient"


@pytest.mark.asyncio
async def test_recommend_issue_comment_carries_envelope():
    gh = MagicMock()
    await _run("recommend", 9.0, [9.0], gh)
    targets = [c.kwargs["pr_number"] for c in gh.post_pr_comment.call_args_list]
    assert targets == [33, 44]
    for call in gh.post_pr_comment.call_args_list:
        body = call.kwargs["body"]
        assert "```flow-arbitration" in body
        assert "https://cp.example.invalid/tenants/owner/repo#metric-dependency_count" in body
        assert "_Baseline (`main`): `bbbbbbb`_" in body
    gh.create_check_run.assert_called_once()
