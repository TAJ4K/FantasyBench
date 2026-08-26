from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import LeagueEvent


def emit_event(
    db: Session,
    league_id: str,
    event_type: str,
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    team_id: str | None = None,
    data: dict[str, Any] | None = None,
    commentary: str | None = None,
    visibility: str = "PUBLIC",
) -> LeagueEvent:
    event = LeagueEvent(
        league_id=league_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        team_id=team_id,
        data=data or {},
        public_commentary=commentary,
        visibility=visibility,
    )
    db.add(event)
    db.flush()
    return event
