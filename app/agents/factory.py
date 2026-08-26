from __future__ import annotations

from app.agents.contracts import LLMProvider
from app.agents.fake import DeterministicFakeProvider
from app.agents.openrouter import OpenRouterProvider
from app.core.config import Settings


def create_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return DeterministicFakeProvider()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter")
    return OpenRouterProvider(
        settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.openrouter_timeout_seconds,
        max_retries=settings.openrouter_max_retries,
        requests_per_minute=settings.openrouter_requests_per_minute,
        site_url=settings.openrouter_site_url,
        app_name=settings.openrouter_app_name,
    )
