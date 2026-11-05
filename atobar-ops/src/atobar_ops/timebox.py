"""Hard deadlines for every network or poll operation. Nothing in atobar-ops may hang.

Default --timeout: 100s under Claude Code (CLAUDECODE env var set; its own bash
default is 120s) otherwise 240s (Pi/Gemini, keeps the 5-minute prompt cache
warm). Hard cap 280s regardless of what's requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import subprocess
import time

HARD_CAP_SECONDS = 280


def default_timeout() -> int:
    return 100 if os.environ.get("CLAUDECODE") else 240


def resolve_timeout(requested: int | None) -> int:
    t = requested if requested is not None else default_timeout()
    return max(1, min(int(t), HARD_CAP_SECONDS))


@dataclass
class Deadline:
    seconds: int
    start: float = field(default_factory=time.monotonic)

    @property
    def at(self) -> float:
        return self.start + self.seconds

    def remaining(self) -> float:
        return max(0.0, self.at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.at

    def elapsed(self) -> int:
        return int(time.monotonic() - self.start)

    def sleep(self, chunk: float) -> None:
        time.sleep(max(0.0, min(chunk, self.remaining())))


def run(
    cmd: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run that always returns (never raises TimeoutExpired)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return subprocess.CompletedProcess(
            cmd, 124, stdout=stdout, stderr=stderr + f"\n[timebox] command timed out after {timeout}s"
        )
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"[timebox] {type(exc).__name__}: {exc}")


# Exit codes shared by every subcommand.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 2
