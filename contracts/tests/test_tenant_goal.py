"""Tests for TenantGoal schema, validation, and TenantManifest integration (WP-1 / AC-1)."""

import pytest
from pydantic import ValidationError

from atobar_contracts.goal import GoalBudget, GoalStallPolicy, TenantGoal
from atobar_contracts.manifest import Metric, TenantManifest
from atobar_contracts.schema import check_schemas


def _base_metrics() -> list[dict[str, object]]:
    return [
        {
            "key": "test_pass_rate",
            "run": "pytest -q",
            "comparator": ">=",
            "threshold": 1.0,
        },
        {
            "key": "coverage_pct",
            "run": "coverage report",
            "comparator": ">=",
            "threshold": 85.0,
        },
        {
            "key": "p95_latency_ms",
            "run": "bench",
            "comparator": "<=",
            "threshold": 120.0,
        },
    ]


def test_tenant_goal_defaults_and_bounds() -> None:
    goal = TenantGoal(
        objective="Raise test coverage above 85% without regressing latency.",
        targets=["coverage_pct"],
    )
    assert goal.autonomy == "approve"
    assert goal.guardrails == []
    assert goal.tracking_issue_number is None
    assert goal.budget == GoalBudget(max_goal_cost_usd=100.0, max_goal_prs=10, max_goal_days=14)
    assert goal.stall == GoalStallPolicy(max_merges_without_progress=3, max_open_planned_issues=2)
    assert len(goal.sha256()) == 64

    with pytest.raises(ValidationError):
        TenantGoal(objective="", targets=["coverage_pct"])

    with pytest.raises(ValidationError):
        TenantGoal(objective="Valid objective", targets=[])

    with pytest.raises(ValidationError):
        GoalBudget(max_goal_cost_usd=0.0)

    with pytest.raises(ValidationError):
        GoalStallPolicy(max_open_planned_issues=6)

    with pytest.raises(ValidationError, match="unique"):
        TenantGoal(objective="Duplicate target keys", targets=["coverage_pct", "coverage_pct"])


def test_tenant_manifest_with_goal_round_trips() -> None:
    manifest = TenantManifest(
        repo="arsenyspb/ollama-monitor",
        owner_github_login="arsenyspb",
        metrics=[Metric.model_validate(m) for m in _base_metrics()],
        goal=TenantGoal(
            objective="Reach 85% coverage while keeping tests green and latency low.",
            targets=["coverage_pct"],
            guardrails=["test_pass_rate", "p95_latency_ms"],
            autonomy="auto",
            tracking_issue_number=42,
            budget=GoalBudget(max_goal_cost_usd=250.0, max_goal_prs=15, max_goal_days=21),
            stall=GoalStallPolicy(max_merges_without_progress=4, max_open_planned_issues=3),
        ),
    )
    dumped = manifest.model_dump(mode="json")
    restored = TenantManifest.model_validate(dumped)
    assert restored == manifest
    assert restored.goal is not None
    assert restored.goal.objective.startswith("Reach 85%")
    assert restored.goal.targets == ["coverage_pct"]
    assert restored.goal.guardrails == ["test_pass_rate", "p95_latency_ms"]
    assert restored.goal.autonomy == "auto"
    assert restored.goal.tracking_issue_number == 42
    assert restored.sha256() == manifest.sha256()


def test_tenant_manifest_rejects_unknown_target_or_guardrail_metric() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        TenantManifest(
            repo="arsenyspb/ollama-monitor",
            owner_github_login="arsenyspb",
            metrics=[Metric.model_validate(m) for m in _base_metrics()],
            goal=TenantGoal(
                objective="Improve non-existent metric",
                targets=["missing_metric"],
            ),
        )

    with pytest.raises(ValidationError, match="unknown"):
        TenantManifest(
            repo="arsenyspb/ollama-monitor",
            owner_github_login="arsenyspb",
            metrics=[Metric.model_validate(m) for m in _base_metrics()],
            goal=TenantGoal(
                objective="Guard non-existent metric",
                targets=["coverage_pct"],
                guardrails=["unknown_guardrail"],
            ),
        )


def test_tenant_manifest_rejects_overlapping_targets_and_guardrails() -> None:
    with pytest.raises(ValidationError, match="both"):
        TenantManifest(
            repo="arsenyspb/ollama-monitor",
            owner_github_login="arsenyspb",
            metrics=[Metric.model_validate(m) for m in _base_metrics()],
            goal=TenantGoal(
                objective="Overlap metric in targets and guardrails",
                targets=["coverage_pct"],
                guardrails=["test_pass_rate", "coverage_pct"],
            ),
        )


def test_json_schemas_in_sync() -> None:
    assert check_schemas() is True
