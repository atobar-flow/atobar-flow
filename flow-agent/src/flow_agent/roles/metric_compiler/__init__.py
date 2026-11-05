"""METRIC_COMPILER role: compiles a plain-language metric intent into a dry-run-verified Metric spec."""

from flow_agent.roles.metric_compiler.prompt import METRIC_COMPILER_SYSTEM_PROMPT
from flow_agent.roles.metric_compiler.runner import (
    MetricCompilationError,
    dry_run_metric,
    parse_compiled_metric,
    run_metric_compiler,
)

__all__ = [
    "METRIC_COMPILER_SYSTEM_PROMPT",
    "MetricCompilationError",
    "dry_run_metric",
    "parse_compiled_metric",
    "run_metric_compiler",
]
