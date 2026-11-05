"""Tests for Arbitrator Target-Progress vs. Guardrail Gate (WP-2 / AC-2)."""

import pytest

from atobar_contracts.arbitration import Observation, arbitrate, run_arbitrator
from atobar_contracts.envelopes import Arbitration, parse, render
from atobar_contracts.goal import TenantGoal
from atobar_contracts.manifest import ArbitrationPolicy, Metric, TenantManifest

PR_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def _goal_manifest(with_goal: bool = True) -> TenantManifest:
    metrics = [
        Metric(
            key="coverage_pct",
            run="coverage",
            comparator=">=",
            threshold=85.0,
            baseline_mode="absolute",
            tolerance_pct=1.0,
            min_samples=1,
        ),
        Metric(
            key="latency_ms",
            run="bench",
            comparator="<=",
            threshold=100.0,
            baseline_mode="no_regression",
            tolerance_pct=5.0,
            min_samples=1,
        ),
        Metric(
            key="test_pass_rate",
            run="pytest",
            comparator=">=",
            threshold=1.0,
            baseline_mode="absolute",
            min_samples=1,
        ),
    ]
    goal = (
        TenantGoal(
            objective="Reach 85% coverage without regressing latency or tests.",
            targets=["coverage_pct"],
            guardrails=["latency_ms", "test_pass_rate"],
        )
        if with_goal
        else None
    )
    return TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        arbitration=ArbitrationPolicy(mode="recommend"),
        metrics=metrics,
        goal=goal,
    )


