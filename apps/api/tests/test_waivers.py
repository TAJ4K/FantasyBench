from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.base import Base
from app.models.entities import Player, RosterAssignment, Transaction, WaiverPeriod
from app.services.initialization import initialize_league
from app.services.transactions import add_free_agent
from app.services.waivers import process_waivers, submit_claims


def test_continual_rolling_priority_ignores_standings_and_is_idempotent() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        ordered = sorted(league.teams, key=lambda team: team.draft_position)
        first_priority = ordered[-1]
        second_priority = ordered[-2]
        first_priority.wins = 4
        first_priority.points_for = 500
        second_priority.losses = 4
        second_priority.points_for = 300
        player = Player(full_name="Waiver Target", position="RB", active=True)
        db.add(player)
        period = WaiverPeriod(
            league_id=league.id,
            season=2026,
            week=1,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(period)
        db.flush()
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=first_priority.id,
            claims=[{"add_player_id": player.id, "priority": 1}],
        )
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=second_priority.id,
            claims=[{"add_player_id": player.id, "priority": 1}],
        )
        results = process_waivers(db, waiver_period_id=period.id, idempotency_key="period-1")
        by_team = {claim.team_id: claim for claim in results}
        assert by_team[first_priority.id].status == "WON"
        assert by_team[second_priority.id].status == "LOST"
        assert first_priority.waiver_priority == 8
        assert second_priority.waiver_priority == 1
        owner_id = db.scalar(
            select(RosterAssignment.team_id).where(RosterAssignment.player_id == player.id)
        )
        assert owner_id == first_priority.id
        assert len(list(db.scalars(select(Transaction)))) == 1
        retried = process_waivers(db, waiver_period_id=period.id, idempotency_key="period-1")
        assert {claim.id for claim in retried} == {claim.id for claim in results}
        assert len(list(db.scalars(select(Transaction)))) == 1


def test_rolling_winner_moves_to_bottom_before_the_next_claim() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        ordered = sorted(league.teams, key=lambda team: team.waiver_priority)
        first_priority, second_priority = ordered[:2]
        first_target = Player(full_name="First Target", position="RB", active=True)
        contested_target = Player(full_name="Contested Target", position="WR", active=True)
        period = WaiverPeriod(
            league_id=league.id,
            season=2026,
            week=1,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all([first_target, contested_target, period])
        db.flush()
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=first_priority.id,
            claims=[
                {"add_player_id": first_target.id, "priority": 1},
                {"add_player_id": contested_target.id, "priority": 2},
            ],
        )
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=second_priority.id,
            claims=[{"add_player_id": contested_target.id, "priority": 1}],
        )

        claims = process_waivers(db, waiver_period_id=period.id, idempotency_key="rolling-order")
        outcomes = {(claim.team_id, claim.add_player_id): claim.status for claim in claims}
        assert outcomes[(first_priority.id, first_target.id)] == "WON"
        assert outcomes[(first_priority.id, contested_target.id)] == "LOST"
        assert outcomes[(second_priority.id, contested_target.id)] == "WON"
        assert second_priority.waiver_priority == 8
        assert first_priority.waiver_priority == 7


def test_only_a_manager_request_started_before_cutoff_may_finish_during_grace() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        team = league.teams[0]
        player = Player(full_name="Grace Target", position="WR", active=True)
        deadline = datetime.now(UTC) - timedelta(seconds=1)
        period = WaiverPeriod(
            league_id=league.id,
            season=2026,
            week=1,
            deadline_at=deadline,
            processing_at=deadline + timedelta(minutes=30),
        )
        db.add_all([player, period])
        db.flush()
        claim = {"add_player_id": player.id, "priority": 1}
        with pytest.raises(ConflictError) as exc:
            submit_claims(db, waiver_period_id=period.id, team_id=team.id, claims=[claim])
        assert exc.value.code == "WAIVER_DEADLINE_PASSED"
        claims = submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=team.id,
            claims=[claim],
            collection_started_at=deadline - timedelta(seconds=1),
        )
        assert len(claims) == 1
        process_waivers(db, waiver_period_id=period.id, idempotency_key="period-1")
        assert len(list(db.scalars(select(Transaction)))) == 1


def test_free_agents_cannot_bypass_an_open_waiver_run() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        team = league.teams[0]
        league.current_week = 1
        player = Player(full_name="Waiver Only", position="RB", active=True)
        period = WaiverPeriod(
            league_id=league.id,
            season=2026,
            week=1,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all([player, period])
        db.flush()
        with pytest.raises(ConflictError) as exc:
            add_free_agent(
                db,
                league_id=league.id,
                team_id=team.id,
                add_player_id=player.id,
                idempotency_key="waiver-blocked-add",
            )
        assert exc.value.code == "PLAYER_ON_WAIVERS"

        period.status = "PROCESSED"
        assignment, _ = add_free_agent(
            db,
            league_id=league.id,
            team_id=team.id,
            add_player_id=player.id,
            idempotency_key="waiver-cleared-add",
        )
        assert assignment.player_id == player.id
