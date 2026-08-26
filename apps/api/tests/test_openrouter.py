from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.agents.contracts import LLMRequest
from app.agents.errors import LLMProviderError, LLMResponseError
from app.agents.openrouter import OpenRouterProvider
from app.schemas.decisions import DraftDecision


def _request() -> LLMRequest:
    return LLMRequest(
        league_id="league",
        team_id="team",
        model="openai/gpt-5.6-sol",
        decision_type="DRAFT",
        prompt_version="draft_v1",
        system_prompt="system",
        user_prompt="user",
        response_model=DraftDecision,
        reasoning_effort="low",
        temperature=0.2,
        max_tokens=100,
    )


def _success(request: httpx.Request) -> httpx.Response:
    decision = {
        "action": "draft_player",
        "player_id": "player-1",
        "public_reasoning": "Best available value.",
        "confidence": 0.8,
    }
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": "request-123"},
        json={
            "id": "generation-123",
            "choices": [{"message": {"content": json.dumps(decision)}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 3},
                "cost": 0.004,
            },
        },
    )


async def _no_sleep(delay: float) -> None:
    assert delay >= 0


async def _provider(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    retries: int = 2,
) -> tuple[OpenRouterProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    return (
        OpenRouterProvider(
            "test-key",
            client=client,
            max_retries=retries,
            requests_per_minute=1000,
            sleep=_no_sleep,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_openrouter_structured_success_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["provider"] == {
            "sort": "price",
            "max_price": {"prompt": 5.5, "completion": 33.0},
        }
        return _success(request)

    provider, client = await _provider(handler)
    try:
        result = await provider.decide(_request())
    finally:
        await client.aclose()
    assert result.parsed.player_id == "player-1"  # type: ignore[attr-defined]
    assert result.request_id == "request-123"
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (11, 7, 3)
    assert result.cost_usd == 0.004


@pytest.mark.asyncio
async def test_openrouter_retries_rate_limit_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"retry-after": "0"},
                json={"error": {"message": "slow down"}},
            )
        return _success(request)

    provider, client = await _provider(handler)
    try:
        result = await provider.decide(_request())
    finally:
        await client.aclose()
    assert result.request_id == "request-123"
    assert calls == 2


@pytest.mark.asyncio
async def test_openrouter_rejects_malformed_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )

    provider, client = await _provider(handler)
    try:
        with pytest.raises(LLMResponseError, match="Invalid structured response"):
            await provider.decide(_request())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_openrouter_retries_timeout_then_fails_clearly() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    provider, client = await _provider(handler, retries=2)
    try:
        with pytest.raises(LLMProviderError, match="ReadTimeout") as failed:
            await provider.decide(_request())
    finally:
        await client.aclose()
    assert failed.value.retryable is True
    assert calls == 3
