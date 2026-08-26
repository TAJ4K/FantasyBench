from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.models.base import Base
from app.models.entities import NflGame, Player, RosterAssignment, Team
from app.services.initialization import initialize_league
from app.services.rosters import RosterService


def _roster_database() -> tuple[Session, Team, dict[str, Player]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
    league.current_week = 1
    team = league.teams[0]
    positions = ["QB", "RB", "RB", "WR", "WR", "TE", "TE", "DST", "K"]
    players = {
        f"p{index}": Player(
            full_name=f"Player {index}", position=position, nfl_team="SEA", active=True
        )
        for index, position in enumerate(positions)
    }
    db.add_all(players.values())
    db.flush()
    for player in players.values():
        db.add(
            RosterAssignment(
                league_id=league.id,
                team_id=team.id,
                player_id=player.id,
                slot_type="BENCH",
                acquired_via="COMMISSIONER_ADD",
            )
        )
    db.commit()
    return db, team, players


def _valid_lineup(players: dict[str, Player]) -> dict[str, str]:
    return {
        "QB": players["p0"].id,
        "RB1": players["p1"].id,
        "RB2": players["p2"].id,
        "WR1": players["p3"].id,
        "WR2": players["p4"].id,
        "TE": players["p5"].id,
        "FLEX": players["p6"].id,
        "DST": players["p7"].id,
        "K": players["p8"].id,
    }


def test_valid_lineup_and_flex_eligibility() -> None:
    db, team, players = _roster_database()
    try:
        service = RosterService(db)
        service.set_lineup(team.id, _valid_lineup(players))
        illegal = _valid_lineup(players)
        illegal["FLEX"] = players["p0"].id
        illegal["QB"] = players["p6"].id
        with pytest.raises(DomainError) as exc:
            service.validate_lineup(team.id, illegal)
        assert exc.value.code == "INELIGIBLE_LINEUP_SLOT"
    finally:
        db.close()


def test_duplicate_ownership_and_ir_rules() -> None:
    db, team, players = _roster_database()
    try:
        service = RosterService(db)
        other = db.query(Team).filter(Team.league_id == team.league_id, Team.id != team.id).first()
        assert other is not None
        with pytest.raises(ConflictError) as exc:
            service.add_player(other.id, players["p0"].id, acquired_via="COMMISSIONER_ADD")
        assert exc.value.code == "PLAYER_ALREADY_OWNED"

        healthy = Player(full_name="Healthy", position="RB", active=True)
        injured = Player(full_name="Injured", position="RB", active=True, injury_status="IR")
        db.add_all([healthy, injured])
        db.flush()
        with pytest.raises(DomainError):
            service.add_player(team.id, healthy.id, acquired_via="COMMISSIONER_ADD", slot_type="IR")
        service.add_player(team.id, injured.id, acquired_via="COMMISSIONER_ADD", slot_type="IR")
    finally:
        db.close()


def test_started_player_cannot_move_or_drop() -> None:
    db, team, players = _roster_database()
    try:
        service = RosterService(db)
        lineup = _valid_lineup(players)
        before = datetime.now(UTC)
        service.set_lineup(team.id, lineup, now=before)
        db.add(
            NflGame(
                season=2026,
                week=1,
                provider_game_id="game-1",
                kickoff_at=before + timedelta(minutes=1),
                home_team="SEA",
                away_team="SF",
            )
        )
        db.flush()
        after = before + timedelta(minutes=2)
        swapped = dict(lineup)
        swapped["RB1"], swapped["RB2"] = swapped["RB2"], swapped["RB1"]
        with pytest.raises(ConflictError) as exc:
            service.set_lineup(team.id, swapped, now=after)
        assert exc.value.code == "PLAYER_LOCKED"
        with pytest.raises(ConflictError):
            service.drop_player(team.id, players["p1"].id, now=after)
    finally:
        db.close()
