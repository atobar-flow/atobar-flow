"""stdio MCP server exposing FLOW_TOOLS per-role with server-side allowlists.

Env contract:
Inherits GITHUB_TOKEN, TARGET_REPO, ISSUE_ID, FLOW_RUN_ID, WORKSPACE_DIR from
the spawning runtime — the same environment variables read by the in-process tools.
"""

import argparse
import functools
import inspect
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from flow_agent.runtime.tools import FLOW_TOOLS, ROLE_ALLOWLISTS, allowlist_for


def _wrap_tool(tool: Any) -> Any:
    name = tool.__name__

    if inspect.iscoroutinefunction(tool):
        @functools.wraps(tool)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            args_head = str(kwargs or args)[:4000]
            try:
                res = await tool(*args, **kwargs)
                dur_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit(
                        "tool_call",
                        name=name,
                        args_head=args_head,
                        result_head=str(res)[:4000],
                        duration_ms=dur_ms,
                        ok=True,
                        error=None,
                    )
                except Exception:
                    pass
                return res
            except Exception as e:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit(
                        "tool_call",
                        name=name,
                        args_head=args_head,
                        result_head="",
                        duration_ms=dur_ms,
                        ok=False,
                        error=str(e)[:4000],
                    )
                except Exception:
                    pass
                raise

        return async_wrapped
    else:
        @functools.wraps(tool)
        def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            args_head = str(kwargs or args)[:4000]
            try:
                res = tool(*args, **kwargs)
                dur_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit(
                        "tool_call",
                        name=name,
                        args_head=args_head,
                        result_head=str(res)[:4000],
                        duration_ms=dur_ms,
                        ok=True,
                        error=None,
                    )
                except Exception:
                    pass
                return res
            except Exception as e:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                try:
                    from flow_agent.telemetry.event_sink import emit
                    emit(
                        "tool_call",
                        name=name,
                        args_head=args_head,
                        result_head="",
                        duration_ms=dur_ms,
                        ok=False,
                        error=str(e)[:4000],
                    )
                except Exception:
                    pass
                raise

        return sync_wrapped


def create_server(role: str) -> FastMCP:
    """Creates a FastMCP server configured with allowed tools for the given role."""
    allowed = allowlist_for(role)
    server = FastMCP(f"flow-tools-{role}")

    for tool in FLOW_TOOLS:
        # Server-side allowlist is enforced by only registering allowed tools.
        if tool.__name__ in allowed:
            server.add_tool(_wrap_tool(tool))

    return server


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="flow-engine stdio MCP tools server")
    parser.add_argument(
        "--role",
        required=True,
        choices=sorted(ROLE_ALLOWLISTS),
        help="Swarm role to enforce allowlist for",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    server = create_server(role=args.role)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