def test_target_below_threshold_moves_toward_threshold_passes_with_progress() -> None:
    manifest = _goal_manifest(with_goal=True)
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    pr_obs = [
        Observation(key="coverage_pct", value=74.5, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=121.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]

    decision = arbitrate(manifest, pr_observations=pr_obs, base_observations=base_obs, changed_paths=[])
    assert decision.conclusion == "pass"
    assert decision.progress is True
    cov_res = decision.per_metric["coverage_pct"]
    assert cov_res.kind == "target"
    assert cov_res.passed is True
    assert cov_res.progress is True
    assert cov_res.delta == pytest.approx(4.5)
    assert decision.per_metric["latency_ms"].kind == "guardrail"
    assert decision.per_metric["test_pass_rate"].kind == "guardrail"


def test_target_below_threshold_unchanged_within_tolerance_passes_without_progress() -> None:
    manifest = _goal_manifest(with_goal=True)
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    # 69.5 is within 1% tolerance of 70.0 (70 * 0.99 = 69.3)
    pr_obs = [
        Observation(key="coverage_pct", value=69.5, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]

    decision = arbitrate(manifest, pr_observations=pr_obs, base_observations=base_obs, changed_paths=[])
    assert decision.conclusion == "pass"
    assert decision.progress is False
    cov_res = decision.per_metric["coverage_pct"]
    assert cov_res.kind == "target"
    assert cov_res.passed is True
    assert cov_res.progress is False
    assert cov_res.delta == pytest.approx(-0.5)


def test_target_regresses_beyond_tolerance_fails() -> None:
    manifest = _goal_manifest(with_goal=True)
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    pr_obs = [
        Observation(key="coverage_pct", value=65.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]

    decision = arbitrate(manifest, pr_observations=pr_obs, base_observations=base_obs, changed_paths=[])
    assert decision.conclusion == "fail"
    assert decision.progress is False
    cov_res = decision.per_metric["coverage_pct"]
    assert cov_res.passed is False
    assert cov_res.progress is False


def test_guardrail_breached_while_target_improves_fails_overall() -> None:
    manifest = _goal_manifest(with_goal=True)
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    # Coverage jumps from 70% to 90% (progress!), but test_pass_rate drops to 0.95 (guardrail breach)
    pr_obs = [
        Observation(key="coverage_pct", value=90.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=0.95, commit_sha=PR_SHA, run_id=2),
    ]

    decision = arbitrate(manifest, pr_observations=pr_obs, base_observations=base_obs, changed_paths=[])
    assert decision.conclusion == "fail"
    assert decision.progress is False
    assert decision.per_metric["coverage_pct"].progress is True
    assert decision.per_metric["test_pass_rate"].passed is False


def test_target_lower_is_better_and_insufficient_baseline() -> None:
    manifest = TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        metrics=[
            Metric(
                key="bundle_kb",
                run="size",
                comparator="<=",
                threshold=100.0,
                tolerance_pct=2.0,
                min_samples=2,
            )
        ],
        goal=TenantGoal(objective="Shrink bundle below 100KB", targets=["bundle_kb"]),
    )
    # Insufficient baseline samples (1 < 2)
    d_insuff = arbitrate(
        manifest,
        pr_observations=[Observation(key="bundle_kb", value=140.0, commit_sha=PR_SHA)],
        base_observations=[Observation(key="bundle_kb", value=150.0, commit_sha=BASE_SHA, run_id=1)],
        changed_paths=[],
    )
    assert d_insuff.conclusion == "insufficient"
    assert d_insuff.progress is False

    # Sufficient baseline (mean = 150.0); PR drops to 140.0 -> progress!
    base_obs = [
        Observation(key="bundle_kb", value=150.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="bundle_kb", value=150.0, commit_sha=BASE_SHA, run_id=2),
    ]
    d_prog = arbitrate(
        manifest,
        pr_observations=[
            Observation(key="bundle_kb", value=160.0, commit_sha=PR_SHA, run_attempt=1),
            Observation(key="bundle_kb", value=140.0, commit_sha=PR_SHA, run_attempt=2),
        ],
        base_observations=base_obs,
        changed_paths=[],
    )
    assert d_prog.conclusion == "pass"
    assert d_prog.progress is True
    assert d_prog.per_metric["bundle_kb"].delta == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_manifest_without_goal_preserves_exact_behavior_and_envelope_roundtrip() -> None:
    manifest_no_goal = _goal_manifest(with_goal=False)
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    # Without goal, coverage_pct is evaluated with baseline_mode="absolute" >= 85.0 -> 74.5 FAILS
    pr_obs = [
        Observation(key="coverage_pct", value=74.5, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]
    decision = arbitrate(manifest_no_goal, pr_observations=pr_obs, base_observations=base_obs, changed_paths=[])
    assert decision.conclusion == "fail"
    assert decision.progress is False

    # With goal, run_arbitrator sets envelope.progress = True
    outcome = await run_arbitrator(
        manifest=_goal_manifest(with_goal=True),
        pr=12,
        head_sha=PR_SHA,
        pr_observations=pr_obs,
        base_observations=base_obs,
        changed_paths=[],
    )
    assert outcome.decision.conclusion == "pass"
    assert outcome.decision.progress is True
    assert outcome.envelope is not None
    assert outcome.envelope.progress is True
    rendered = render(outcome.envelope)
    parsed = parse(
        rendered,
        author_login="atobar-flow-agent[bot]",
        bots={"arbitrator": "atobar-flow-agent[bot]"},
    )
    assert isinstance(parsed, Arbitration)
    assert parsed.progress is True


def test_p0_empty_metrics_manifest_fails_closed_as_insufficient() -> None:
    empty_manifest = TenantManifest.model_construct(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        arbitration=ArbitrationPolicy(mode="auto_merge"),
        protected_paths=[".github/workflows/**"],
        metrics=[],
        goal=TenantGoal(objective="Test", targets=["coverage_pct"]),
    )
    dec = arbitrate(empty_manifest, pr_observations=[], base_observations=[], changed_paths=[])
    assert dec.conclusion == "insufficient"
    assert dec.progress is False


def test_p1_progress_requires_exceeding_tolerance_band_and_already_met_baseline_is_not_progress() -> None:
    manifest = TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        metrics=[
            Metric(
                key="coverage_pct",
                run="cov",
                comparator=">=",
                threshold=80.0,
                baseline_mode="absolute",
                tolerance_pct=5.0,
                min_samples=1,
            )
        ],
        goal=TenantGoal(objective="Reach 80% coverage", targets=["coverage_pct"]),
    )

    # 1. Noise within 5% tolerance band (baseline=60, tol_band=3.0, PR=60.0001) -> pass, progress=False
    d_noise = arbitrate(
        manifest,
        pr_observations=[Observation(key="coverage_pct", value=60.0001, commit_sha=PR_SHA)],
        base_observations=[Observation(key="coverage_pct", value=60.0, commit_sha=BASE_SHA, run_id=1)],
        changed_paths=[],
    )
    assert d_noise.conclusion == "pass"
    assert d_noise.progress is False

    # 2. Baseline already meets threshold (baseline=90 >= 80) and PR holds at 90 -> pass, progress=False
    d_already = arbitrate(
        manifest,
        pr_observations=[Observation(key="coverage_pct", value=90.0, commit_sha=PR_SHA)],
        base_observations=[Observation(key="coverage_pct", value=90.0, commit_sha=BASE_SHA, run_id=1)],
        changed_paths=[],
    )
    assert d_already.conclusion == "pass"
    assert d_already.progress is False


def test_p1_absolute_target_with_missing_baseline_passes_when_threshold_met() -> None:
    manifest = TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        metrics=[
            Metric(
                key="coverage_pct",
                run="cov",
                comparator=">=",
                threshold=80.0,
                baseline_mode="absolute",
                min_samples=1,
            )
        ],
        goal=TenantGoal(objective="Reach 80% coverage", targets=["coverage_pct"]),
    )
    dec = arbitrate(
        manifest,
        pr_observations=[Observation(key="coverage_pct", value=85.0, commit_sha=PR_SHA)],
        base_observations=[],
        changed_paths=[],
    )
    assert dec.conclusion == "pass"
    assert dec.progress is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["recommend", "shadow"])
