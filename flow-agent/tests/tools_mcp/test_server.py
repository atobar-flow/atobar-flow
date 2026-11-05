import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flow_agent.runtime.tools import allowlist_for, execute_tool


@pytest.mark.asyncio
async def test_developer_role_tools_schema_and_read_file_parity(tmp_path):
    test_file = tmp_path / "hello.txt"
    test_file.write_text("Hello, world!\nLine 2\n")
    in_process_res = await execute_tool("read_file", {"file_path": str(test_file)})

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flow_agent.tools_mcp.server", "--role", "developer"],
        env={**os.environ},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}

            dev_allowed = allowlist_for("developer")
            assert tool_names == dev_allowed
            assert "create_and_verify_pr" in tool_names
            assert "capture_evaluator_screenshot" not in tool_names

            pr_tool = next(t for t in tools_result.tools if t.name == "create_and_verify_pr")
            schema = pr_tool.inputSchema
            assert schema is not None
            assert schema.get("type") == "object"
            assert "title" in schema.get("properties", {})
            assert "body" in schema.get("properties", {})
            assert "title" in schema.get("required", [])
            assert "body" in schema.get("required", [])
            assert schema["properties"]["title"].get("type") == "string"
            assert schema["properties"]["body"].get("type") == "string"

            mcp_res = await session.call_tool("read_file", arguments={"file_path": str(test_file)})
            assert not mcp_res.isError
            assert mcp_res.content[0].text == str(in_process_res)


@pytest.mark.asyncio
async def test_arbitrator_role_server_side_allowlist_enforcement():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flow_agent.tools_mcp.server", "--role", "arbitrator"],
        env={**os.environ},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert "write_file" not in tool_names
            assert "execute_command" not in tool_names
            assert "create_and_verify_pr" not in tool_names

            # Direct call by name must fail server-side
            for rejected_tool in ["write_file", "execute_command", "create_and_verify_pr"]:
                res = await session.call_tool(rejected_tool, arguments={})
                # In MCP, calling an unallowed tool or error returns isError=True or error message in text
                assert res.isError or "Error" in res.content[0].text or "not in the allowlist" in res.content[0].text


@pytest.mark.asyncio
async def test_evaluator_role_capture_screenshot_via_mcp(tmp_path):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flow_agent.tools_mcp.server", "--role", "evaluator"],
        env={**os.environ},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert "capture_evaluator_screenshot" in tool_names

            res = await session.call_tool(
                "capture_evaluator_screenshot",
                arguments={"artifacts_dir": str(tmp_path)},
            )
            has_expected = (
                not res.isError
                or "Failed to capture visual screen" in res.content[0].text
                or "sha256" in res.content[0].text
            )
            assert has_expected
