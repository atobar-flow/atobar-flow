import pytest

from flow_agent.agents.base import AgentRuntime
from flow_agent.agents.registry import DirectNativeRuntime, get_runtime


def test_get_runtime_native():
    runtime = get_runtime("native")
    assert isinstance(runtime, (AgentRuntime, DirectNativeRuntime))


def test_get_runtime_unknown():
    with pytest.raises(ValueError, match="Unknown agent runtime: 'unknown'"):
        get_runtime("unknown")
