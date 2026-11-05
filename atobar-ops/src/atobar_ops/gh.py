"""Thin, timeboxed wrapper around the `gh` CLI shared by every subcommand."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from . import timebox


class GhError(RuntimeError):
    pass


def run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return timebox.run(["gh", *args], timeout=timeout)


def json_or_raise(args: list[str], *, timeout: float = 30) -> Any:
    res = run(args, timeout=timeout)
    if res.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {res.stderr.strip()[:300]}")
    out = res.stdout.strip()
    return json.loads(out) if out else []


def json_or_empty(args: list[str], *, timeout: float = 30) -> Any:
    try:
        return json_or_raise(args, timeout=timeout)
    except Exception:
        return []
