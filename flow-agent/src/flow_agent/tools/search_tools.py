import os
import subprocess
import uuid

from flow_agent.tools.exec_tool import get_truncation_limit
from flow_agent.tools.fs_tools import _resolve_path_in_workspace, register_overflow_path


def search_codebase(pattern: str, directory: str = ".", include_glob: str = "*") -> str:
    """
    Executes ripgrep (rg) for fixed strings to find code.
    If the output is too large, it is head-truncated and the rest is dumped to a file.
    """
    try:
        ws = os.environ.get("WORKSPACE_DIR", "").strip()
        if directory == "." and ws and os.path.exists(ws):
            directory = ws
        directory = _resolve_path_in_workspace(directory, allow_overflow=True)
        cmd = [
            "rg",
            "--fixed-strings",
            "-n",
            "--heading",
            "-C", "2",
            "-g", include_glob,
            pattern,
            directory
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"

        if not output.strip() and result.returncode != 0:
            if result.returncode == 1:
                return "No matches found."
            return f"Error executing ripgrep: Return code {result.returncode}"

        limit = get_truncation_limit()

        if len(output) > limit:
            truncated = output[:limit]
            file_uuid = uuid.uuid4().hex
            overflow_path = register_overflow_path(f"/tmp/overflow_rg_{file_uuid}.log")

            with open(overflow_path, "w", encoding="utf-8") as f:
                f.write(output)

            notice = (
                f'\n\n<truncation_notice path="{overflow_path}">'
                'Output truncated. Use the ripgrep tool on this file to locate errors.'
                '</truncation_notice>'
            )
            return truncated + notice

        return output

    except PermissionError:
        raise
    except Exception as e:
        return f"Exception executing ripgrep: {str(e)}"
