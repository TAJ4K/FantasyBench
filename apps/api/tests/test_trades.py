from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.base import Base
from app.models.entities import NflGame, Player, RosterAssignment, Transaction
from app.services.initialization import initialize_league
from app.services.trades import accept_trade, counter_trade, propose_trade


def test_counter_and_atomic_trade_execution() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        first, second = league.teams[:2]
        players = [Player(full_name=f"Player {i}", position="RB", active=True) for i in range(4)]
        db.add_all(players)
        db.flush()
        for team, player in zip([first, first, second, second], players, strict=True):
            db.add(
                RosterAssignment(
                    league_id=league.id,
                    team_id=team.id,
                    player_id=player.id,
                    slot_type="BENCH",
                    acquired_via="DRAFT",
                )
            )
        db.flush()
        thread, offer = propose_trade(
            db,
            league_id=league.id,
            proposer_team_id=first.id,
            recipient_team_id=second.id,
            send_player_ids=[players[0].id],
            receive_player_ids=[players[2].id],
        )
        thread, counter = counter_trade(
            db,
            offer_id=offer.id,
            countering_team_id=second.id,
            send_player_ids=[players[2].id, players[3].id],
            receive_player_ids=[players[0].id, players[1].id],
        )
        accept_trade(db, offer_id=counter.id, accepting_team_id=first.id)
        owners = dict(
            db.execute(select(RosterAssignment.player_id, RosterAssignment.team_id)).all()
        )
        assert owners[players[0].id] == second.id
        assert owners[players[1].id] == second.id
        assert owners[players[2].id] == first.id
        assert owners[players[3].id] == first.id
        assert thread.status == "PROCESSED"
        assert len(list(db.scalars(select(Transaction)))) == 4


def test_trade_cannot_move_a_player_after_kickoff() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        league.current_week = 1
        first, second = league.teams[:2]
        players = [
            Player(full_name="Locked", position="RB", nfl_team="SEA"),
            Player(full_name="Return", position="RB", nfl_team="SF"),
        ]
        db.add_all(players)
        db.flush()
        for team, player in zip((first, second), players, strict=True):
            db.add(
                RosterAssignment(
                    league_id=league.id,
                    team_id=team.id,
                    player_id=player.id,
                    slot_type="BENCH",
                    acquired_via="DRAFT",
                )
            )
        db.add(
            NflGame(
                season=2026,
                week=1,
                provider_game_id="locked-game",
                kickoff_at=datetime.now(UTC) - timedelta(minutes=1),
                home_team="SEA",
                away_team="SF",
            )
        )
        db.flush()
        _, offer = propose_trade(
            db,
            league_id=league.id,
            proposer_team_id=first.id,
            recipient_team_id=second.id,
            send_player_ids=[players[0].id],
            receive_player_ids=[players[1].id],
        )
        with pytest.raises(ConflictError) as rejected:
            accept_trade(db, offer_id=offer.id, accepting_team_id=second.id)
        assert rejected.value.code == "PLAYER_LOCKED"
