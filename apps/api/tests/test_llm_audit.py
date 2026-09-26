from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.contracts import LLMRequest, LLMResult
from app.agents.errors import LLMProviderError, LLMResponseError
from app.agents.service import LLMInvocationService
from app.core.config import Settings
from app.models.entities import LLMRun
from app.schemas.decisions import TradeResponseDecision
from app.services.initialization import initialize_league


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_cost", [0, 0.1])
async def test_paid_invalid_response_retains_cost_audit(
    db: Session, actual_cost: float,
) -> None:
    league = initialize_league(db, nfl_season=2026)
    team = league.teams[0]
    decision = TradeResponseDecision(
        action="reject", offer_id="offer", message="No", public_reasoning="Not beneficial",
    )
    provider = AsyncMock()
    provider.decide.side_effect = [
        LLMResponseError("invalid JSON", raw_response={"usage": {"cost": actual_cost}}),
        LLMResult(parsed=decision, raw_response={}, cost_usd=0.1),
    ]
    service = LLMInvocationService(db, provider)
    request = LLMRequest(
        league_id=league.id, team_id=team.id, model=team.model_identifier,
        decision_type="TRADE_RESPONSE", prompt_version="test", system_prompt="system",
        user_prompt="user", response_model=TradeResponseDecision,
        metadata={"estimated_cost_usd": "0.8"},
    )
    with pytest.raises(LLMResponseError):
        await service.invoke(request)
    assert (await service.invoke(request)).parsed == decision
    runs = list(db.scalars(select(LLMRun).order_by(LLMRun.started_at)))
    assert provider.decide.await_count == 2
    assert runs[0].success is False
    assert runs[0].estimated_cost_usd == Decimal("0.8")
    assert runs[0].cost_usd == Decimal(str(actual_cost))


@pytest.mark.asyncio
@pytest.mark.parametrize("billing", ["pending", "timeout", "missing", "null", "charged"])
async def test_historical_spending_never_blocks_provider_calls(
    db: Session, billing: str,
) -> None:
    league = initialize_league(db, nfl_season=2026)
    team = league.teams[0]
    now = datetime.now(UTC)
    raw_response = {"usage": {"cost": None}} if billing == "null" else {}
    if billing == "charged":
        raw_response = {"usage": {"cost": 0.3}}
    db.add(LLMRun(
        league_id=league.id, team_id=team.id, model=team.model_identifier,
        decision_type="TRADE_RESPONSE", prompt_version="test", started_at=now,
        completed_at=None if billing == "pending" else now,
        success=False, estimated_cost_usd=Decimal("1000"),
        cost_usd=Decimal("1000") if billing == "charged" else Decimal("0"),
        raw_response=None if billing in {"pending", "timeout"} else raw_response,
    ))
    db.commit()
    provider = AsyncMock()
    decision = TradeResponseDecision(
        action="reject", offer_id="offer", message="No", public_reasoning="Not beneficial",
    )
    provider.decide.return_value = LLMResult(parsed=decision, raw_response={}, cost_usd=20)
    service = LLMInvocationService(db, provider)
    request = LLMRequest(
        league_id=league.id, team_id=team.id, model=team.model_identifier,
        decision_type="TRADE_RESPONSE", prompt_version="test", system_prompt="system",
        user_prompt="user", response_model=TradeResponseDecision,
        metadata={"estimated_cost_usd": "0.8"},
    )
    assert (await service.invoke(request)).parsed == decision
    provider.decide.assert_awaited_once()
    run = db.scalars(select(LLMRun).order_by(LLMRun.started_at.desc())).first()
    assert run.success is True
    assert run.cost_usd == Decimal("20")


@pytest.mark.asyncio
async def test_provider_credit_rejection_is_audited(db: Session) -> None:
    league = initialize_league(db, nfl_season=2026)
    team = league.teams[0]
    provider = AsyncMock()
    provider.decide.side_effect = LLMProviderError("Insufficient credits", status_code=402)
    request = LLMRequest(
        league_id=league.id, team_id=team.id, model="unknown/model",
        decision_type="TRADE_RESPONSE", prompt_version="test", system_prompt="system",
        user_prompt="user", response_model=TradeResponseDecision,
    )
    with pytest.raises(LLMProviderError, match="Insufficient credits"):
        await LLMInvocationService(db, provider).invoke(request)
    provider.decide.assert_awaited_once()
    run = db.scalar(select(LLMRun))
    assert run.success is False
    assert run.completed_at is not None
    assert "Insufficient credits" in run.error


@pytest.mark.parametrize("legacy_caps", [False, True])
def test_production_relies_on_provider_limits(monkeypatch, legacy_caps: bool) -> None:
    for name in (
        "OPENROUTER_DAILY_BUDGET_USD", "OPENROUTER_SEASON_BUDGET_USD",
        "OPENROUTER_MAX_SINGLE_REQUEST_USD", "OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED",
    ):
        if legacy_caps:
            monkeypatch.setenv(name, "0")
        else:
            monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None, app_env="production", admin_api_key="a" * 32,
        llm_provider="openrouter", openrouter_api_key="test-key",
        database_url="postgresql+psycopg://user:secret@localhost/league",
    )
    assert settings.llm_provider == "openrouter"
    assert "openrouter_season_budget_usd" not in settings.model_dump()
