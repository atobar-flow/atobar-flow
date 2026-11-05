# Nanobot SDK Reference

**Context-Hygiene Rule**: NEVER run `python -c "import inspect; print(inspect.getsource(...))"` on
`nanobot`. Use the signatures below or run targeted
`rg --no-ignore -n "<pattern>" .venv/lib/python3.12/site-packages/nanobot/`.

---

## 1. Key File Map (`.venv/lib/python3.12/site-packages/nanobot/`)

| Area | File Path |
|---|---|
| Top-level Facade | `.venv/lib/python3.12/site-packages/nanobot/nanobot.py` |
| Agent Loop | `.venv/lib/python3.12/site-packages/nanobot/agent/loop.py` |
| Shared Runner | `.venv/lib/python3.12/site-packages/nanobot/agent/runner.py` |
| Lifecycle Hooks | `.venv/lib/python3.12/site-packages/nanobot/agent/hook.py` |
| Tool Base & Registry | `.venv/lib/python3.12/site-packages/nanobot/agent/tools/base.py`, `registry.py` |
| MCP Tool Wrappers | `.venv/lib/python3.12/site-packages/nanobot/agent/tools/mcp.py` |
| LLM Provider & ToolCallRequest | `.venv/lib/python3.12/site-packages/nanobot/providers/base.py`, `factory.py` |
| Config Schema & Loader | `.venv/lib/python3.12/site-packages/nanobot/config/schema.py`, `loader.py` |
| Atobar Integration | `flow-agent/src/flow_agent/agents/nanobot_runtime.py`, `flow-agent/src/flow_agent/agents/nanobot_config.py` |

---

## 2. Core Class Signatures

### `Nanobot` (`.venv/lib/python3.12/site-packages/nanobot/nanobot.py`)
```python
class Nanobot:
    def __init__(
        self,
        loop: AgentLoop,
        *,
        config: Config | None = None,
        mcp_provider: MCPProvider | None = None,
    ) -> None: ...

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        model: str | None = None,
        model_preset: str | None = None,
    ) -> Nanobot: ...

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        ephemeral: bool = False,
        attributes: Mapping[str, Any] | None = None,
        hooks: list[AgentHook] | None = None,
        model: str | None = None,
    ) -> RunResult: ...
```

### `AgentLoop` (`.venv/lib/python3.12/site-packages/nanobot/agent/loop.py`)
```python
class AgentLoop:
    tools: ToolRegistry  # caller-owned or default ToolRegistry
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        tool_registry: ToolRegistry | None = None,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        ...,
    ) -> None: ...

    @classmethod
    def from_config(
        cls,
        config: Config,
        bus: MessageBus | None = None,
        *,
        tool_registry: ToolRegistry,
        **extra: Any,
    ) -> AgentLoop: ...

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        hook_factories: list[AgentTurnHookFactory] | None = None,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
        runtime: LLMRuntime | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> OutboundMessage | None: ...
```

### `AgentHook`, `AgentHookContext`, `AgentRunHookContext` (`.venv/lib/python3.12/site-packages/nanobot/agent/hook.py`)
```python
@dataclass(slots=True)
class AgentHookContext:
    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: LLMUsage | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    streamed_content: bool = False
    streamed_reasoning: bool = False
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    session_key: str | None = None

@dataclass(slots=True)
class AgentRunHookContext:
    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: LLMUsage | None = None
    stop_reason: str | None = None
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    exception: BaseException | None = None

class AgentHook:
    def __init__(self, reraise: bool = False) -> None: ...
    def wants_streaming(self) -> bool: ...
    async def before_run(self, context: AgentRunHookContext) -> None: ...
    async def after_run(self, context: AgentRunHookContext) -> None: ...
    async def on_error(self, context: AgentRunHookContext) -> None: ...
    async def on_finally(self, context: AgentRunHookContext) -> None: ...
    async def before_iteration(self, context: AgentHookContext) -> None: ...
    async def after_iteration(self, context: AgentHookContext) -> None: ...
    async def on_stream(self, context: AgentHookContext, delta: str) -> None: ...
    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None: ...
    async def on_provider_tool_event(self, context: AgentHookContext, event: dict[str, Any]) -> None: ...
    async def before_execute_tools(self, context: AgentHookContext) -> None: ...
    async def before_execute_tool(
        self, context: AgentHookContext, tool_call: ToolCallRequest, tool: Any, params: Any
    ) -> None: ...
    async def after_execute_tool(
        self, context: AgentHookContext, tool_call: ToolCallRequest, tool: Any, params: Any, result: Any
    ) -> None: ...
    async def on_execute_tool_error(
        self, context: AgentHookContext, tool_call: ToolCallRequest, tool: Any, params: Any, error: Any
    ) -> None: ...
    async def emit_reasoning(self, reasoning_content: str | None) -> None: ...
    async def emit_reasoning_end(self) -> None: ...
    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None: ...
```
*Note*: `CompositeHook._for_each_hook_safe` catches hook exceptions unless `reraise=True` on the hook, so tool allowlists must also unregister disallowed tools from `bot._loop.tools` (see `_prune_disallowed_tools` in `flow-agent/src/flow_agent/agents/nanobot_runtime.py`).

### `ToolCallRequest` (`.venv/lib/python3.12/site-packages/nanobot/providers/base.py`)
```python
@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Any  # dict[str, Any] or JSON string; normalize via parse_tool_arguments(arguments)
    extra_content: dict[str, Any] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def has_valid_name(self) -> bool: ...
    def to_openai_tool_call(self) -> dict[str, Any]: ...
```

### MCP Tool Wrappers (`.venv/lib/python3.12/site-packages/nanobot/agent/tools/mcp.py`)
```python
class MCPToolWrapper(_MCPWrapperBase):
    # Wrapped tool name format: _sanitize_mcp_tool_name(f"mcp_{server_name}_{tool_def.name}")
    # e.g., server "flow" + tool "create_pull_request" -> "mcp_flow_create_pull_request"
    def __init__(
        self,
        session: ClientSession,
        server_name: str,
        tool_def: MCPToolDefinition,
        tool_timeout: int = 30,
    ) -> None: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters(self) -> dict[str, Any]: ...
    async def execute(self, **kwargs: Any) -> str: ...

class MCPResourceWrapper(_MCPWrapperBase): ...
class MCPPromptWrapper(_MCPWrapperBase): ...

async def connect_mcp_servers(
    mcp_servers: dict[str, MCPServerConfig],
    registry: ToolRegistry,
    *,
    oauth_handlers: Mapping[str, MCPOAuthHandlers] | None = None,
) -> dict[str, MCPConnection]: ...

class MCPProvider:
    def __init__(
        self,
        servers: Mapping[str, MCPServerConfig],
        registry: ToolRegistry,
        *,
        server_loader: MCPServerLoader | None = None,
    ) -> None: ...
    async def connect(self) -> None: ...
    async def reload(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...
```
