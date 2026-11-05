from typing import Any
from unittest.mock import MagicMock

import pytest

from flow_agent.roles.arbitrator.gate import Observation
from flow_agent.roles.arbitrator.run import (
    filter_trusted_observations,
    run_arbitrator,
)
from atobar_contracts.manifest import Metric, MetricsRuntime, TenantManifest

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"

BOT_LOGINS = {
    "developer": "atobar-flow-agent[bot]",
    "evaluator": "atobar-flow-agent[bot]",
    "arbitrator": "atobar-flow-agent[bot]",
    "conductor": "atobar-flow[bot]",
}


class FakeGitHubClient:
    def __init__(self):
        self.check_runs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, Any]):
        entry = {
            "repo": repo,
            "name": name,
            "head_sha": head_sha,
            "conclusion": conclusion,
            "output": output,
        }
        self.check_runs.append(entry)
        return entry

    def post_pr_comment(self, repo: str, pr_number: int, body: str):
        entry = {"repo": repo, "pr_number": pr_number, "body": body}
        self.comments.append(entry)
        return entry

    def merge_pr(self, repo: str, pr_number: int, commit_title: str, merge_method: str):
        entry = {
            "repo": repo,
            "pr_number": pr_number,
            "commit_title": commit_title,
            "merge_method": merge_method,
        }
        self.merges.append(entry)
        return {"merged": True, "sha": VALID_SHA}


def sample_manifest(mode: str = "recommend") -> TenantManifest:
    return TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        arbitration_mode=mode,
        metrics=[
            Metric(
                key="test_coverage",
                run="pytest --cov",
                comparator=">=",
                threshold=80.0,
                baseline_mode="absolute",
            )
        ],
        metrics_runtime=MetricsRuntime(setup=["pip install pytest"]),
    )


def sample_passing_observations() -> list[Observation]:
    return [
        Observation(
            key="test_coverage",
            value=85.0,
            commit_sha=VALID_SHA,
            pr=42,
            workflow_path=".github/workflows/flow-metrics.yml",
            run_id=1001,
        )
    ]


def sample_failing_observations() -> list[Observation]:
    return [
        Observation(
            key="test_coverage",
            value=70.0,
            commit_sha=VALID_SHA,
            pr=42,
            workflow_path=".github/workflows/flow-metrics.yml",
            run_id=1002,
        )
    ]


# --- AC-1: Each mode tested with fake GitHub ---

@pytest.mark.asyncio
async def test_ac1_mode_shadow_records_only_no_gh_effects():
    manifest = sample_manifest(mode="shadow")
    fake_gh = FakeGitHubClient()

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_passing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
    )

    assert outcome.mode == "shadow"
    assert outcome.decision.conclusion == "pass"
    assert outcome.action_taken == "recorded"
    # Shadow mode only reports on the issue: no envelope, no check run, no merge
    assert len(fake_gh.check_runs) == 0
    assert len(fake_gh.comments) == 1
    assert "flow-arbitration" not in str(fake_gh.comments[0])
    assert len(fake_gh.merges) == 0


@pytest.mark.asyncio
async def test_ac1_mode_recommend_posts_check_and_comment_no_merge():
    manifest = sample_manifest(mode="recommend")
    fake_gh = FakeGitHubClient()

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_passing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
    )

    assert outcome.mode == "recommend"
    assert outcome.decision.conclusion == "pass"
    assert outcome.action_taken == "posted_recommendation"

    # flow/arbiter check run created with success conclusion
    assert len(fake_gh.check_runs) == 1
    assert fake_gh.check_runs[0]["name"] == "flow/arbiter"
    assert fake_gh.check_runs[0]["conclusion"] == "success"

    # flow-arbitration comment posted
    assert len(fake_gh.comments) == 1
    comment_body = fake_gh.comments[0]["body"]
    assert "```flow-arbitration" in comment_body or "<!-- atobar:flow-arbitration" in comment_body

    # No merge performed in recommend mode
    assert len(fake_gh.merges) == 0


@pytest.mark.asyncio
async def test_ac1_mode_auto_merge_passes_and_merges():
    manifest = sample_manifest(mode="auto_merge")
    fake_gh = FakeGitHubClient()

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_passing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
    )

    assert outcome.mode == "auto_merge"
    assert outcome.decision.conclusion == "pass"
    assert outcome.action_taken == "merged"

    # Check run and comment created
    assert len(fake_gh.check_runs) == 1
    assert fake_gh.check_runs[0]["conclusion"] == "success"
    assert len(fake_gh.comments) == 1

    # PR merge was invoked!
    assert len(fake_gh.merges) == 1
    assert fake_gh.merges[0]["pr_number"] == 42
    assert fake_gh.merges[0]["merge_method"] == "squash"


@pytest.mark.asyncio
async def test_ac1_mode_auto_merge_fails_does_not_merge():
    manifest = sample_manifest(mode="auto_merge")
    fake_gh = FakeGitHubClient()

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_failing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
    )

    assert outcome.mode == "auto_merge"
    assert outcome.decision.conclusion == "fail"
    assert outcome.action_taken == "rejected"

    assert len(fake_gh.check_runs) == 1
    assert fake_gh.check_runs[0]["conclusion"] == "failure"
    assert len(fake_gh.comments) == 1

    # Merge was NOT called because decision was fail
    assert len(fake_gh.merges) == 0


# --- AC-2: Observations from a PR that changed metrics_workflow_path are excluded ---

def test_ac2_observations_from_pr_changing_workflow_path_excluded():
    manifest = sample_manifest()
    obs = sample_passing_observations()

    # Case 1: PR did not touch metrics workflow path -> trusted
    trusted = filter_trusted_observations(
        manifest=manifest,
        observations=obs,
        changed_paths=["src/calculator.py", "tests/test_calculator.py"],
    )
    assert len(trusted) == 1

    # Case 2: PR changed .github/workflows/flow-metrics.yml -> untrusted, excluded!
    untrusted = filter_trusted_observations(
        manifest=manifest,
        observations=obs,
        changed_paths=[".github/workflows/flow-metrics.yml", "src/calculator.py"],
    )
    assert len(untrusted) == 0


# --- AC-3: Check conclusion never depends on narration ---

@pytest.mark.asyncio
async def test_ac3_check_conclusion_never_depends_on_narration():
    manifest = sample_manifest(mode="recommend")
    fake_gh = FakeGitHubClient()

    # Even if LLM returns enthusiastic praise, failing decision stays failure!
    fake_llm = MagicMock()
    fake_llm.chat.return_value = MagicMock(text="Amazing code! 100/10 approved!")

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_failing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
        llm=fake_llm,
    )

    assert outcome.decision.conclusion == "fail"
    assert len(fake_gh.check_runs) == 1
    assert fake_gh.check_runs[0]["conclusion"] == "failure"
    assert outcome.summary is not None and "Amazing" not in outcome.summary


@pytest.mark.asyncio
async def test_ac3_narration_failure_does_not_affect_check_conclusion():
    manifest = sample_manifest(mode="recommend")
    fake_gh = FakeGitHubClient()

    # LLM throws an exception during narration
    failing_llm = MagicMock()
    failing_llm.chat.side_effect = RuntimeError("LLM quota exceeded")

    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=VALID_SHA,
        pr_observations=sample_passing_observations(),
        base_observations=[],
        changed_paths=["src/app.py"],
        gh=fake_gh,
        llm=failing_llm,
    )

    assert outcome.decision.conclusion == "pass"
    assert len(fake_gh.check_runs) == 1
    assert fake_gh.check_runs[0]["conclusion"] == "success"
