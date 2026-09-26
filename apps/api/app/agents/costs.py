from __future__ import annotations

import json
from decimal import Decimal

from app.agents.contracts import LLMRequest

# Conservative USD prices per million tokens. These intentionally use the
# highest normal provider price shown by OpenRouter when a model has multiple
# providers (rounded upward where useful). Keep this table current when changing
# the eight configured managers.
MODEL_PRICES_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    # New manager prices from OpenRouter /api/v1/models on 2026-09-26.
    "openai/gpt-6-sol": (Decimal("2"), Decimal("10")),
    "anthropic/claude-opus-5.5": (Decimal("4"), Decimal("20")),
    "google/gemini-3.8-flash": (Decimal("0.75"), Decimal("3.75")),
    "deepseek/deepseek-v4.1-flash": (Decimal("0.3"), Decimal("1.2")),
    "x-ai/grok-4.7": (Decimal("1.6"), Decimal("4.8")),
    "openai/gpt-5.6-sol": (Decimal("5.5"), Decimal("33")),
    "anthropic/claude-opus-5": (Decimal("5.5"), Decimal("27.5")),
    "z-ai/glm-5.3": (Decimal("1.4"), Decimal("4.4")),
    "deepseek/deepseek-v4-pro": (Decimal("2"), Decimal("5")),
    "qwen/qwen3.8-max": (Decimal("2"), Decimal("6")),
    "qwen/qwen3.8-max-0902": (Decimal("2"), Decimal("6")),
    "x-ai/grok-4.6": (Decimal("2.2"), Decimal("6.6")),
    "google/gemini-3.7-flash": (Decimal("1.5"), Decimal("7.5")),
    "moonshotai/kimi-k3": (Decimal("3"), Decimal("15")),
}

# Estimates include tokenizer variance, hidden provider framing, reasoning tokens,
# and modest price drift. They are informational and never block requests.
COST_SAFETY_MULTIPLIER = Decimal("2.5")


def estimate_request_cost(request: LLMRequest) -> Decimal | None:
    prices = MODEL_PRICES_PER_MILLION.get(request.model)
    if prices is None:
        return None
    # Four characters/token is a common approximation. Add 20% for message,
    # schema, and provider framing, then assume the full output cap is consumed.
    prompt_chars = (
        len(request.system_prompt)
        + len(request.user_prompt)
        + len(json.dumps(request.messages))
        + len(json.dumps(request.tools))
    )
    input_tokens = max(1, (prompt_chars + 3) // 4)
    input_tokens = (input_tokens * 6 + 4) // 5
    output_tokens = max(1, request.max_tokens or 1200)
    input_rate, output_rate = prices
    estimated = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal("1000000")
    return estimated * COST_SAFETY_MULTIPLIER
