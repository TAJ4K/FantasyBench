from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Fantasy Bench"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./fantasy_bench.db"
    admin_api_key: str = "change-me"
    cors_origins: str = ""

    llm_provider: Literal["fake", "openrouter"] = "fake"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: float = 90.0
    openrouter_max_retries: int = 3
    openrouter_temperature: float = 0.2
    openrouter_max_tokens: int = 1200
    openrouter_requests_per_minute: int = 30
    openrouter_daily_budget_usd: float | None = None
    openrouter_season_budget_usd: float | None = None
    openrouter_max_single_request_usd: float | None = None
    openrouter_provider_spend_limit_confirmed: bool = False
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Fantasy Bench"

    draft_reveal_min_delay_seconds: float = Field(default=3.0, ge=0)
    draft_reveal_max_delay_seconds: float = Field(default=8.0, ge=0)
    auto_resume_draft: bool = True
    draft_runner_lease_seconds: float = Field(default=180.0, gt=10)
    draft_runner_heartbeat_seconds: float = Field(default=15.0, gt=0)
    max_trade_negotiation_rounds: int = 4
    scheduler_poll_seconds: float = 5.0
    job_lease_seconds: float = Field(default=600.0, gt=10)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_retry_base_seconds: float = Field(default=30.0, gt=0)
    lineup_review_hours_before_kickoff: str = "48,3"
    waiver_collection_hours_before_deadline: float = 2.0
    waiver_period_hours: float = Field(default=48.0, gt=0)
    waiver_processing_grace_minutes: float = Field(default=30.0, ge=5)
    trade_review_interval_hours: float = Field(default=24.0, gt=0)

    @model_validator(mode="after")
    def reject_unsafe_production_configuration(self) -> Settings:
        if self.draft_runner_heartbeat_seconds >= self.draft_runner_lease_seconds:
            raise ValueError("DRAFT_RUNNER_HEARTBEAT_SECONDS must be shorter than the lease")
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if self.admin_api_key == "change-me" or len(self.admin_api_key) < 32:
            problems.append("ADMIN_API_KEY must be a non-default secret of at least 32 characters")
        if self.llm_provider != "openrouter":
            problems.append("LLM_PROVIDER must be openrouter")
        if not self.openrouter_api_key:
            problems.append("OPENROUTER_API_KEY is required")
        if self.openrouter_daily_budget_usd is None or self.openrouter_daily_budget_usd <= 0:
            problems.append("OPENROUTER_DAILY_BUDGET_USD must be a positive hard cap")
        if self.openrouter_season_budget_usd is None or self.openrouter_season_budget_usd <= 0:
            problems.append("OPENROUTER_SEASON_BUDGET_USD must be a positive hard cap")
        if (
            self.openrouter_max_single_request_usd is None
            or self.openrouter_max_single_request_usd <= 0
        ):
            problems.append("OPENROUTER_MAX_SINGLE_REQUEST_USD must be a positive hard cap")
        if not self.openrouter_provider_spend_limit_confirmed:
            problems.append(
                "OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED must attest to an external key limit"
            )
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            problems.append("DATABASE_URL must use PostgreSQL")
        if "change-me" in self.database_url.lower():
            problems.append("DATABASE_URL contains the default password")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def lineup_review_windows(self) -> list[float]:
        return sorted(
            {
                float(item.strip())
                for item in self.lineup_review_hours_before_kickoff.split(",")
                if item.strip()
            },
            reverse=True,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
