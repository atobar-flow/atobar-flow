"""Narrow, version-pinned fixes for nanobot-ai behaviour that breaks factory runs.

Each patch is idempotent and is installed once by ``install()`` before a run starts.
"""

import json
from typing import Any

_INSTALLED_ATTR = "_flow_parallel_tool_call_fix"


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _set_index(tc: Any, index: int) -> None:
    if isinstance(tc, dict):
        tc["index"] = index
    else:
        setattr(tc, "index", index)


def _tool_call_deltas(chunk: Any) -> list[Any]:
    choices = _get(chunk, "choices") or []
    if not choices:
        return []
    delta = _get(choices[0], "delta")
    if delta is None:
        return []
    return list(_get(delta, "tool_calls") or [])


def _is_complete_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def reindex_parallel_tool_call_deltas(chunks: list[Any]) -> None:
    """Give every streamed tool call its own ``index`` before nanobot accumulates them.

    nanobot 0.3.5 concatenates streamed tool-call deltas by ``index``. Gemini's
    OpenAI-compatible endpoint streams each parallel call as its own complete delta,
    all at index 0, so three ``read_file`` calls arrived as one call with arguments
    ``{...}{...}{...}``, failed validation, and pushed the model into one-file-per-turn
    reads. A delta starts a new call when it carries a call id different from the
    slot's id and the slot's call is finished (complete JSON arguments, or no arguments
    and the delta names its own function), or, when ids are absent, a second function
    name after complete JSON arguments. Standard OpenAI fragment streams send the id
    and name only on a call's first fragment, so they never meet either condition.
    """
    slot_of: dict[Any, int] = {}  # provider index -> our current index
    slot_id: dict[int, str] = {}
    slot_named: dict[int, bool] = {}
    slot_args: dict[int, str] = {}
    next_index = 0

    for chunk in chunks:
        if isinstance(chunk, str):
            continue
        for pos, tc in enumerate(_tool_call_deltas(chunk)):
            raw_index = _get(tc, "index")
            key = raw_index if raw_index is not None else pos
            call_id = str(_get(tc, "id") or "")
            fn = _get(tc, "function")
            name = str(_get(fn, "name") or "") if fn is not None else ""
            args = str(_get(fn, "arguments") or "") if fn is not None else ""

            slot = slot_of.get(key)
            if slot is not None:
                if call_id and slot_id[slot]:
                    # Some proxies send a fresh id per fragment; never cut a call mid-JSON.
                    prev = slot_args[slot]
                    prev_done = _is_complete_json(prev) or (not prev.strip() and bool(name))
                    starts_new = call_id != slot_id[slot] and prev_done
                else:
                    starts_new = bool(name) and slot_named[slot] and _is_complete_json(slot_args[slot])
                if starts_new:
                    slot = None
            if slot is None:
                slot = next_index
                next_index += 1
                slot_of[key] = slot
                slot_id[slot] = ""
                slot_named[slot] = False
                slot_args[slot] = ""

            if call_id:
                slot_id[slot] = call_id
            if name:
                slot_named[slot] = True
            slot_args[slot] += args
            _set_index(tc, slot)


def install() -> None:
    """Install the patches (safe to call repeatedly)."""
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    if getattr(OpenAICompatProvider, _INSTALLED_ATTR, False):
        return
    original = OpenAICompatProvider.__dict__["_parse_chunks"].__func__

    def _parse_chunks(cls: Any, chunks: list[Any]) -> Any:
        reindex_parallel_tool_call_deltas(chunks)
        return original(cls, chunks)

    OpenAICompatProvider._parse_chunks = classmethod(_parse_chunks)  # type: ignore[method-assign,assignment]
    setattr(OpenAICompatProvider, _INSTALLED_ATTR, True)
