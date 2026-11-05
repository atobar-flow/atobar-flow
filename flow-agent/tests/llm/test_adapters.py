import json
from unittest.mock import MagicMock

from flow_agent.llm.claude import ClaudeClient
from flow_agent.llm.factory import get_client
from flow_agent.llm.openai_compatible import DeepSeekClient, GrokClient
from flow_agent.llm.protocol import Message, ToolSpec

# --- Recorded Fixtures ---

CLAUDE_TEXT_FIXTURE = {
    "id": "msg_01X9",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "I analyzed the evaluation spec and it looks valid."}],
    "model": "claude-3-5-sonnet-20241022",
    "usage": {"input_tokens": 120, "output_tokens": 40},
}

CLAUDE_TOOL_FIXTURE = {
    "id": "msg_02Y8",
    "type": "message",
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Calling tool to inspect file."},
        {
            "type": "tool_use",
            "id": "toolu_01A",
            "name": "read_file",
            "input": {"file_path": "README.md"},
        },
    ],
    "model": "claude-3-5-sonnet-20241022",
    "usage": {"input_tokens": 200, "output_tokens": 60},
}

OPENAI_COMPAT_TEXT_FIXTURE = {
    "id": "chatcmpl-123",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "DeepSeek response text here.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 80, "completion_tokens": 25, "total_tokens": 105},
}

OPENAI_COMPAT_TOOL_FIXTURE = {
    "id": "chatcmpl-456",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "execute_command",
                            "arguments": json.dumps({"command": "pytest -q"}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 150, "completion_tokens": 45, "total_tokens": 195},
}


# --- AC-1: Recorded-fixture tests per adapter ---

def test_ac1_claude_adapter_chat_and_tools():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = CLAUDE_TOOL_FIXTURE
    mock_resp.raise_for_status.return_value = None
    mock_http.post.return_value = mock_resp

    client = ClaudeClient(model="claude-3-5-sonnet", api_key="test-key", _http_client=mock_http)
    messages = [Message(role="user", content="Read README.md")]
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={"type": "object"})]

    reply = client.chat(messages, tools=tools, system_instruction="Be concise")

    assert reply.text == "Calling tool to inspect file."
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].args == {"file_path": "README.md"}
    assert reply.tool_calls[0].id == "toolu_01A"
    assert reply.usage.input_tokens == 200
    assert reply.usage.output_tokens == 60
    assert reply.usage.cost_usd > 0.0


def test_ac1_deepseek_adapter_chat_and_tools():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = OPENAI_COMPAT_TOOL_FIXTURE
    mock_resp.raise_for_status.return_value = None
    mock_http.post.return_value = mock_resp

    client = DeepSeekClient(model="deepseek-chat", api_key="test-key", _http_client=mock_http)
    messages = [Message(role="user", content="Run tests")]
    tools = [ToolSpec(name="execute_command", description="Run command", parameters={"type": "object"})]

    reply = client.chat(messages, tools=tools)

    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "execute_command"
    assert reply.tool_calls[0].args == {"command": "pytest -q"}
    assert reply.tool_calls[0].id == "call_abc123"
    assert reply.usage.input_tokens == 150
    assert reply.usage.output_tokens == 45
    assert reply.usage.cost_usd > 0.0


def test_ac1_grok_adapter_chat():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = OPENAI_COMPAT_TEXT_FIXTURE
    mock_resp.raise_for_status.return_value = None
    mock_http.post.return_value = mock_resp

    client = GrokClient(model="grok-beta", api_key="test-key", _http_client=mock_http)
    messages = [Message(role="user", content="Hello Grok")]

    reply = client.chat(messages)

    assert reply.text == "DeepSeek response text here."
    assert reply.usage.input_tokens == 80
    assert reply.usage.output_tokens == 25
    assert reply.usage.cost_usd > 0.0


def test_ac1_factory_supports_all_providers():
    claude = get_client("claude", "claude-3-5-sonnet", api_key="k")
    assert isinstance(claude, ClaudeClient)

    deepseek = get_client("deepseek", "deepseek-chat", api_key="k")
    assert isinstance(deepseek, DeepSeekClient)

    grok = get_client("grok", "grok-beta", api_key="k")
    assert isinstance(grok, GrokClient)
