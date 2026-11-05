"""Agent runtime registry."""

from typing import Dict, Type

from flow_agent.agents.base import AgentRuntime, RoleResult, RoleTask, RuntimeHooks, UsageLedger


class DirectNativeRuntime(AgentRuntime):
    """Direct llm_loop runtime for native execution."""

    async def run(self, task: RoleTask, *, hooks: RuntimeHooks) -> RoleResult:
        from flow_agent.roles.base import RoleSpec
        from flow_agent.runtime.llm_loop import llm_loop

        if task.llm is None:
            raise ValueError("DirectNativeRuntime requires task.llm to be provided.")

        from flow_agent.roles.evaluator.spec import evaluator_done_predicate

        done_fn = (
            evaluator_done_predicate
            if task.done_envelope == "flow-verdict"
            else (lambda reply: bool(reply.text and task.done_envelope in reply.text))
        )
        spec = RoleSpec(
            name=task.role,
            system_prompt=task.system_prompt,
            tools=task.allowed_tools,
            llm=task.llm,
            done=done_fn,
        )
        reply = await llm_loop(
            spec=spec,
            task_prompt=task.task_prompt,
            context=task.context,
            registry=hooks.registry,
            telemetry_ui=hooks.telemetry_ui,
            circuit_breaker=hooks.circuit_breaker,
        )
        return RoleResult(
            final_text=reply.text or "",
            envelope_text=reply.text,
            usage=UsageLedger(),
            halt="done",
        )


_REGISTRY: Dict[str, Type[AgentRuntime]] = {
    "native": DirectNativeRuntime,
}


def register_runtime(name: str, runtime_cls: Type[AgentRuntime]) -> None:
    """Registers an AgentRuntime implementation."""
    _REGISTRY[name] = runtime_cls


def get_runtime(name: str) -> AgentRuntime:
    """Returns an instantiated AgentRuntime by name.

    Raises ValueError if runtime is unknown.
    """
    if name == "nanobot" and "nanobot" not in _REGISTRY:
        from flow_agent.agents.nanobot_runtime import NanobotRuntime
        _REGISTRY["nanobot"] = NanobotRuntime

    if name not in _REGISTRY:
        raise ValueError(f"Unknown agent runtime: '{name}'. Supported: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[name]()
