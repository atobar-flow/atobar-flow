import asyncio
import re
import uuid

from flow_agent.core.config import get_flow_config
from flow_agent.tools.fs_tools import register_overflow_path
from flow_agent.tools.pty_manager import PTYManager

pty_manager = PTYManager()

# ANSI stripping regex
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def get_truncation_limit() -> int:
    config = get_flow_config()
    return int(config.get("max_stdout_truncation_chars", 4000))

async def execute_command(command: str, shell_id: str = "default", timeout: int = 30) -> str:
    """
    Executes a shell command in a persistent session. If output is massive,
    it will be head-truncated with an overflow file path.
    """
    delimiter = f"__DONE_{uuid.uuid4().hex}__"
    d1 = delimiter[:15]
    d2 = delimiter[15:]

    # Execute the command and then print the delimiter.
    # We split the delimiter in the command string so the PTY's local echo
    # doesn't contain the full concatenated delimiter, preventing a race condition.
    full_cmd = f"{command}\nprintf '\\n%s%s\\n' \"{d1}\" \"{d2}\""

    await pty_manager.run_command(shell_id, full_cmd)

    raw_output = ""
    elapsed = 0.0
    step = 0.1

    while elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step

        chunk = pty_manager.read_output(shell_id)
        if chunk:
            raw_output += chunk

        if delimiter in raw_output:
            break

    # Remove the delimiter and anything after it (like the prompt printing again)
    if delimiter in raw_output:
        raw_output = raw_output.split(delimiter)[0]

    # Strip ANSI escape codes (colors, carriage returns for progress bars)
    clean_output = ANSI_ESCAPE.sub('', raw_output)
    # Also clean up carriage returns that might overwrite lines
    clean_output = clean_output.replace('\r\n', '\n').replace('\r', '\n')

    # Head truncation logic
    limit = get_truncation_limit()

    if len(clean_output) > limit:
        truncated = clean_output[:limit]
        file_uuid = uuid.uuid4().hex
        overflow_path = register_overflow_path(f"/tmp/overflow_{shell_id}_{file_uuid}.log")

        with open(overflow_path, "w") as f:
            f.write(clean_output)

        notice = (
            f'\n\n<truncation_notice path="{overflow_path}">'
            'Output truncated. Use the ripgrep tool on this file to locate errors.'
            '</truncation_notice>'
        )
        return truncated + notice

    return clean_output
