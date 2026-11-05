"""Deterministic metric gate, provenance verification, and action dispatch for ARBITRATOR."""

import fnmatch
import logging
import os
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from atobar_contracts.envelopes import Arbitration, render_hidden
from atobar_contracts.manifest import TenantManifest

logger = logging.getLogger(__name__)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: float
    commit_sha: str
    pr: Optional[int] = None
    check_run_id: Optional[int] = None
    workflow_path: Optional[str] = None
    run_attempt: Optional[int] = None
    artifact_sha256: Optional[str] = None
    run_id: Optional[int] = None
    observed_at: Optional[str] = None


class RunInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str
    event: str
    workflow_path: str
    head_sha: str


class MetricResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    kind: Literal["guardrail", "target"] = "guardrail"
    passed: bool
    observed_value: Optional[float] = None
    baseline_value: Optional[float] = None
    delta: Optional[float] = None
    progress: bool = False
    threshold: Optional[float] = None
    comparator: str
    reason: Optional[str] = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conclusion: Literal["pass", "fail", "insufficient", "blocked_requires_human"]
    progress: bool = False
    reasons: list[str] = Field(default_factory=list)
    per_metric: dict[str, MetricResult] = Field(default_factory=dict)


class ArbitrationOutcome(BaseModel):
    """Outcome of an arbitrator execution round."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["shadow", "recommend", "auto_merge"]
    decision: Decision
    action_taken: str
    summary: Optional[str] = None
    envelope: Optional[Arbitration] = None
    comment_id: Optional[int] = None


def verify_provenance(
    obs: Observation,
    run_info: RunInfo,
    manifest: TenantManifest,
    head_sha: str,
) -> list[str]:
    """Verifies observation provenance against CI run metadata and manifest."""
    reasons: list[str] = []

    if obs.commit_sha != head_sha:
        reasons.append(f"Observation commit_sha '{obs.commit_sha}' does not match expected head_sha '{head_sha}'")
    if run_info.head_sha != head_sha:
        reasons.append(f"Run head_sha '{run_info.head_sha}' does not match expected head_sha '{head_sha}'")

    if obs.workflow_path and obs.workflow_path != manifest.metrics_workflow_path:
        reasons.append(
            f"Observation workflow_path '{obs.workflow_path}' does not match "
            f"manifest metrics_workflow_path '{manifest.metrics_workflow_path}'"
        )
    if run_info.workflow_path != manifest.metrics_workflow_path:
        reasons.append(
            f"Run workflow_path '{run_info.workflow_path}' does not match "
            f"manifest metrics_workflow_path '{manifest.metrics_workflow_path}'"
        )

    actor_lower = run_info.actor.lower()
    if actor_lower.startswith("flow-") or "bot" in actor_lower:
        reasons.append(f"Run actor '{run_info.actor}' is a flow bot; self-certification is prohibited")

    if run_info.event not in ("pull_request", "push"):
        reasons.append(f"Run event '{run_info.event}' is not in allowed events ('pull_request', 'push')")

    return reasons


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return fnmatch.fnmatch(path, pattern)


def _eval_comparator(
    val: float,
    target: float,
    comparator: str,
    tolerance_pct: float = 0.0,
) -> bool:
    tol = tolerance_pct / 100.0
    if comparator == ">=":
        return val >= target * (1.0 - tol)
    if comparator == "<=":
        return val <= target * (1.0 + tol)
    if comparator == ">":
        return val > target * (1.0 - tol)
    if comparator == "<":
        return val < target * (1.0 + tol)
    if comparator == "==":
        margin = abs(target * tol) if target != 0 else 1e-9
        return abs(val - target) <= margin
    return False


def arbitrate(
    manifest: TenantManifest,
    pr_observations: list[Observation],
    base_observations: list[Observation],
    changed_paths: list[str],
) -> Decision:
    """Pure function evaluating metric gate per tenant policy and observations."""
    reasons: list[str] = []

    for changed in changed_paths:
        for protected in manifest.protected_paths:
            if _path_matches_pattern(changed, protected):
                return Decision(
                    conclusion="blocked_requires_human",
                    reasons=[f"Changed protected path '{changed}' matching pattern '{protected}'"],
                )

    invalid_reason = getattr(manifest, "_invalid_manifest_reason", None)
    if invalid_reason:
        return Decision(
            conclusion="insufficient",
            progress=False,
            reasons=[str(invalid_reason)],
        )
    if manifest.goal is not None and not manifest.metrics:
        return Decision(
            conclusion="insufficient",
            progress=False,
            reasons=["Tenant manifest has a goal but no active metrics configured"],
        )

    pr_by_key: dict[str, list[Observation]] = {}
    for o in pr_observations:
        pr_by_key.setdefault(o.key, []).append(o)

    base_by_key: dict[str, list[Observation]] = {}
    for o in base_observations:
        base_by_key.setdefault(o.key, []).append(o)

    per_metric: dict[str, MetricResult] = {}
    has_insufficient = False
    has_fail = False
    target_keys = set(manifest.goal.targets) if manifest.goal is not None else set()

    def _dedup_base_samples(metric_key: str) -> list[float]:
        seen_base: set[tuple[Any, ...]] = set()
        samples: list[float] = []
        for idx, b in enumerate(base_by_key.get(metric_key, [])):
            run_ident = b.run_id if b.run_id is not None else b.check_run_id
            sig: tuple[Any, ...] = (
                (b.commit_sha, run_ident, int(b.run_attempt or 1)) if run_ident is not None else ("idx", idx)
            )
            if sig in seen_base:
                continue
            seen_base.add(sig)
            samples.append(b.value)
        return samples

    for m in manifest.metrics:
        kind: Literal["guardrail", "target"] = "target" if m.key in target_keys else "guardrail"
        obs_list = pr_by_key.get(m.key, [])
        if not obs_list:
            per_metric[m.key] = MetricResult(
                key=m.key,
                kind=kind,
                passed=False,
                comparator=m.comparator,
                threshold=m.threshold,
                reason=f"Missing observation for metric '{m.key}'",
            )
            has_insufficient = True
            reasons.append(f"Missing observation for metric '{m.key}'")
            continue

        latest_obs = max(
            enumerate(obs_list),
            key=lambda iv: (
                iv[1].run_attempt if iv[1].run_attempt is not None else 1,
                iv[1].observed_at or "",
                iv[1].run_id or 0,
                iv[0],
            ),
        )[1]

        if kind == "target":
            base_samples = _dedup_base_samples(m.key)
            target_met = _eval_comparator(latest_obs.value, m.threshold, m.comparator)
            if len(base_samples) < m.min_samples:
                if m.baseline_mode == "absolute" and target_met:
                    per_metric[m.key] = MetricResult(
                        key=m.key,
                        kind="target",
                        passed=True,
                        progress=False,
                        observed_value=latest_obs.value,
                        threshold=m.threshold,
                        comparator=m.comparator,
                        reason=None,
                    )
                else:
                    per_metric[m.key] = MetricResult(
                        key=m.key,
                        kind="target",
                        passed=False,
                        progress=False,
                        observed_value=latest_obs.value,
                        threshold=m.threshold,
                        comparator=m.comparator,
                        reason=(f"Insufficient baseline samples for '{m.key}': got {len(base_samples)} < {m.min_samples}"),
                    )
                    has_insufficient = True
                    reasons.append(f"Insufficient baseline samples for '{m.key}'")
            else:
                baseline_val = sum(base_samples) / len(base_samples)
                delta = latest_obs.value - baseline_val
                tol_pct = m.tolerance_pct or 0.0
                tol_band = abs(baseline_val) * (tol_pct / 100.0)
                req_delta = m.min_progress_delta if m.min_progress_delta is not None else tol_band
                baseline_already_met = _eval_comparator(baseline_val, m.threshold, m.comparator)
                no_reg = _eval_comparator(
                    latest_obs.value,
                    baseline_val,
                    ">=" if m.comparator in (">=", ">") else "<=" if m.comparator in ("<=", "<") else "==",
                    tolerance_pct=tol_pct,
                )
                if m.comparator in (">=", ">"):
                    moved_toward = delta > req_delta
                elif m.comparator in ("<=", "<"):
                    moved_toward = (-delta) > req_delta
                else:
                    moved_toward = (abs(baseline_val - m.threshold) - abs(latest_obs.value - m.threshold)) > req_delta

                passed = no_reg or (m.baseline_mode == "absolute" and target_met)
                progress = passed and (moved_toward or (target_met and not baseline_already_met))

                result = MetricResult(
                    key=m.key,
                    kind="target",
                    passed=passed,
                    progress=progress,
                    observed_value=latest_obs.value,
                    baseline_value=baseline_val,
                    delta=delta,
                    threshold=m.threshold,
                    comparator=m.comparator,
                    reason=None
                    if passed
                    else (f"Observed {latest_obs.value} regressed compared to baseline {baseline_val:.4f}"),
                )
                per_metric[m.key] = result
                if not passed:
                    has_fail = True
                    reasons.append(f"Metric '{m.key}': {result.reason}")

        elif m.baseline_mode == "absolute":
            passed = _eval_comparator(latest_obs.value, m.threshold, m.comparator)
            result = MetricResult(
                key=m.key,
                kind="guardrail",
                passed=passed,
                observed_value=latest_obs.value,
                threshold=m.threshold,
                comparator=m.comparator,
                reason=None if passed else f"Observed {latest_obs.value} not {m.comparator} {m.threshold}",
            )
            per_metric[m.key] = result
            if not passed:
                has_fail = True
                reasons.append(f"Metric '{m.key}': {result.reason}")

        elif m.baseline_mode == "no_regression":
            base_samples = _dedup_base_samples(m.key)
            if len(base_samples) < m.min_samples:
                per_metric[m.key] = MetricResult(
                    key=m.key,
                    kind="guardrail",
                    passed=False,
                    observed_value=latest_obs.value,
                    comparator=m.comparator,
                    reason=(f"Insufficient baseline samples for '{m.key}': got {len(base_samples)} < {m.min_samples}"),
                )
                has_insufficient = True
                reasons.append(f"Insufficient baseline samples for '{m.key}'")
            else:
                baseline_val = sum(base_samples) / len(base_samples)
                passed = _eval_comparator(
                    latest_obs.value,
                    baseline_val,
                    m.comparator,
                    tolerance_pct=m.tolerance_pct or 0.0,
                )
                result = MetricResult(
                    key=m.key,
                    kind="guardrail",
                    passed=passed,
                    observed_value=latest_obs.value,
                    baseline_value=baseline_val,
                    delta=latest_obs.value - baseline_val,
                    comparator=m.comparator,
                    reason=None
                    if passed
                    else (f"Observed {latest_obs.value} regressed compared to baseline {baseline_val:.4f}"),
                )
                per_metric[m.key] = result
                if not passed:
                    has_fail = True
                    reasons.append(f"Metric '{m.key}': {result.reason}")

    if has_insufficient:
        conclusion: Literal["pass", "fail", "insufficient", "blocked_requires_human"] = "insufficient"
    elif has_fail:
        conclusion = "fail"
    else:
        conclusion = "pass"

    overall_progress = conclusion == "pass" and any(
        r.kind == "target" and r.progress for r in per_metric.values()
    )

    return Decision(
        conclusion=conclusion,
        progress=overall_progress,
        reasons=reasons,
        per_metric=per_metric,
    )


def filter_trusted_observations(
    manifest: TenantManifest,
    observations: list[Observation],
    changed_paths: list[str],
) -> list[Observation]:
    """Excludes observations if the PR modified the metrics workflow path, preventing self-certification."""
    metrics_wf = manifest.metrics_workflow_path

    for changed in changed_paths:
        if changed == metrics_wf or changed.endswith(metrics_wf.split("/")[-1]):
            logger.warning(
                "PR modified metrics workflow '%s'; all PR metric observations are untrusted and excluded.",
                changed,
            )
            return []

    trusted: list[Observation] = []
    for obs in observations:
        if obs.workflow_path and obs.workflow_path != metrics_wf:
            logger.warning(
                "Observation workflow_path '%s' != manifest '%s', skipping.",
                obs.workflow_path,
                metrics_wf,
            )
            continue
        trusted.append(obs)

    return trusted


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:g}"


def _verdict_markdown(
    mode: str,
    decision: Any,
    head_sha: str,
    impl_pr: Optional[int],
    repo: str = "",
    base_sha: str = "",
) -> str:
    target = f"PR #{impl_pr} @ `{head_sha[:7]}`" if impl_pr else f"`{head_sha[:7]}`"
    lines = [f"🌊 **flow arbitrator** (`{mode}` mode) on {target}: **{decision.conclusion}**"]
    if decision.per_metric:
        lines += ["", "| Metric | PR | main (baseline) | Rule | Result |", "|---|---|---|---|---|"]
        for key, r in decision.per_metric.items():
            rule = f"{r.comparator} main" if r.baseline_value is not None else f"{r.comparator} {_fmt(r.threshold)}"
            result = "✅ pass" if r.passed else "❌ violated"
            from atobar_contracts.client_templates import resolve_cp_url

            cp_base = resolve_cp_url()
            link = f" ([policy]({cp_base}/tenants/{repo}#metric-{key}))" if repo else ""
            lines.append(f"| `{key}` | {_fmt(r.observed_value)} | {_fmt(r.baseline_value)} | {rule} | {result}{link} |")
    if base_sha:
        lines += ["", f"_Baseline (`main`): `{base_sha[:7]}`_"]
    if decision.reasons:
        lines += [""] + [f"- {reason}" for reason in decision.reasons]
    if mode == "shadow":
        lines += ["", "_Shadow mode: recorded only — no check run, no merge. Change the mode in the Control Panel._"]
    return "\n".join(lines)


async def run_arbitrator(
    manifest: TenantManifest,
    pr: int,
    head_sha: str,
    pr_observations: list[Observation],
    base_observations: list[Observation],
    changed_paths: list[str],
    gh: Optional[Any] = None,
    llm: Optional[Any] = None,
    impl_pr: Optional[int] = None,
    emit_event: Optional[Callable[..., Any]] = None,
) -> ArbitrationOutcome:
    """Executes the ARBITRATOR role: evaluates metric gate and acts per tenant mode."""
    mode = manifest.arbitration.mode
    if mode not in ("shadow", "recommend", "auto_merge"):
        mode = "recommend"

    trusted_pr_obs = filter_trusted_observations(
        manifest=manifest,
        observations=pr_observations,
        changed_paths=changed_paths,
    )

    decision = arbitrate(
        manifest=manifest,
        pr_observations=trusted_pr_obs,
        base_observations=base_observations,
        changed_paths=changed_paths,
    )

    reasons_text = "; ".join(decision.reasons) if decision.reasons else "All metrics passed"
    summary = f"{decision.conclusion}: {reasons_text}"[:1000]

    env_decision: Literal["pass", "fail", "blocked_requires_human"]
    if decision.conclusion in ("pass", "blocked_requires_human"):
        env_decision = decision.conclusion
    else:
        env_decision = "fail"

    if mode == "shadow":
        action_taken = "recorded"
    elif mode == "recommend":
        action_taken = "posted_recommendation"
    elif mode == "auto_merge":
        action_taken = "merged" if decision.conclusion == "pass" else "rejected"
    else:
        action_taken = "recorded"

    envelope = Arbitration(
        tenant=manifest.repo,
        pr=pr,
        head_sha=head_sha,
        run_id=os.environ.get("FLOW_RUN_ID"),
        decision=env_decision,
        mode=mode,
        action_taken=action_taken,
        observation_ids=[str(o.run_id) for o in trusted_pr_obs if o.run_id is not None],
        summary=summary,
        progress=decision.progress,
    )

    posted_comment_id: Optional[int] = None
    if gh is not None:
        base_sha = next((o.commit_sha for o in base_observations if o.commit_sha), "")
        verdict_md = _verdict_markdown(mode, decision, head_sha, impl_pr, repo=manifest.repo, base_sha=base_sha)
        issue_body = verdict_md if mode == "shadow" else f"{verdict_md}\n\n{render_hidden(envelope)}"
        posted = gh.post_pr_comment(repo=manifest.repo, pr_number=pr, body=issue_body)
        if isinstance(posted, dict) and posted.get("id") is not None:
            try:
                posted_comment_id = int(posted["id"])
            except (TypeError, ValueError):
                posted_comment_id = None
        elif isinstance(posted, int):
            posted_comment_id = posted
        violated = [r for r in decision.per_metric.values() if not r.passed]
        if impl_pr and impl_pr != pr:
            if mode == "shadow" and violated:
                gh.post_pr_comment(repo=manifest.repo, pr_number=impl_pr, body=verdict_md)
            elif mode in ("recommend", "auto_merge"):
                gh.post_pr_comment(repo=manifest.repo, pr_number=impl_pr, body=issue_body)

    if gh is not None and mode in ("recommend", "auto_merge"):
        check_conclusion = "success" if decision.conclusion == "pass" else "failure"
        check_summary = (
            f"Arbitration conclusion: {decision.conclusion}. "
            f"Reasons: {'; '.join(decision.reasons) if decision.reasons else 'None'}."
        )
        gh.create_check_run(
            repo=manifest.repo,
            name="flow/arbiter",
            head_sha=head_sha,
            conclusion=check_conclusion,
            output={
                "title": f"flow/arbiter: {decision.conclusion}",
                "summary": check_summary,
            },
        )

        if mode == "auto_merge" and decision.conclusion == "pass":
            merge_method = manifest.arbitration.merge_method or "squash"
            gh.merge_pr(
                repo=manifest.repo,
                pr_number=impl_pr or pr,
                commit_title=f"Merge PR #{impl_pr or pr} (auto-merged by flow-arbiter)",
                merge_method=merge_method,
            )
            for stale_lbl in ("flow:in-review", "flow:in-progress"):
                if hasattr(gh, "remove_label"):
                    try:
                        gh.remove_label(manifest.repo, pr, stale_lbl)
                    except Exception as exc:
                        logger.warning("Failed to remove label %s on %s#%s: %s", stale_lbl, manifest.repo, pr, exc)
            if hasattr(gh, "add_label"):
                try:
                    gh.add_label(manifest.repo, pr, "flow:shipped")
                except Exception as exc:
                    logger.warning("Failed to add flow:shipped on %s#%s: %s", manifest.repo, pr, exc)

    if (
        gh is not None
        and decision.conclusion == "pass"
        and mode in ("recommend", "shadow")
        and manifest.owner_github_login
        and hasattr(gh, "request_reviewers")
    ):
        resolved_pr = impl_pr
        if resolved_pr is None and hasattr(gh, "find_pr_for_sha"):
            try:
                resolved_pr = gh.find_pr_for_sha(manifest.repo, head_sha)
            except Exception as exc:
                logger.warning("Failed to resolve PR for sha %s on %s: %s", head_sha[:7], manifest.repo, exc)
                resolved_pr = None
        reviewer_ok = True
        if resolved_pr and hasattr(gh, "is_valid_assignee"):
            try:
                reviewer_ok = bool(gh.is_valid_assignee(manifest.repo, manifest.owner_github_login))
            except Exception as exc:
                logger.warning("Could not verify reviewer %s on %s: %s", manifest.owner_github_login, manifest.repo, exc)
            if not reviewer_ok:
                logger.warning(
                    "Reviewer %s is not assignable on %s (org or no access); skipping review request",
                    manifest.owner_github_login,
                    manifest.repo,
                )
        if resolved_pr and reviewer_ok:
            try:
                gh.request_reviewers(manifest.repo, int(resolved_pr), [manifest.owner_github_login])
                if mode == "shadow" and hasattr(gh, "create_comment"):
                    gh.create_comment(
                        manifest.repo,
                        int(resolved_pr),
                        "Arbitrator recommends merge (shadow mode: you decide).",
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to request review from %s on %s PR #%s: %s",
                    manifest.owner_github_login,
                    manifest.repo,
                    resolved_pr,
                    exc,
                )
        else:
            logger.warning(
                "Skipping review request on %s#%s (sha %s): no open PR resolved for head_sha",
                manifest.repo,
                pr,
                head_sha[:7],
            )

    if emit_event is not None:
        try:
            emit_event(
                "verdict",
                envelope="arbitration",
                mode=mode,
                decision=envelope.decision if envelope else decision.conclusion,
                reasons="; ".join(decision.reasons) if decision.reasons else "",
            )
        except Exception:
            pass

    return ArbitrationOutcome(
        mode=mode,
        decision=decision,
        action_taken=action_taken,
        summary=summary,
        envelope=envelope,
        comment_id=posted_comment_id,
    )
