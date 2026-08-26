from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.entities import League


def current_league(db: Session, league_id: str | None = None) -> League:
    if league_id:
        league = db.get(League, league_id)
    else:
        league = db.scalar(select(League).order_by(desc(League.created_at)).limit(1))
    if league is None:
        raise NotFoundError("league", league_id or "current")
    return league
