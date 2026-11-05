from unittest.mock import MagicMock

import pytest

from flow_engine.conductor.machine import AckEvent, LoopLimits, LoopState, VerdictEvent, step
from flow_agent.core.watchdog import CircuitBreaker, CircuitBreakerException
from atobar_contracts.envelopes import Ack, Verdict
from flow_agent.roles.base import RoleSpec
from flow_agent.runtime.llm_loop import llm_loop


def test_j06_ac1_step_cost_accumulation_across_events():
    state = LoopState(tenant="owner/repo", pr=42, head_sha="a" * 40)

    ack1 = AckEvent(ack=Ack(tenant="owner/repo", pr=42, head_sha="a" * 40, dispute=[]), cost_usd=10.0)
    verdict1 = VerdictEvent(
        verdict=Verdict(
            tenant="owner/repo", pr=42, head_sha="a" * 40, round=1, verdict="changes_requested", findings=[]
        ),
        cost_usd=20.0,
    )

    limits = LoopLimits(max_pr_cost_usd=25.0)

    s1, a1 = step(state, ack1, limits)
    assert s1.accumulated_cost_usd == 10.0

    s2, a2 = step(s1, verdict1, limits)
    assert s2.accumulated_cost_usd == 30.0
    assert s2.status == "escalated"
    assert "max_pr_cost_usd" in s2.escalation_reason


@pytest.mark.asyncio
async def test_j06_ac2_circuit_breaker_trips():
    breaker = CircuitBreaker(max_wall_clock_minutes=0)

    # Fake LLM that never finishes
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text="still thinking...", tool_calls=[])

    spec = RoleSpec(name="test", system_prompt="prompt", tools=[], llm=llm, done=lambda r: False)

    # Ensure it raises
    import time

    time.sleep(0.01)  # ensure time passes so breaker trips on check_time
    with pytest.raises(CircuitBreakerException):
        await llm_loop(spec, "task", circuit_breaker=breaker)
