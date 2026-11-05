import asyncio
import os
import pty
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict


@dataclass
class PTYSession:
    shell_id: str
    process: asyncio.subprocess.Process
    fd: int
    buffer: Deque[bytes]

class PTYManager:
    def __init__(self) -> None:
        self.sessions: Dict[str, PTYSession] = {}

    async def get_or_create_session(self, shell_id: str) -> PTYSession:
        if shell_id in self.sessions:
            return self.sessions[shell_id]

        master, slave = pty.openpty()
        os.set_blocking(master, False)

        env = os.environ.copy()
        # Ensure Zero-Trust Subshells (Issue #112)
        # We strip sensitive control-plane tokens and credentials from the PTY sub-shell
        # so that prompted scripts or compromised client repositories cannot exfiltrate them.
        sensitive_patterns = [
            "GEMINI_API_KEY",
            "_API_KEY",
            "FLOW_LLM_",
            "FLOW_ROLE_MODEL",
            "GITHUB_TOKEN",
            "SUPABASE_SERVICE_KEY",
            "SUPABASE_URL",
            "FLOW_RUN_CREDENTIAL",
            "FLOW_RUN_ID",
            "ATOBAR_",
            "SECRET",
        ]
        for key in list(env.keys()):
            if any(p in key.upper() for p in sensitive_patterns):
                env.pop(key, None)

        process = await asyncio.create_subprocess_exec(
            "bash",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env=env
        )
        os.close(slave)

        buffer: Deque[bytes] = deque(maxlen=1048576) # 1MB byte buffer

        session = PTYSession(
            shell_id=shell_id,
            process=process,
            fd=master,
            buffer=buffer
        )

        loop = asyncio.get_running_loop()

        def _read_ready() -> None:
            try:
                data = os.read(master, 4096)
                if data:
                    buffer.append(data)
                else:
                    loop.remove_reader(master)
            except OSError:
                loop.remove_reader(master)

        loop.add_reader(master, _read_ready)

        self.sessions[shell_id] = session

        # Wait a moment for bash to initialize and flush any startup logs
        await asyncio.sleep(0.1)
        buffer.clear()

        return session

    async def run_command(self, shell_id: str, command: str) -> None:
        session = await self.get_or_create_session(shell_id)
        os.write(session.fd, f"{command}\n".encode('utf-8'))

    def read_output(self, shell_id: str) -> str:
        if shell_id not in self.sessions:
            return ""

        session = self.sessions[shell_id]
        data = b"".join(session.buffer).decode('utf-8', errors='replace')
        session.buffer.clear()
        return data

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        for shell_id, session in self.sessions.items():
            loop.remove_reader(session.fd)
            try:
                os.close(session.fd)
            except OSError:
                pass
            try:
                session.process.kill()
                await session.process.wait()
            except (ProcessLookupError, OSError):
                pass
        self.sessions.clear()
