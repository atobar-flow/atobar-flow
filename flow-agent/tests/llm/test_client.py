from unittest.mock import MagicMock

import pytest

from flow_agent.llm import (
    FakeLLMClient,
    GeminiClient,
    LLMClient,
    Message,
    Reply,
    ToolCall,
    ToolSpec,
    Usage,
    get_client,
)
from flow_agent.llm.pricing import calculate_cost


def test_ac1_fake_client_satisfies_protocol():
    canned_reply = Reply(
        text="Hello!",
        tool_calls=[ToolCall(name="bash", args={"command": "ls"})],
        usage=Usage(input_tokens=50, output_tokens=10, cost_usd=0.0001),
    )
    fake = FakeLLMClient(canned_replies=[canned_reply])

    assert isinstance(fake, LLMClient)
    reply = fake.chat([Message(role="user", content="run ls")])
    assert reply.text == "Hello!"
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "bash"
    assert reply.tool_calls[0].args == {"command": "ls"}
    assert len(fake.history) == 1


def test_ac1_get_client_factory():
    client = get_client("gemini", model="gemini-2.5-flash", api_key="fake-test-key-abcd")
    assert isinstance(client, GeminiClient)

    fake = get_client("fake", model="fake-model")
    assert isinstance(fake, FakeLLMClient)

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        get_client("unsupported_provider", model="test")


def test_ac2_gemini_adapter_maps_tool_calls_from_fixture():
    # Construct a recorded Gemini SDK response fixture without calling network
    mock_genai_client = MagicMock()

    mock_call = MagicMock()
    mock_call.name = "execute_command"
    mock_call.args = {"cmd": "pytest tests/"}

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 1200
    mock_usage.candidates_token_count = 350

    mock_response = MagicMock()
    mock_response.text = None
    mock_response.function_calls = [mock_call]
    mock_response.usage_metadata = mock_usage

    mock_genai_client.models.generate_content.return_value = mock_response

    adapter = GeminiClient(
        model="gemini-2.5-flash",
        api_key="fake-test-key-abcd",
        _genai_client=mock_genai_client,
    )

    messages = [Message(role="user", content="Run tests")]
    tools = [
        ToolSpec(
            name="execute_command",
            description="Run bash command",
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        )
    ]

    reply = adapter.chat(messages=messages, tools=tools)

    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "execute_command"
    assert reply.tool_calls[0].args == {"cmd": "pytest tests/"}
    assert reply.usage.input_tokens == 1200
    assert reply.usage.output_tokens == 350
    assert reply.usage.cost_usd > 0


def test_ac3_cost_calculation():
    # gemini-2.5-flash: 0.075 per 1M input, 0.30 per 1M output
    cost = calculate_cost("gemini-2.5-flash", input_tokens=1_000_000, output_tokens=1_000_000)
    assert pytest.approx(cost, 1e-5) == 0.375

    # Half million
    cost_half = calculate_cost("gemini-2.5-flash", input_tokens=500_000, output_tokens=500_000)
    assert pytest.approx(cost_half, 1e-5) == 0.1875

    # gemini-1.5-pro: 1.25 per 1M input, 5.00 per 1M output
    cost_pro = calculate_cost("gemini-1.5-pro", input_tokens=1_000_000, output_tokens=1_000_000)
    assert pytest.approx(cost_pro, 1e-5) == 6.25

    # Zero tokens is zero cost
    assert calculate_cost("gemini-2.5-flash", input_tokens=0, output_tokens=0) == 0.0
