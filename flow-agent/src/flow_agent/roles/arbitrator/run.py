"""Execution and action dispatch for the ARBITRATOR role (delegating to atobar_contracts.arbitration)."""

from typing import Any, Optional

from atobar_contracts.arbitration import (
    ArbitrationOutcome,
    Decision,
    Observation,
    _fmt,
    _verdict_markdown,
    arbitrate,
    filter_trusted_observations,
)
from atobar_contracts.arbitration import run_arbitrator as _contracts_run_arbitrator
from atobar_contracts.manifest import TenantManifest

__all__ = [
    "ArbitrationOutcome",
    "Decision",
    "Observation",
    "_fmt",
    "_verdict_markdown",
    "arbitrate",
    "filter_trusted_observations",
    "run_arbitrator",
]


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
) -> ArbitrationOutcome:
    """Executes the ARBITRATOR role: evaluates metric gate and acts per tenant mode."""
    emit_fn = None
    try:
        from flow_agent.telemetry.event_sink import emit

        emit_fn = emit
    except Exception:
        emit_fn = None

    return await _contracts_run_arbitrator(
        manifest=manifest,
        pr=pr,
        head_sha=head_sha,
        pr_observations=pr_observations,
        base_observations=base_observations,
        changed_paths=changed_paths,
        gh=gh,
        llm=llm,
        impl_pr=impl_pr,
        emit_event=emit_fn,
    )
