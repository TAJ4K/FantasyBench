from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.read_api import get_trade
from app.core.errors import ConflictError
from app.models.base import Base
from app.models.entities import NflGame, Player, RosterAssignment, Transaction
from app.services.initialization import initialize_league
from app.services.trades import accept_trade, counter_trade, propose_trade


@pytest.mark.parametrize("replacement_position", ["RB", "WR"])
def test_starter_trade_repairs_lineups_or_leaves_all_assets_untouched(
    db: Session, replacement_position: str
) -> None:
    league = initialize_league(db, nfl_season=2026)
    league.current_week = 1
    league.roster_config = {"starters": {"RB": 1}, "bench": 3, "ir": 1}
    first, second = league.teams[:2]
    players = [
        Player(full_name="Starter", position="RB"),
        Player(full_name="Replacement", position=replacement_position),
    ]
    db.add_all(players)
    db.flush()
    rows = [
        RosterAssignment(
            league_id=league.id,
            team_id=team.id,
            player_id=player.id,
            slot_type="STARTER" if index == 0 else "BENCH",
            position_slot="RB" if index == 0 else None,
            acquired_via="DRAFT",
        )
        for index, (team, player) in enumerate(zip((first, second), players, strict=True))
    ]
    db.add_all(rows)
    db.flush()
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=first.id,
        recipient_team_id=second.id,
        send_player_ids=[players[0].id],
        receive_player_ids=[players[1].id],
    )
    if replacement_position == "WR":
        with pytest.raises(ConflictError, match="post-trade roster"):
            accept_trade(db, offer_id=offer.id, accepting_team_id=second.id)
        assert thread.status == "PROPOSED"
        assert [row.team_id for row in rows] == [first.id, second.id]
        assert rows[0].slot_type == "STARTER"
        assert not list(db.scalars(select(Transaction)))
    else:
        accept_trade(db, offer_id=offer.id, accepting_team_id=second.id)
        assert thread.status == "PROCESSED"
        assert rows[1].team_id == first.id
        assert (rows[1].slot_type, rows[1].position_slot) == ("STARTER", "RB")
        assert rows[0].team_id == second.id
        assert len(list(db.scalars(select(Transaction)))) == 2


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
        detail = get_trade(db, thread.id)
        assert len(detail["offers"]) == 2
        assert {asset["player"]["full_name"] for asset in detail["offers"][1]["assets"]} == {
            "Player 0",
            "Player 1",
            "Player 2",
            "Player 3",
        }
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


@pytest.mark.parametrize("direction", ["recipient", "proposer", "counter"])
def test_uneven_trade_with_conditional_drops(db: Session, direction: str) -> None:
    league = initialize_league(db, nfl_season=2026)
    league.roster_config = {"starters": {"RB": 1}, "bench": 2, "ir": 1}
    first, second = league.teams[:2]
    players = [Player(full_name=f"RB {i}", position="RB", nfl_team="SEA") for i in range(6)]
    db.add_all(players)
    db.flush()
    rows = [
        RosterAssignment(
            league_id=league.id,
            team_id=first.id if i < 3 else second.id,
            player_id=p.id,
            slot_type="STARTER" if i in (2, 5) else "BENCH",
            position_slot="RB" if i in (2, 5) else None,
            acquired_via="DRAFT",
        )
        for i, p in enumerate(players)
    ]
    db.add_all(rows)
    db.flush()
    send, receive = [p.id for p in players[:2]], [players[3].id]
    proposer_drops = []
    if direction == "proposer":
        send, receive = [players[0].id], [p.id for p in players[3:5]]
        proposer_drops = [players[2].id]
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=first.id,
        recipient_team_id=second.id,
        send_player_ids=send,
        receive_player_ids=receive,
        drop_player_ids=proposer_drops,
    )
    accepting = second.id
    drops = [players[5].id] if direction == "recipient" else []
    if direction == "counter":
        thread, offer = counter_trade(
            db,
            offer_id=offer.id,
            countering_team_id=second.id,
            send_player_ids=receive,
            receive_player_ids=send,
            drop_player_ids=[players[5].id],
        )
        accepting = first.id
    # Offers do not release players, and proposer choices survive a session reload.
    db.commit()
    db.expire_all()
    assert len(list(db.scalars(select(RosterAssignment)))) == 6
    accept_trade(db, offer_id=offer.id, accepting_team_id=accepting, drop_player_ids=drops)
    db.commit()
    owners = dict(db.execute(select(RosterAssignment.player_id, RosterAssignment.team_id)).all())
    dropped = players[2] if direction == "proposer" else players[5]
    assert dropped.id not in owners
    assert len(owners) == 5
    assert thread.status == "PROCESSED"
    records = list(db.scalars(select(Transaction)))
    assert sorted(r.transaction_type for r in records) == ["DROP", "TRADE", "TRADE", "TRADE"]
    # Dropping a starter repairs the lineup using remaining or incoming players.
    starters = list(
        db.scalars(select(RosterAssignment).where(RosterAssignment.slot_type == "STARTER"))
    )
    assert len(starters) == 2
    assert {r.team_id for r in starters} == {first.id, second.id}


