"""LLM token pricing tables and cost calculation."""

# Token pricing in USD per 1,000,000 tokens
PRICING: dict[str, dict[str, float]] = {
    # Gemini models
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    # Claude models (for reference / M07feat02)
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    # DeepSeek
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    # Grok
    "grok-beta": {"input": 5.00, "output": 15.00},
}

DEFAULT_PRICE = {"input": 0.10, "output": 0.40}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculates estimated cost in USD given model name and token counts."""
    price = PRICING.get(model)
    if not price:
        for prefix, p in PRICING.items():
            if model.startswith(prefix):
                price = p
                break
    if not price:
        price = DEFAULT_PRICE

    cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000.0
    return round(cost, 6)
