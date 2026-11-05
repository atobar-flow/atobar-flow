import os

import pytest

from flow_agent.tools.exec_tool import execute_command, pty_manager


@pytest.fixture(autouse=True)
async def cleanup():
    yield
    await pty_manager.close()

@pytest.mark.asyncio
async def test_execute_basic_command():
    output = await execute_command("echo 'test basic'", shell_id="basic")
    assert "test basic" in output
    assert "<truncation_notice" not in output

@pytest.mark.asyncio
async def test_ansi_stripping():
    # Echo green text: \033[32mhello\033[0m
    # We use a python one-liner to reliably print ansi escapes in bash
    cmd = "python3 -c \"print('\\033[32mhello green\\033[0m')\""
    output = await execute_command(cmd, shell_id="ansi")
    assert "hello green" in output
    assert "\x1b" not in output
    # Note: we don't assert "[32m" not in output because the literal string is in the echoed command

@pytest.mark.asyncio
async def test_head_truncation_and_overflow():
    # Print 5000 characters of 'A'
    cmd = "python3 -c \"print('A' * 5000)\""
    output = await execute_command(cmd, shell_id="trunc")

    # Check length and truncation notice
    assert len(output) > 4000
    assert "A" * 3500 in output # Reduced from 4000 to account for echoed command chars
    assert len(output) < 4500 # Should be 4000 + notice length
    assert "<truncation_notice path=\"/tmp/overflow_trunc_" in output

    # Extract file path
    import re
    match = re.search(r'path="([^"]+)"', output)
    assert match is not None

    overflow_path = match.group(1)
    assert os.path.exists(overflow_path)

    # Check file content
    with open(overflow_path, 'r') as f:
        file_content = f.read()
        assert len(file_content) >= 5000
        assert "A" * 5000 in file_content
