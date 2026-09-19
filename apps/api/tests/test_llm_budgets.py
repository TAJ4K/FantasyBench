from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.contracts import LLMRequest, LLMResult
from app.agents.errors import LLMBudgetExceeded, LLMResponseError
from app.agents.service import LLMInvocationService
from app.models.entities import LLMRun
from app.schemas.decisions import TradeResponseDecision
from app.services.initialization import initialize_league


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_kind", ["daily", "season"])
@pytest.mark.parametrize("actual_cost", [0, 0.1])
async def test_paid_invalid_response_releases_unused_reservation(
    db: Session, budget_kind: str, actual_cost: float,
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
    service = LLMInvocationService(
        db, provider,
        daily_budget_usd=1 if budget_kind == "daily" else None,
        season_budget_usd=1 if budget_kind == "season" else None,
    )
    request = LLMRequest(
        league_id=league.id, team_id=team.id, model=team.model_identifier,
        decision_type="TRADE_RESPONSE", prompt_version="test", system_prompt="system",
        user_prompt="user", response_model=TradeResponseDecision,
        metadata={"estimated_cost_usd": "0.8"},
    )
    with pytest.raises(LLMResponseError):
        await service.invoke(request)
    # The next $0.80 reservation fits after a known $0.00/$0.10 charge.
    assert (await service.invoke(request)).parsed == decision
    runs = list(db.scalars(select(LLMRun).order_by(LLMRun.started_at)))
    assert provider.decide.await_count == 2
    assert runs[0].success is False
    assert runs[0].estimated_cost_usd == Decimal("0.8")
    assert runs[0].cost_usd == Decimal(str(actual_cost))


@pytest.mark.asyncio
@pytest.mark.parametrize("billing", ["pending", "timeout", "missing", "null", "charged"])
async def test_budget_preserves_unsettled_reservations_and_enforces_real_charges(
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
        success=False, estimated_cost_usd=Decimal("0.8"),
        cost_usd=Decimal("0.3") if billing == "charged" else Decimal("0"),
        raw_response=None if billing in {"pending", "timeout"} else raw_response,
    ))
    db.commit()
    provider = AsyncMock()
    service = LLMInvocationService(db, provider, season_budget_usd=1)
    request = LLMRequest(
        league_id=league.id, team_id=team.id, model=team.model_identifier,
        decision_type="TRADE_RESPONSE", prompt_version="test", system_prompt="system",
        user_prompt="user", response_model=TradeResponseDecision,
        metadata={"estimated_cost_usd": "0.8"},
    )
    with pytest.raises(LLMBudgetExceeded, match="season OpenRouter budget exhausted"):
        await service.invoke(request)
    provider.decide.assert_not_awaited()
