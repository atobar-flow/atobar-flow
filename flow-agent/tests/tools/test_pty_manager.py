import asyncio

import pytest

from flow_agent.tools.pty_manager import PTYManager


@pytest.mark.asyncio
async def test_pty_session_creation_and_output():
    manager = PTYManager()

    # Run a simple echo command
    await manager.run_command("test-shell", "echo 'hello world'")

    # Let it execute
    await asyncio.sleep(0.1)

    output = manager.read_output("test-shell")
    assert "hello world" in output
    await manager.close()

@pytest.mark.asyncio
async def test_pty_session_persists_working_directory():
    manager = PTYManager()

    # Change directory
    await manager.run_command("test-shell-2", "cd /tmp")
    await asyncio.sleep(0.1)
    manager.read_output("test-shell-2") # clear buffer

    # Check directory
    await manager.run_command("test-shell-2", "pwd")
    await asyncio.sleep(0.1)
    output = manager.read_output("test-shell-2")

    assert "/tmp" in output
    await manager.close()
