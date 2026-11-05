"""Parallel tool calls streamed by Gemini's OpenAI-compatible endpoint must stay separate."""

from typing import Any

from openai.types.chat import ChatCompletionChunk

from flow_agent.agents import nanobot_patches


def _chunk(tool_calls: list[dict[str, Any]], finish: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "gemini",
        "choices": [{"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": finish}],
    })


def _call(index: Any, call_id: str | None, name: str | None, args: str) -> dict[str, Any]:
    fn: dict[str, Any] = {"arguments": args}
    if name:
        fn["name"] = name
    tc: dict[str, Any] = {"index": index, "type": "function", "function": fn}
    if call_id:
        tc["id"] = call_id
    return tc


def _parse(chunks: list[Any]) -> list[tuple[str, Any]]:
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    nanobot_patches.install()
    resp = OpenAICompatProvider._parse_chunks(chunks)
    return [(tc.name, tc.arguments) for tc in resp.tool_calls]


def test_gemini_parallel_calls_at_same_index_are_split_by_id():
    chunks = [
        _chunk([_call(0, "a", "mcp_flow_read_file", '{"file_path": "src/monitor_tui.py"}')]),
        _chunk([_call(0, "b", "mcp_flow_read_file", '{"file_path": "src/ollama_proxy.py"}')]),
        _chunk([_call(0, "c", "search_codebase", '{"query": "version"}')], finish="tool_calls"),
    ]
    assert _parse(chunks) == [
        ("mcp_flow_read_file", {"file_path": "src/monitor_tui.py"}),
        ("mcp_flow_read_file", {"file_path": "src/ollama_proxy.py"}),
        ("search_codebase", {"query": "version"}),
    ]


def test_parallel_calls_without_ids_are_split_on_new_name_after_complete_args():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [_call(0, None, "read_file", "{}")]}}]},
        {"choices": [{"delta": {"tool_calls": [_call(0, None, "read_file", '{"path": "src/a.py"}')]}}]},
    ]
    assert _parse(chunks) == [("read_file", {}), ("read_file", {"path": "src/a.py"})]


def test_standard_openai_fragmented_stream_is_unchanged():
    chunks = [
        _chunk([_call(0, "a", "read_file", ""), _call(1, "b", "grep", "")]),
        _chunk([_call(0, None, None, '{"path": '), _call(1, None, None, '{"pattern": ')]),
        _chunk([_call(0, None, None, '"x.py"}'), _call(1, None, None, '"foo"}')], finish="tool_calls"),
    ]
    assert _parse(chunks) == [("read_file", {"path": "x.py"}), ("grep", {"pattern": "foo"})]


def test_same_id_repeated_on_every_fragment_is_one_call():
    chunks = [
        _chunk([_call(0, "a", "read_file", '{"path": ')]),
        _chunk([_call(0, "a", None, '"x.py"}')], finish="tool_calls"),
    ]
    assert _parse(chunks) == [("read_file", {"path": "x.py"})]


def test_install_is_idempotent():
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    nanobot_patches.install()
    first = OpenAICompatProvider.__dict__["_parse_chunks"]
    nanobot_patches.install()
    assert OpenAICompatProvider.__dict__["_parse_chunks"] is first


def test_fresh_id_on_every_fragment_does_not_cut_a_call_mid_json():
    chunks = [
        _chunk([_call(0, "a", "read_file", "")]),
        _chunk([_call(0, "b", None, '{"pa')]),
        _chunk([_call(0, "c", None, 'th": "x.py"}')], finish="tool_calls"),
    ]
    assert _parse(chunks) == [("read_file", {"path": "x.py"})]


def test_parallel_zero_arg_calls_with_ids_are_split():
    chunks = [
        _chunk([_call(0, "a", "git_pr_checks", "")]),
        _chunk([_call(0, "b", "git_ci_job_logs", "")], finish="tool_calls"),
    ]
    assert _parse(chunks) == [("git_pr_checks", {}), ("git_ci_job_logs", {})]
