from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.entities import Draft, League, Matchup, Team
from app.models.enums import DraftStatus
from app.services.initialization import initialize_league


def test_initialization_is_idempotent_and_does_not_start_draft() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = initialize_league(db, nfl_season=2026)
        db.commit()
        second = initialize_league(db, nfl_season=2026)
        db.commit()

        assert first.id == second.id
        assert db.scalar(select(func.count(League.id))) == 1
        assert db.scalar(select(func.count(Team.id))) == 8
        draft = db.scalar(select(Draft).where(Draft.league_id == first.id))
        assert draft is not None
        assert draft.status == DraftStatus.NOT_STARTED.value
        assert len(draft.order) == 8
        assert db.scalar(select(func.count(Matchup.id))) == 14 * 4
