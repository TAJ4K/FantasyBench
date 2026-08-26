from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.entities import League


def ensure_league_unlocked(db: Session, league_id: str) -> League:
    """Lock and validate the league before a state-changing operation."""
    league = db.scalar(select(League).where(League.id == league_id).with_for_update())
    if league is None:
        raise NotFoundError("League", league_id)
    if league.locked:
        raise ConflictError(
            "LEAGUE_LOCKED",
            "The league is administratively locked; unlock it before making changes.",
        )
    return league
