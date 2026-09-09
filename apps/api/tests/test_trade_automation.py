from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.contracts import LLMRequest, LLMResult
from app.agents.errors import LLMBudgetExceeded, LLMResponseError
from app.agents.fake import DeterministicFakeProvider
from app.core.config import Settings
from app.core.errors import ConflictError
from app.jobs.manager_automation import ManagerAutomation, _request
from app.models.entities import LLMRun, Player, RosterAssignment, TradeThread
from app.schemas.decisions import TradeResponseDecision
from app.services.initialization import initialize_league
from app.services.trades import propose_trade


@pytest.mark.asyncio
@pytest.mark.parametrize("corrects_decision", [True, False])
async def test_roster_limit_feedback_is_bounded_and_preserves_ownership(
    engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch, corrects_decision: bool
) -> None:
    league = initialize_league(db, nfl_season=2026)
    league.roster_config = {"starters": {"RB": 1}, "bench": 1, "ir": 1}
    first, second = league.teams[:2]
    players = [Player(full_name=f"RB {i}", position="RB") for i in range(4)]
    db.add_all(players)
    db.flush()
    rows = [
        RosterAssignment(
            league_id=league.id,
            team_id=first.id if i < 2 else second.id,
            player_id=player.id,
            slot_type="BENCH",
            acquired_via="DRAFT",
        )
        for i, player in enumerate(players)
    ]
    db.add_all(rows)
    db.flush()
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=first.id,
        recipient_team_id=second.id,
        send_player_ids=[players[0].id],
        receive_player_ids=[players[2].id, players[3].id],
    )
    db.commit()
    original_owners = [row.team_id for row in rows]
    contexts = []

    class CorrectingProvider:
        async def decide(self, request: LLMRequest) -> LLMResult:
            context = request.metadata["context"]
            contexts.append(context)
            return LLMResult(
                parsed=TradeResponseDecision(
                    action="reject"
                    if context["validation_error"] and corrects_decision
                    else "accept",
                    offer_id=offer.id,
                    message="Decision",
                    public_reasoning="Roster constraints",
                ),
                raw_response={},
            )

    automation = ManagerAutomation(
        sessionmaker(engine, expire_on_commit=False), CorrectingProvider(), Settings()
    )
    monkeypatch.setattr(automation, "_record_memory", lambda *args, **kwargs: None)
    if corrects_decision:
        assert await automation._respond_to_trade(league.id, offer.id) == "REJECT"
    else:
        with pytest.raises(ConflictError, match="roster limit"):
            await automation._respond_to_trade(league.id, offer.id)
    assert len(contexts) == 2
    assert contexts[0]["validation_error"] is None
    assert "TRADE_ROSTER_FULL" in contexts[1]["validation_error"]
    assert contexts[1]["roster_config"]["bench"] == 1
    db.expire_all()
    assert [row.team_id for row in rows] == original_owners
    assert thread.status == ("REJECTED" if corrects_decision else "PROPOSED")


@pytest.mark.asyncio
async def test_new_proposal_and_counters_are_answered_in_same_review(
    engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    league = initialize_league(db, nfl_season=2026)
    first, second = league.teams[:2]
    players = [Player(full_name=f"RB {i}", position="RB") for i in range(2)]
    db.add_all(players)
    db.flush()
    db.add_all(
        [
            RosterAssignment(
                league_id=league.id,
                team_id=team.id,
                player_id=player.id,
                slot_type="BENCH",
                acquired_via="DRAFT",
            )
            for team, player in zip((first, second), players, strict=True)
        ]
    )
    db.commit()

    class CounterProvider(DeterministicFakeProvider):
        async def decide(self, request: LLMRequest) -> LLMResult:
            context = request.metadata["context"]
            if request.decision_type != "TRADE_RESPONSE":
                return await super().decide(request)
            assert request.max_tokens == 8192
            action = "counter" if context["can_counter"] else "accept"
            decision = TradeResponseDecision(
                action=action,
                offer_id=context["offer"]["offer_id"],
                send=[{"type": "player", "id": context["my_roster"][0]["player_id"]}],
                receive=[{"type": "player", "id": context["other_roster"][0]["player_id"]}],
                message="Offer",
                public_reasoning="Fair value",
            )
            return LLMResult(parsed=decision, raw_response={})

    automation = ManagerAutomation(
        sessionmaker(engine, expire_on_commit=False), CounterProvider(), Settings()
    )
    monkeypatch.setattr(automation, "_record_memory", lambda *args, **kwargs: None)

    async def propose(league_id: str, team_id: str) -> bool:
        if team_id != first.id:
            return False
        propose_trade(
            db,
            league_id=league_id,
            proposer_team_id=first.id,
            recipient_team_id=second.id,
            send_player_ids=[players[0].id],
            receive_player_ids=[players[1].id],
        )
        db.commit()
        return True

    monkeypatch.setattr(automation, "_consider_trade_proposal", propose)
    results = await automation.review_trades(league.id)
    assert [v for k, v in results.items() if k.startswith("offer:")] == [
        "COUNTER",
        "COUNTER",
        "COUNTER",
        "ACCEPT",
    ]
    db.expire_all()
    assert db.scalar(select(TradeThread)).status == "PROCESSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 0.001])
async def test_truncated_trade_retry_is_audited_and_budget_checked(
    engine: Engine, db: Session, budget: float | None
) -> None:
    league = initialize_league(db, nfl_season=2026)
    team = league.teams[0]
    team.model_identifier = "deepseek/deepseek-v4-pro"
    db.commit()
    provider = AsyncMock()
    decision = TradeResponseDecision(
        action="reject", offer_id="offer", message="No", public_reasoning="Not beneficial"
    )
    provider.decide.side_effect = [
        LLMResponseError(
            "truncated",
            raw_response={
                "choices": [{"finish_reason": "length"}],
                "usage": {"completion_tokens": 8192, "cost": 0.001},
            },
        ),
        LLMResult(parsed=decision, raw_response={}, cost_usd=0.002),
    ]
    settings = Settings(openrouter_season_budget_usd=budget)
    automation = ManagerAutomation(sessionmaker(engine), provider, settings)
    request = _request(
        team,
        league.id,
        "TRADE_RESPONSE",
        "trade_response_v1",
        "system",
        "user",
        TradeResponseDecision,
        {},
        settings,
    )
    if budget is not None:
        # First request can run; its paid failure exhausts the remaining budget.
        from dataclasses import replace

        request = replace(request, metadata={"estimated_cost_usd": "0.001"})
        with pytest.raises(LLMBudgetExceeded):
            await automation._invoke_trade(db, request)
        assert provider.decide.await_count == 1
    else:
        result = await automation._invoke_trade(db, request)
        assert result.parsed == decision
        assert [call.args[0].max_tokens for call in provider.decide.await_args_list] == [
            8192,
            16384,
        ]
    runs = list(db.scalars(select(LLMRun).order_by(LLMRun.started_at)))
    assert len(runs) == 2
    assert runs[0].success is False
    assert float(runs[0].cost_usd) == 0.001
    assert runs[1].success is (budget is None)
