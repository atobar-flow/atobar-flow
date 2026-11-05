"""Gitignored bookmark files so repeat `watch` calls print only what's new."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_DIR = Path(".atobar/ops-state")


def _path(repo: str, issue: int) -> Path:
    slug = repo.replace("/", "__")
    return STATE_DIR / f"{slug}-{issue}.json"


def load(repo: str, issue: int) -> dict[str, Any]:
    p = _path(repo, issue)
    if p.is_file():
        try:
            data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            return data
        except Exception:
            return {}
    return {}


def save(repo: str, issue: int, data: dict[str, Any]) -> None:
    p = _path(repo, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, default=str), encoding="utf-8")


def diff_new_events(repo: str, issue: int, events: list[dict[str, Any]], seq_key: str = "seq") -> list[dict[str, Any]]:
    """Returns only events newer than the last bookmarked seq, then updates the bookmark."""
    st = load(repo, issue)
    last_seen = st.get("last_event_seq", -1)
    new = [e for e in events if e.get(seq_key, -1) > last_seen]
    if events:
        st["last_event_seq"] = max(int(e.get(seq_key, -1)) for e in events)
        save(repo, issue, st)
    return new


def clear(repo: str, issue: int) -> None:
    p = _path(repo, issue)
    if p.is_file():
        p.unlink()
