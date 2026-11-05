"""EVALUATOR swarm role module."""

from flow_agent.roles.evaluator.prompt import EVALUATOR_SYSTEM_PROMPT
from flow_agent.roles.evaluator.runner import run_evaluator
from flow_agent.roles.evaluator.spec import (
    StaleShaError,
    build_evaluator_context,
    create_evaluator_verdict,
    evaluator_done_predicate,
    format_evaluator_review_summary,
    get_evaluator_spec,
    post_evaluator_verdict,
    validate_evaluator_finding,
)
from flow_agent.roles.evaluator.visual import (
    VisualArtifact,
    capture_evaluator_screenshot,
    record_visual_artifact,
)

__all__ = [
    "EVALUATOR_SYSTEM_PROMPT",
    "StaleShaError",
    "VisualArtifact",
    "build_evaluator_context",
    "capture_evaluator_screenshot",
    "create_evaluator_verdict",
    "evaluator_done_predicate",
    "format_evaluator_review_summary",
    "get_evaluator_spec",
    "post_evaluator_verdict",
    "record_visual_artifact",
    "run_evaluator",
    "validate_evaluator_finding",
]
