from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.jobs.manager_automation import ManagerAutomation
from app.jobs.review_cache import ScheduledReviews
from app.models.entities import NflGame, Player, PlayerNews, PlayerWeekStat, RosterAssignment
from app.services.events import emit_event
from app.services.initialization import initialize_league
from app.services.trades import propose_trade


@pytest.fixture
def review_state(db, monkeypatch):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    clock = [now]
    monkeypatch.setattr("app.jobs.review_cache.utcnow", lambda: clock[0])
    league = initialize_league(db, nfl_season=2026)
    league.current_week = 4
    league.roster_config = {"starters": {"WR": 1}, "bench": 1, "ir": 1}
    player = Player(full_name="Starter", position="WR", nfl_team="SEA", active=True)
    teammate = Player(full_name="Quarterback", position="QB", nfl_team="SEA", active=True)
    db.add_all([player, teammate])
    db.flush()
    assignment = RosterAssignment(
        league_id=league.id, team_id=league.teams[0].id, player_id=player.id,
        slot_type="BENCH", acquired_via="DRAFT",
    )
    stat = PlayerWeekStat(
        player_id=player.id, season=2026, week=3, provider="nflverse",
        raw_stats={"receptions": 5}, source_updated_at=now,
    )
    db.add_all([assignment, stat])
    db.commit()
    return league, player, teammate, assignment, stat, clock


@pytest.mark.asyncio
async def test_lineup_reuses_post_decision_slots_across_restart_and_timestamp_refresh(
    engine, db, review_state,
):
    league, player, teammate, assignment, stat, clock = review_state
    factory = sessionmaker(engine, expire_on_commit=False)
    reviews = ScheduledReviews(factory, Settings())
    team_id = league.teams[0].id

    async def set_lineup():
        assignment.slot_type = "STARTER"
        assignment.position_slot = "WR"
        db.commit()

    first = AsyncMock(side_effect=set_lineup)
    assert await reviews.run(team_id, "LINEUP", first) == "COMPLETE"
    first.assert_awaited_once()
    stat.source_updated_at = clock[0] + timedelta(minutes=5)
    db.commit()
    repeated = AsyncMock()
    restarted = ScheduledReviews(factory, Settings())
    assert await restarted.run(team_id, "LINEUP", repeated) == "SKIPPED: unchanged review inputs"
    repeated.assert_not_awaited()
    clock[0] += timedelta(hours=24)
    assert await restarted.run(team_id, "LINEUP", repeated) == "COMPLETE"
    repeated.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "injury", "teammate_injury", "stats", "news", "new_week", "model",
    "scoring", "roster", "lineup", "kickoff", "research_policy",
])
async def test_meaningful_lineup_changes_trigger_new_review(engine, db, review_state, change):
    league, player, teammate, assignment, stat, clock = review_state
    factory = sessionmaker(engine, expire_on_commit=False)
    reviews = ScheduledReviews(factory, Settings())
    team_id = league.teams[0].id
    operation = AsyncMock(return_value=None)
    await reviews.run(team_id, "LINEUP", operation)
    if change == "injury":
        player.injury_status = "Out"
    elif change == "teammate_injury":
        teammate.injury_status = "Out"
    elif change == "stats":
        stat.raw_stats = {"receptions": 6}
    elif change == "news":
        db.add(PlayerNews(
            player_id=teammate.id, provider="test", provider_news_id="1",
            headline="New information", published_at=clock[0],
        ))
    elif change == "new_week":
        league.current_week += 1
    elif change == "model":
        league.teams[0].model_identifier = "new-model"
    elif change == "scoring":
        league.scoring_config = {"linear": {"receptions": 2}}
    elif change == "roster":
        assignment.player_id = teammate.id
    elif change == "lineup":
        assignment.slot_type = "STARTER"
        assignment.position_slot = "WR"
    elif change == "kickoff":
        db.add(NflGame(
            season=2026, week=4, provider_game_id="1", home_team="SEA", away_team="SF",
            kickoff_at=clock[0] + timedelta(hours=2),
        ))
    elif change == "research_policy":
        reviews.settings.manager_research_rounds = 2
    db.commit()
    assert await reviews.run(team_id, "LINEUP", operation) == "COMPLETE"
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_lineup_evidence_changed_during_call_is_not_marked_reviewed(engine, db, review_state):
    league, player, teammate, assignment, stat, clock = review_state
    reviews = ScheduledReviews(sessionmaker(engine, expire_on_commit=False), Settings())

    async def updated_feed():
        player.injury_status = "Out"
        db.commit()

    await reviews.run(league.teams[0].id, "LINEUP", updated_feed)
    followup = AsyncMock(return_value=None)
    assert await reviews.run(league.teams[0].id, "LINEUP", followup) == "COMPLETE"
    followup.assert_awaited_once()


