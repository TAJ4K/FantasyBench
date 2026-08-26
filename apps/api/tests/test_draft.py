from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.models.base import Base
from app.models.entities import Draft, DraftPick, Player, RosterAssignment
from app.models.enums import DraftPickState, DraftStatus
from app.services.draft import DraftService, pick_coordinates, team_for_pick
from app.services.initialization import initialize_league


def _database() -> tuple[Session, str, list[Player]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    league = initialize_league(
        db,
        nfl_season=2026,
        settings={"draft_rounds": 2, "regular_season_weeks": 1},
    )
    players = [
        Player(full_name=f"Player {number}", position="RB", active=True) for number in range(20)
    ]
    db.add_all(players)
    db.commit()
    return db, league.id, players


def test_snake_math() -> None:
    order = [str(number) for number in range(1, 9)]
    assert [team_for_pick(order, number) for number in range(1, 17)] == [
        *order,
        *reversed(order),
    ]
    assert pick_coordinates(9, 8) == (2, 1)
    assert pick_coordinates(16, 8) == (2, 8)


def test_draft_requires_explicit_start_and_supports_pause_resume() -> None:
    db, league_id, players = _database()
    try:
        service = DraftService(db)
        draft = db.scalar(select(Draft).where(Draft.league_id == league_id))
        assert draft is not None and draft.status == DraftStatus.NOT_STARTED.value
        assert service.current(league_id) is None
        assert db.scalar(select(func.count(DraftPick.id))) == 0
        with pytest.raises(ConflictError):
            service.make_pick(league_id, players[0].id)

        first_turn = service.start(league_id)
        assert first_turn.pick.state == DraftPickState.WAITING_FOR_MANAGER.value
        assert service.pause(league_id).status == DraftStatus.PAUSED.value
        with pytest.raises(ConflictError):
            service.make_pick(league_id, players[0].id)
        assert service.resume(league_id).pick.id == first_turn.pick.id
    finally:
        db.close()


def test_start_requires_player_data() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026)
        with pytest.raises(DomainError, match="Player data"):
            DraftService(db).start(league.id)


def test_one_pick_progression_duplicate_protection_recovery_and_undo() -> None:
    db, league_id, players = _database()
    try:
        service = DraftService(db)
        first = service.start(league_id)
        expected_first_team = first.team.id
        pick = service.make_pick(league_id, players[0].id, reveal_delay_seconds=5)
        db.commit()
        assert pick.state == DraftPickState.REVEAL_PENDING.value
        assert db.scalar(select(func.count(RosterAssignment.id))) == 1

        # A fresh service/session view resumes from the persisted next pick.
        recovered = DraftService(db).current(league_id)
        assert recovered is not None
        assert recovered.pick.pick_number == 2
        with pytest.raises(ConflictError) as exc:
            service.make_pick(league_id, players[0].id)
        assert exc.value.code == "PLAYER_ALREADY_DRAFTED"
        db.rollback()

        undone = service.undo_last_pick(league_id)
        assert undone.pick_number == 1
        assert undone.player_id is None
        assert undone.team_id == expected_first_team
        assert db.scalar(select(func.count(RosterAssignment.id))) == 0
        replacement = service.admin_pick(league_id, players[1].id, public_reasoning="Override")
        assert replacement.pick_number == 1
        assert replacement.player_id == players[1].id
    finally:
        db.close()


def test_round_transition_and_completion() -> None:
    db, league_id, players = _database()
    try:
        service = DraftService(db)
        turn = service.start(league_id)
        order = turn.draft.order
        for index in range(16):
            pick = service.make_pick(league_id, players[index].id)
            assert pick.team_id == team_for_pick(order, index + 1)
        draft = db.scalar(select(Draft).where(Draft.league_id == league_id))
        assert draft is not None and draft.status == DraftStatus.COMPLETED.value
        picks = list(
            db.scalars(
                select(DraftPick)
                .where(DraftPick.draft_id == draft.id)
                .order_by(DraftPick.pick_number)
            )
        )
        assert [(pick.round, pick.round_pick) for pick in picks[7:9]] == [(1, 8), (2, 1)]
        assert len({pick.player_id for pick in picks}) == 16
    finally:
        db.close()