async def test_arbitrator_pass_requests_pr_review_from_owner_github_login(mode: str) -> None:
    manifest = _goal_manifest(with_goal=True)
    manifest = manifest.model_copy(
        update={
            "repo": "owner/repo",
            "owner_github_login": "collaborator-jane",
            "arbitration": ArbitrationPolicy(mode=mode),
        }
    )
    base_obs = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    pr_obs = [
        Observation(key="coverage_pct", value=75.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]

    class _FakeArbGH:
        def __init__(self) -> None:
            self.review_requests: list[tuple[str, int, list[str]]] = []

        def find_pr_for_sha(self, repo: str, sha: str) -> int:
            return 88

        def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
            pass

        def create_check_run(self, **kwargs: object) -> None:
            pass

        def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
            self.review_requests.append((repo, pr_number, reviewers))

    gh = _FakeArbGH()
    outcome = await run_arbitrator(
        manifest=manifest,
        pr=42,
        head_sha=PR_SHA,
        pr_observations=pr_obs,
        base_observations=base_obs,
        changed_paths=[],
        gh=gh,
    )
    assert outcome.decision.conclusion == "pass"
    assert gh.review_requests == [("owner/repo", 88, ["collaborator-jane"])]




@pytest.mark.asyncio
@pytest.mark.parametrize("valid, mode", [(True, "shadow"), (True, "recommend"), (False, "recommend")])
async def test_arbitrator_review_request_checks_assignability_and_explains_shadow(valid: bool, mode: str) -> None:
    manifest = _goal_manifest(with_goal=True).model_copy(
        update={"repo": "acme-org/app", "owner_github_login": "acme-org", "arbitration": ArbitrationPolicy(mode=mode)}
    )
    base = [
        Observation(key="coverage_pct", value=70.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="latency_ms", value=120.0, commit_sha=BASE_SHA, run_id=1),
        Observation(key="test_pass_rate", value=1.0, commit_sha=BASE_SHA, run_id=1),
    ]
    pr_obs = [
        Observation(key="coverage_pct", value=75.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="latency_ms", value=120.0, commit_sha=PR_SHA, run_id=2),
        Observation(key="test_pass_rate", value=1.0, commit_sha=PR_SHA, run_id=2),
    ]

    class _GH:
        def __init__(self) -> None:
            self.review_requests: list[tuple[str, int, list[str]]] = []
            self.comments: list[str] = []

        def find_pr_for_sha(self, repo: str, sha: str) -> int:
            return 88

        def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
            pass

        def create_check_run(self, **kwargs: object) -> None:
            pass

        def is_valid_assignee(self, repo: str, login: str) -> bool:
            return valid

        def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
            self.review_requests.append((repo, pr_number, reviewers))

        def create_comment(self, repo: str, issue_number: int, body: str) -> None:
            self.comments.append(body)

    gh = _GH()
    await run_arbitrator(
        manifest=manifest, pr=42, head_sha=PR_SHA, pr_observations=pr_obs, base_observations=base,
        changed_paths=[], gh=gh,
    )
    if not valid:
        assert gh.review_requests == []
    else:
        assert gh.review_requests == [("acme-org/app", 88, ["acme-org"])]
    assert any("shadow mode" in c for c in gh.comments) == (valid and mode == "shadow")
