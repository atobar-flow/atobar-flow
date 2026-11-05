"""Compatibility re-export of deterministic metric gate from atobar_contracts.arbitration."""

from atobar_contracts.arbitration import (
    Decision,
    MetricResult,
    Observation,
    RunInfo,
    _eval_comparator,
    _path_matches_pattern,
    arbitrate,
    verify_provenance,
)

__all__ = [
    "Decision",
    "MetricResult",
    "Observation",
    "RunInfo",
    "_eval_comparator",
    "_path_matches_pattern",
    "arbitrate",
    "verify_provenance",
]
