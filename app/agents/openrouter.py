from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agents.contracts import LLMRequest, LLMResult
from app.agents.costs import MODEL_PRICES_PER_MILLION
from app.agents.errors import LLMProviderError, LLMResponseError

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self.limit = max(1, requests_per_minute)
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.limit:
                    self._timestamps.append(now)
                    return
                delay = 60 - (now - self._timestamps[0])
            await asyncio.sleep(max(0.01, delay))


class OpenRouterProvider:
    """OpenRouter chat-completions client with strict Pydantic structured output."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 90,
        max_retries: int = 3,
        requests_per_minute: int = 30,
        site_url: str | None = None,
        app_name: str = "Fantasy Bench",
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        self._limiter = AsyncRateLimiter(requests_per_minute)
        headers = {"Authorization": f"Bearer {api_key}", "X-Title": app_name}
        if site_url:
            headers["HTTP-Referer"] = site_url
        self._headers = headers
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds), headers=headers
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def decide(self, request: LLMRequest) -> LLMResult:
        schema = request.response_model.model_json_schema()
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.decision_type, "strict": True, "schema": schema},
            },
        }
        if request.reasoning_effort:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        prices = MODEL_PRICES_PER_MILLION.get(request.model)
        if prices is not None:
            input_rate, output_rate = prices
            payload["provider"] = {
                "sort": "price",
                "max_price": {
                    "prompt": float(input_rate),
                    "completion": float(output_rate),
                },
            }

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.post(
                    "/chat/completions", json=payload, headers=self._headers
                )
                if response.status_code < 400:
                    break
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == self.max_retries:
                    detail = _error_detail(response)
                    raise LLMProviderError(
                        f"OpenRouter returned HTTP {response.status_code}: {detail}",
                        retryable=retryable,
                        status_code=response.status_code,
                    )
                delay = _retry_delay(response, attempt)
                logger.warning(
                    "openrouter_retry",
                    extra={"status_code": response.status_code, "attempt": attempt + 1},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise LLMProviderError(
                        f"OpenRouter request failed: {type(exc).__name__}", retryable=True
                    ) from exc
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.1)
                logger.warning(
                    "openrouter_retry",
                    extra={"error_type": type(exc).__name__, "attempt": attempt + 1},
                )
            await self._sleep(delay)

        if response is None:  # defensive; loop always assigns or raises
            raise LLMProviderError("OpenRouter request produced no response")
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if isinstance(content, str):
                decision_data = json.loads(content)
            elif isinstance(content, dict):
                decision_data = content
            else:
                raise TypeError("message content is not JSON")
            parsed = request.response_model.model_validate(decision_data)
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise LLMResponseError(f"Invalid structured response: {exc}") from exc

        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        cost = usage.get("cost", body.get("cost", 0))
        return LLMResult(
            parsed=parsed,
            raw_response=body,
            request_id=response.headers.get("x-request-id") or body.get("id"),
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            reasoning_tokens=int(
                details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
            ),
            cost_usd=float(cost or 0),
        )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("retry-after")
    if value:
        try:
            return min(60.0, max(0.0, float(value)))
        except ValueError:
            pass
    return float(min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.1))


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message", error))[:500]
        return str(error)[:500]
    except ValueError:
        return response.text[:500]
