"""Common agent runtime protocol, task/result models, usage ledgers, and hooks."""

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from flow_agent.core.watchdog import CircuitBreaker
from flow_agent.llm.protocol import LLMClient, Usage
from flow_agent.telemetry.github_ui import GitHubTelemetryUI
from flow_agent.telemetry.session_registry import SessionRegistry

HaltReason = Literal[
    "done",
    "stall",
    "provider_quota",
    "provider_error",
    "crash",
]


class UsageDetail(Usage):
    """Extended model usage metrics populated when reported by provider."""

    cached_input_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class ModelCallRecord:
    """Runtime-agnostic model call log record."""

    iteration: int
    provider: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: Optional[float] = None
    stop_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "iteration": self.iteration,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }
        if self.cached_input_tokens is not None:
            d["cached_input_tokens"] = self.cached_input_tokens
        if self.cache_write_tokens is not None:
            d["cache_write_tokens"] = self.cache_write_tokens
        if self.latency_ms is not None:
            d["latency_ms"] = round(self.latency_ms, 2)
        if self.stop_reason is not None:
            d["stop_reason"] = self.stop_reason
        return d


@dataclass
class ToolUsageBreakdown:
    """Per-tool execution and token usage metrics."""

    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    result_chars: int = 0
    result_tokens: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "result_chars": self.result_chars,
            "result_tokens": self.result_tokens,
            "duration_ms": self.duration_ms,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class UsageLedger:
    """Aggregate model usage and granular call trace."""

    total: UsageDetail = field(default_factory=UsageDetail)
    calls: list[ModelCallRecord] = field(default_factory=list)
    by_tool: dict[str, ToolUsageBreakdown] = field(default_factory=dict)

    def record_tool_call(
        self,
        tool_name: str,
        *,
        result_chars: int = 0,
        duration_ms: int = 0,
        is_error: bool = False,
    ) -> None:
        if not tool_name:
            return
        entry = self.by_tool.setdefault(tool_name, ToolUsageBreakdown())
        entry.calls += 1
        if is_error:
            entry.errors += 1
        entry.result_chars += max(0, int(result_chars))
        if result_chars > 0:
            entry.result_tokens += max(1, int(result_chars) // 4)
        entry.duration_ms += max(0, int(duration_ms))

    def record_tool_tokens(
        self,
        tool_names: list[str],
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        names = [n for n in tool_names if n]
        if not names:
            return
        count = len(names)
        for idx, name in enumerate(names):
            entry = self.by_tool.setdefault(name, ToolUsageBreakdown())
            in_share = (input_tokens // count) + (1 if idx < (input_tokens % count) else 0)
            out_share = (output_tokens // count) + (1 if idx < (output_tokens % count) else 0)
            tot_share = (total_tokens // count) + (1 if idx < (total_tokens % count) else 0)
            entry.input_tokens += max(0, in_share)
            entry.output_tokens += max(0, out_share)
            entry.total_tokens += max(0, tot_share)

    def to_dict(self) -> dict[str, Any]:
        total_dict: dict[str, Any] = {
            "input_tokens": self.total.input_tokens,
            "output_tokens": self.total.output_tokens,
            "cost_usd": round(self.total.cost_usd, 6),
        }
        if self.total.cached_input_tokens is not None:
            total_dict["cached_input_tokens"] = self.total.cached_input_tokens
        if self.total.cache_write_tokens is not None:
            total_dict["cache_write_tokens"] = self.total.cache_write_tokens
        if self.total.total_tokens is not None:
            total_dict["total_tokens"] = self.total.total_tokens

        res: dict[str, Any] = {
            "agent_runtime": os.environ.get("FLOW_AGENT_RUNTIME", "nanobot"),
            "total": total_dict,
            "calls": [c.to_dict() for c in self.calls],
        }
        if self.by_tool:
            res["by_tool"] = {k: v.to_dict() for k, v in self.by_tool.items()}
        return res


@dataclass
class RuntimeHooks:
    """Factory callbacks carrying control-plane internals."""

    registry: Optional[SessionRegistry] = None
    telemetry_ui: Optional[GitHubTelemetryUI] = None
    circuit_breaker: Optional[CircuitBreaker] = None


@dataclass
class RoleTask:
    """Specification of a task given to an AgentRuntime."""

    role: str
    system_prompt: str
    context: str
    task_prompt: str
    workspace_dir: str
    allowed_tools: frozenset[str]
    model_name: str
    done_envelope: Literal["flow-ack", "flow-verdict", "flow-plan"]
    llm: Optional[LLMClient] = None  # Populated for NativeRuntime


@dataclass
class RoleResult:
    """Result returned by an AgentRuntime upon completion or halt."""

    final_text: str
    envelope_text: Optional[str]
    usage: UsageLedger
    halt: HaltReason
    halt_detail: Optional[str] = None


@runtime_checkable
class AgentRuntime(Protocol):
    """Pluggable agent execution runtime protocol."""

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        """Executes a RoleTask under the provided hooks."""
        ...
