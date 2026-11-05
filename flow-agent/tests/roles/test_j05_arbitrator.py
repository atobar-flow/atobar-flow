from unittest.mock import MagicMock

import pytest

from flow_agent.roles.arbitrator.gate import Observation
from flow_agent.roles.arbitrator.run import run_arbitrator
from atobar_contracts.manifest import TenantManifest


@pytest.mark.asyncio
async def test_j05_ac1_arbitrator_uses_observations_and_changed_paths():
    manifest_dict = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}],
        "arbitration": {"mode": "shadow"}
    }
    manifest = TenantManifest(**manifest_dict)

    pr_obs = [Observation(key="m1", value=0.5, workflow_path=".github/workflows/flow-metrics.yml", commit_sha="a" * 40)]

    gh = MagicMock()

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=1,
        head_sha="a" * 40,
        pr_observations=pr_obs,
        base_observations=[],
        changed_paths=["app.py"],
        gh=gh
    )

    assert outcome.mode == "shadow"
    assert outcome.action_taken == "recorded"
    assert outcome.decision.conclusion == "fail" # 0.5 < 1.0
    gh.post_pr_comment.assert_called_once()
    gh.create_check_run.assert_not_called()

@pytest.mark.asyncio
async def test_j05_ac2_workflow_edit_blocks():
    manifest_dict = {
        "repo": "owner/repo",
        "owner_github_login": "owner",
        "metrics": [{"key": "m1", "comparator": ">=", "threshold": 1.0}],
        "arbitration": {"mode": "shadow"}
    }
    manifest = TenantManifest(**manifest_dict)

    pr_obs = [Observation(key="m1", value=2.0, workflow_path=".github/workflows/flow-metrics.yml", commit_sha="a" * 40)]

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=1,
        head_sha="a" * 40,
        pr_observations=pr_obs,
        base_observations=[],
        changed_paths=[".github/workflows/flow-metrics.yml"],
        gh=MagicMock()
    )

    # Value 2.0 passes, but because the workflow path changed, it should block
    assert outcome.decision.conclusion == "blocked_requires_human"

