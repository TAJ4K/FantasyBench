from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.base import Base
from app.models.entities import Player, RosterAssignment, Transaction, WaiverPeriod
from app.services.initialization import initialize_league
from app.services.waivers import process_waivers, submit_claims


def test_faab_tie_uses_rolling_priority_and_is_idempotent() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        first, second = league.teams[:2]
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
            team_id=first.id,
            claims=[{"add_player_id": player.id, "bid": 10, "priority": 1}],
        )
        submit_claims(
            db,
            waiver_period_id=period.id,
            team_id=second.id,
            claims=[{"add_player_id": player.id, "bid": 10, "priority": 1}],
        )
        results = process_waivers(db, waiver_period_id=period.id, idempotency_key="period-1")
        by_team = {claim.team_id: claim for claim in results}
        assert by_team[first.id].status == "WON"
        assert by_team[second.id].status == "LOST"
        assert first.faab_budget == 90
        assert first.waiver_priority > second.waiver_priority
        owner_id = db.scalar(
            select(RosterAssignment.team_id).where(RosterAssignment.player_id == player.id)
        )
        assert owner_id == first.id
        assert len(list(db.scalars(select(Transaction)))) == 1


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
        claim = {"add_player_id": player.id, "bid": 4, "priority": 1}
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
