"""ARBITRATOR swarm role module."""

from flow_agent.roles.arbitrator.gate import (
    Decision,
    MetricResult,
    Observation,
    RunInfo,
    arbitrate,
    verify_provenance,
)
from flow_agent.roles.arbitrator.run import (
    ArbitrationOutcome,
    filter_trusted_observations,
    run_arbitrator,
)

__all__ = [
    "ArbitrationOutcome",
    "Decision",
    "MetricResult",
    "Observation",
    "RunInfo",
    "arbitrate",
    "filter_trusted_observations",
    "run_arbitrator",
    "verify_provenance",
]
