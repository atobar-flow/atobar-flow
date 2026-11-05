"""Strict output budget so ops commands never flood an agent's context window."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .creds import redact

DEFAULT_MAX_CHARS = 4000
VERBOSE_MAX_CHARS = 40000


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    parser.add_argument("--verbose", action="store_true", help="Raise the output budget to 40,000 chars")
    parser.add_argument("--max-chars", type=int, default=None, help="Explicit output budget in characters")


def budget(args: argparse.Namespace) -> int:
    if getattr(args, "max_chars", None):
        return int(args.max_chars)
    return VERBOSE_MAX_CHARS if getattr(args, "verbose", False) else DEFAULT_MAX_CHARS


def render_text(lines: list[str], max_chars: int) -> str:
    text = redact("\n".join(lines))
    if len(text) <= max_chars:
        return text
    cut = text[: max(0, max_chars - 80)]
    return f"{cut}\n... [truncated: {len(text)} chars total; rerun with --verbose or --max-chars]"


def emit(args: argparse.Namespace, lines: list[str], data: Any = None) -> None:
    """Prints either compact/truncated text, or --json data (also redacted)."""
    if getattr(args, "json", False):
        payload = data if data is not None else {"lines": lines}
        print(redact(json.dumps(payload, default=str, separators=(",", ":"))))
        return
    print(render_text(lines, budget(args)))