@pytest.mark.parametrize(
    "invalid", ["missing", "opponent", "traded", "duplicate", "ir", "locked", "lineup"]
)
def test_invalid_trade_drops_leave_rosters_untouched(db: Session, invalid: str) -> None:
    league = initialize_league(db, nfl_season=2026)
    league.current_week = 1
    league.roster_config = {"starters": {"RB": 1}, "bench": 1, "ir": 1}
    first, second = league.teams[:2]
    players = [Player(full_name=f"Player {i}", position="WR", nfl_team="SEA") for i in range(4)]
    players[3].nfl_team = "SF"
    if invalid == "lineup":
        players[3].position = "RB"
    db.add_all(players)
    db.flush()
    rows = [
        RosterAssignment(
            league_id=league.id,
            team_id=first.id if i < 2 else second.id,
            player_id=p.id,
            slot_type="STARTER" if invalid == "lineup" and i == 3 else "BENCH",
            position_slot="RB" if invalid == "lineup" and i == 3 else None,
            acquired_via="DRAFT",
        )
        for i, p in enumerate(players)
    ]
    db.add_all(rows)
    db.flush()
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=first.id,
        recipient_team_id=second.id,
        send_player_ids=[p.id for p in players[:2]],
        receive_player_ids=[players[2].id],
    )
    drops = [players[3].id]
    if invalid == "missing":
        drops = []
    if invalid == "opponent":
        drops = [players[0].id]
    if invalid == "traded":
        drops = [players[2].id]
    if invalid == "duplicate":
        drops *= 2
    if invalid == "ir":
        rows[3].slot_type = "IR"
        extra = Player(full_name="Extra", position="WR")
        db.add(extra)
        db.flush()
        db.add(
            RosterAssignment(
                league_id=league.id,
                team_id=second.id,
                player_id=extra.id,
                slot_type="BENCH",
                acquired_via="DRAFT",
            )
        )
    if invalid == "locked":
        db.add(
            NflGame(
                season=2026,
                week=1,
                provider_game_id="drop-locked",
                kickoff_at=datetime.now(UTC) - timedelta(minutes=1),
                home_team="SF",
                away_team="LAR",
            )
        )
    db.flush()
    before = [(r.player_id, r.team_id) for r in db.scalars(select(RosterAssignment))]
    with pytest.raises(ConflictError):
        accept_trade(db, offer_id=offer.id, accepting_team_id=second.id, drop_player_ids=drops)
    assert [(r.player_id, r.team_id) for r in db.scalars(select(RosterAssignment))] == before
    assert thread.status == "PROPOSED"
    assert not list(db.scalars(select(Transaction)))
