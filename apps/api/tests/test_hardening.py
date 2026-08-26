from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.memory import ManagerMemoryService
from app.api.draft_api import get_available_players
from app.api.read_api import (
    get_team_decisions,
    get_team_roster,
    get_team_transactions,
    get_team_waivers,
    list_llm_runs,
    list_players,
    manager_memory,
)
from app.core.config import Settings
from app.core.errors import ConflictError
from app.jobs.draft_runner import DraftRunner
from app.jobs.scheduler import LeagueScheduler
from app.models.base import Base
from app.models.entities import (
    Draft,
    JobRun,
    LineupDecision,
    LLMRun,
    NflGame,
    Player,
    RosterAssignment,
    WaiverPeriod,
)
from app.services.draft import DraftService
from app.services.initialization import initialize_league
from app.services.rosters import RosterService
from app.services.transactions import add_free_agent
from app.services.waivers import process_waivers, submit_claims


def _engine() -> object:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def test_pending_draft_identity_is_hidden_from_every_public_projection() -> None:
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        league = initialize_league(
            db, nfl_season=2026, settings={"draft_rounds": 1, "regular_season_weeks": 1}
        )
        player = Player(full_name="Private Pick", position="RB", active=True)
        db.add(player)
        db.commit()
        turn = DraftService(db).start(league.id)
        run = LLMRun(
            league_id=league.id,
            team_id=turn.team.id,
            model=turn.team.model_identifier,
            decision_type="DRAFT",
            prompt_version="draft_test",
            parsed_response={"public_reasoning": "I selected Private Pick."},
            success=True,
        )
        db.add(run)
        db.flush()
        pick = DraftService(db).make_pick(
            league.id,
            player.id,
            llm_run_id=run.id,
            reveal_delay_seconds=60,
        )
        db.commit()
        ManagerMemoryService(db).record_decision(
            league.id,
            turn.team.id,
            f"Drafted {player.id}",
            valued_player_ids=[player.id],
            last_llm_run_id=run.id,
        )

        assert get_team_roster(db, turn.team.id) == []
        assert get_team_transactions(db, turn.team.id, limit=50) == []
        available = get_available_players(db, league.id, None, 100, 0)
        assert player.id in {item["id"] for item in available["items"]}
        player_page = list_players(db, league.id, available=True, limit=100, offset=0)
        selected = next(item for item in player_page["items"] if item["id"] == player.id)
        assert selected["owned_by"] is None
        owned_page = list_players(db, league.id, owned_by=turn.team.id, limit=100, offset=0)
        assert player.id not in {item["id"] for item in owned_page["items"]}
        assert get_team_decisions(db, turn.team.id, limit=50, offset=0) == []
        assert list_llm_runs(db, league.id, limit=50, offset=0)["items"] == []
        assert manager_memory(db, turn.team.id)["withheld"] is True

        DraftService(db).reveal_pick(pick.id, force=True)
        db.commit()
        assert get_team_roster(db, turn.team.id)[0]["player_id"] == player.id
        assert get_team_transactions(db, turn.team.id, limit=50)[0]["player_id"] == player.id
        assert get_team_decisions(db, turn.team.id, limit=50, offset=0)[0]["id"] == run.id
        assert manager_memory(db, turn.team.id)["summary"]["valued_player_ids"] == [player.id]


def test_pending_waiver_bids_are_private_until_processing() -> None:
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        team = league.teams[0]
        player = Player(full_name="Waiver Secret", position="WR", active=True)
        period = WaiverPeriod(
            league_id=league.id,
            season=2026,
            week=1,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all([player, period])
        db.flush()
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=team.id,
            claims=[{"add_player_id": player.id, "bid": 37, "priority": 1}],
        )
        run = LLMRun(
            league_id=league.id,
            team_id=team.id,
            model=team.model_identifier,
            decision_type="WAIVER",
            prompt_version="waiver_test",
            request_payload={"metadata": {"context": {"waiver_period_id": period.id, "week": 1}}},
            parsed_response={"public_reasoning": "Bid $37 on Waiver Secret."},
            success=True,
        )
        db.add(run)
        db.commit()
        ManagerMemoryService(db).record_decision(
            league.id,
            team.id,
            f"Bid on {player.id}",
            valued_player_ids=[player.id],
            last_llm_run_id=run.id,
        )
        assert get_team_waivers(db, team.id) == []
        assert get_team_decisions(db, team.id, limit=50, offset=0) == []
        assert manager_memory(db, team.id)["withheld"] is True
        process_waivers(db, waiver_period_id=period.id, idempotency_key="privacy")
        db.commit()
        assert get_team_waivers(db, team.id)[0]["bid"] == 37
        assert get_team_decisions(db, team.id, limit=50, offset=0)[0]["id"] == run.id