@pytest.mark.asyncio
async def test_overlapping_kickoff_jobs_share_one_paid_review(engine, review_state):
    league, *rest = review_state
    reviews = ScheduledReviews(sessionmaker(engine, expire_on_commit=False), Settings())
    started = asyncio.Event()
    finish = asyncio.Event()

    async def paid_call():
        started.set()
        await finish.wait()

    first = asyncio.create_task(reviews.run(league.teams[0].id, "LINEUP", paid_call))
    await started.wait()
    other_call = AsyncMock(return_value=None)
    second = asyncio.create_task(reviews.run(league.teams[0].id, "LINEUP", other_call))
    finish.set()
    assert await first == "COMPLETE"
    assert await second == "SKIPPED: unchanged review inputs"
    other_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_decision_is_not_cached(engine, review_state):
    league, *rest = review_state
    reviews = ScheduledReviews(sessionmaker(engine, expire_on_commit=False), Settings())
    failed = AsyncMock(side_effect=RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        await reviews.run(league.teams[0].id, "LINEUP", failed)
    successful = AsyncMock(return_value=None)
    assert await reviews.run(league.teams[0].id, "LINEUP", successful) == "COMPLETE"
    successful.assert_awaited_once()


@pytest.mark.asyncio
async def test_trade_cooldown_unchanged_state_and_new_week(engine, db, review_state):
    league, player, teammate, assignment, stat, clock = review_state
    reviews = ScheduledReviews(sessionmaker(engine, expire_on_commit=False), Settings())
    operation = AsyncMock(return_value=False)
    team_id = league.teams[0].id
    assert await reviews.run(team_id, "TRADE_PROPOSAL", operation) == "PASS"
    player.injury_status = "Questionable"
    db.commit()
    clock[0] += timedelta(hours=24)
    assert await reviews.run(team_id, "TRADE_PROPOSAL", operation) == (
        "SKIPPED: trade proposal cooldown"
    )
    clock[0] += timedelta(hours=48)
    assert await reviews.run(team_id, "TRADE_PROPOSAL", operation) == "PASS"
    clock[0] += timedelta(hours=72)
    assert await reviews.run(team_id, "TRADE_PROPOSAL", operation) == (
        "SKIPPED: unchanged review inputs"
    )
    league.current_week += 1
    db.commit()
    assert await reviews.run(team_id, "TRADE_PROPOSAL", operation) == "PASS"
    assert operation.await_count == 3


@pytest.mark.asyncio
async def test_pending_offer_response_is_not_blocked_by_proposal_cooldown(
    engine, db, review_state, monkeypatch,
):
    league, player, teammate, assignment, stat, clock = review_state
    first, second = league.teams[:2]
    db.add(RosterAssignment(
        league_id=league.id, team_id=second.id, player_id=teammate.id,
        slot_type="BENCH", acquired_via="DRAFT",
    ))
    db.commit()
    factory = sessionmaker(engine, expire_on_commit=False)
    automation = ManagerAutomation(factory, AsyncMock(), Settings())
    await automation.scheduled_reviews.run(
        second.id, "TRADE_PROPOSAL", AsyncMock(return_value=False),
    )
    thread, offer = propose_trade(
        db, league_id=league.id, proposer_team_id=first.id, recipient_team_id=second.id,
        send_player_ids=[player.id], receive_player_ids=[teammate.id],
    )
    db.commit()
    response = AsyncMock(return_value="REJECT")
    proposal = AsyncMock(return_value=False)
    monkeypatch.setattr(automation, "_respond_to_trade", response)
    monkeypatch.setattr(automation, "_consider_trade_proposal", proposal)

    # Match the scheduler's progress contract; all proposals are already checkpointed.
    class Progress:
        results = {f"team:{team.id}": "PASS" for team in league.teams}

        def complete(self, key):
            return key in self.results

        def record(self, key, value):
            self.results[key] = value

    result = await automation.review_trades(league.id, progress=Progress())
    assert result[f"offer:{offer.id}"] == "REJECT"
    response.assert_awaited_once_with(league.id, offer.id)
    proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_reviews_bypass_scheduled_reuse(engine, review_state, monkeypatch):
    league, *rest = review_state
    automation = ManagerAutomation(
        sessionmaker(engine, expire_on_commit=False), AsyncMock(), Settings(),
    )
    guarded = AsyncMock(side_effect=AssertionError("manual calls must bypass reuse"))
    lineup = AsyncMock()
    proposal = AsyncMock(return_value=False)
    monkeypatch.setattr(automation.scheduled_reviews, "run", guarded)
    monkeypatch.setattr(automation, "_set_team_lineup", lineup)
    monkeypatch.setattr(automation, "_consider_trade_proposal", proposal)
    await automation.set_all_lineups(league.id, 4, admin_message="Review now")
    await automation.review_trades(league.id)
    assert lineup.await_count == 8
    assert proposal.await_count == 8
    guarded.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_week", [False, True])
async def test_trade_cooldown_uses_preupgrade_reviews_only_in_current_week(
    engine, db, review_state, previous_week,
):
    league, player, teammate, assignment, stat, clock = review_state
    team_id = league.teams[0].id
    week_start = emit_event(db, league.id, "WEEK_STARTED", data={"week": 4})
    week_start.occurred_at = clock[0] - timedelta(hours=24)
    previous = emit_event(db, league.id, "TRADE_REVIEWED", team_id=team_id)
    previous.occurred_at = clock[0] - timedelta(hours=36 if previous_week else 12)
    db.commit()
    reviews = ScheduledReviews(sessionmaker(engine, expire_on_commit=False), Settings())
    operation = AsyncMock(return_value=False)
    result = await reviews.run(team_id, "TRADE_PROPOSAL", operation)
    if previous_week:
        assert result == "PASS"
        operation.assert_awaited_once()
    else:
        assert result == "SKIPPED: trade proposal cooldown"
        operation.assert_not_awaited()