def test_add_drop_preserves_a_vacated_starter_slot_and_rejects_ineligible_replacement() -> None:
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        team = league.teams[0]
        starter = Player(full_name="Starter", position="WR", active=True)
        replacement = Player(full_name="Replacement", position="WR", active=True)
        bad_replacement = Player(full_name="Quarterback", position="QB", active=True)
        db.add_all([starter, replacement, bad_replacement])
        db.flush()
        db.add(
            RosterAssignment(
                league_id=league.id,
                team_id=team.id,
                player_id=starter.id,
                slot_type="STARTER",
                position_slot="WR1",
                acquired_via="COMMISSIONER_ADD",
            )
        )
        db.commit()

        assignment, _ = add_free_agent(
            db,
            league_id=league.id,
            team_id=team.id,
            add_player_id=replacement.id,
            drop_player_id=starter.id,
            idempotency_key="starter-swap",
            week=1,
        )
        assert (assignment.slot_type, assignment.position_slot) == ("STARTER", "WR1")
        decision = db.scalar(
            select(LineupDecision)
            .where(LineupDecision.team_id == team.id)
            .order_by(LineupDecision.created_at.desc())
        )
        assert decision is not None and decision.lineup["WR1"] == replacement.id
        db.rollback()
        with pytest.raises(ConflictError) as exc:
            add_free_agent(
                db,
                league_id=league.id,
                team_id=team.id,
                add_player_id=bad_replacement.id,
                drop_player_id=starter.id,
                idempotency_key="bad-starter-swap",
            )
        assert exc.value.code == "STARTER_REPLACEMENT_INELIGIBLE"
        db.rollback()
        with pytest.raises(ConflictError) as exc:
            RosterService(db).drop_player(team.id, starter.id)
        assert exc.value.code == "STARTER_REPLACEMENT_REQUIRED"


def test_expired_draft_lease_can_be_reclaimed_but_stale_runner_cannot_pause_it() -> None:
    engine = _engine()
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        db.add(Player(full_name="Draftable", position="RB", active=True))
        db.flush()
        DraftService(db).start(league.id)
        db.commit()
        league_id = league.id

    settings = Settings(
        app_env="test",
        llm_provider="fake",
        draft_runner_lease_seconds=11,
        draft_runner_heartbeat_seconds=1,
    )
    provider = SimpleNamespace(decide=None)
    first = DraftRunner(factory, provider, settings)  # type: ignore[arg-type]
    second = DraftRunner(factory, provider, settings)  # type: ignore[arg-type]
    assert first._claim_lease(league_id)
    with factory() as db:
        draft = db.scalar(select(Draft).where(Draft.league_id == league_id))
        assert draft is not None
        draft.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert second._claim_lease(league_id)
    with factory() as db:
        with pytest.raises(ConflictError) as exc:
            DraftService(db).mark_failed(
                league_id, "stale failure", expected_runner_id=first.runner_id
            )
        assert exc.value.code == "DRAFT_LEASE_LOST"
        db.rollback()
        draft = db.scalar(select(Draft).where(Draft.league_id == league_id))
        assert (
            draft is not None and draft.runner_id == second.runner_id and draft.status == "ACTIVE"
        )


@pytest.mark.asyncio
async def test_jobs_reclaim_expired_attempts_and_lineup_keys_are_per_kickoff() -> None:
    engine = _engine()
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        llm_provider="fake",
        lineup_review_hours_before_kickoff="6",
        job_lease_seconds=30,
        job_retry_base_seconds=1,
    )

    async def complete(*args: object) -> dict[str, str]:
        return {}

    manager = SimpleNamespace(
        settings=settings,
        set_all_lineups=complete,
        collect_waiver_claims=complete,
        review_free_agents=complete,
        review_trades=complete,
    )
    scheduler = LeagueScheduler(factory, SimpleNamespace(start=lambda _: None), manager, 60)  # type: ignore[arg-type]
    scheduler._sync_schedule = complete  # type: ignore[method-assign]
    scheduler._sync_injuries = complete  # type: ignore[method-assign]
    scheduler._sync_stats_and_score = complete  # type: ignore[method-assign]
    now = datetime.now(UTC)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        league.status = "REGULAR_SEASON"
        league.current_week = 1
        db.add_all(
            [
                NflGame(
                    season=2026,
                    week=1,
                    provider_game_id="early",
                    kickoff_at=now + timedelta(hours=2),
                    home_team="SEA",
                    away_team="SF",
                ),
                NflGame(
                    season=2026,
                    week=1,
                    provider_game_id="late",
                    kickoff_at=now + timedelta(hours=4),
                    home_team="DAL",
                    away_team="NYG",
                ),
            ]
        )
        db.commit()

        claimed = scheduler._claim_job(
            db,
            "recovery",
            "one",
            kind="trade",
            target_id=league.id,
            week=None,
            now=now,
        )
        assert claimed is not None and claimed.attempt_count == 1
        claimed.lease_expires_at = now - timedelta(seconds=1)
        db.commit()
        reclaimed = scheduler._claim_job(
            db,
            "recovery",
            "one",
            kind="trade",
            target_id=league.id,
            week=None,
            now=now,
        )
        assert reclaimed is not None and reclaimed.attempt_count == 2
        db.commit()

    scheduler.tick()
    await asyncio.sleep(0)
    with factory() as db:
        lineup_jobs = list(db.scalars(select(JobRun).where(JobRun.job_name == "lineup_review")))
        assert len(lineup_jobs) == 2
        assert len({job.idempotency_key for job in lineup_jobs}) == 2
    await scheduler.stop()
