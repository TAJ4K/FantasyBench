from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import AdminAccess, DbSession
from app.api.query import current_league
from app.api.serialization import public_draft, public_draft_pick, serialize
from app.core.errors import NotFoundError
from app.models.entities import Draft, DraftPick, LeagueEvent, Player, RosterAssignment, Team
from app.schemas.api import AdminDraftPickRequest, DraftOrderRequest
from app.services.draft import DraftService

router = APIRouter(prefix="/api/v1/draft", tags=["draft"])


def _draft(db: DbSession, league_id: str | None) -> Draft:
    league = current_league(db, league_id)
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    if not draft:
        raise NotFoundError("draft", league.id)
    return draft


@router.get("")
def get_draft(db: DbSession, league_id: str | None = None) -> dict[str, Any]:
    draft = _draft(db, league_id)
    made = (
        db.scalar(
            select(func.count(DraftPick.id)).where(
                DraftPick.draft_id == draft.id,
                DraftPick.state == "REVEALED",
            )
        )
        or 0
    )
    return public_draft(draft) | {
        "picks_made": made,
        "total_picks": draft.rounds * len(draft.order),
    }


@router.post("/start")
async def start_draft(
    request: Request,
    db: DbSession,
    _: AdminAccess,
    league_id: str | None = None,
) -> dict[str, Any]:
    league = current_league(db, league_id)
    turn = DraftService(db).start(league.id)
    db.commit()
    request.app.state.draft_runner.start(league.id)
    return {
        "draft": serialize(turn.draft),
        "current": {"team": serialize(turn.team), "pick": serialize(turn.pick)},
    }


@router.post("/pause")
def pause_draft(db: DbSession, _: AdminAccess, league_id: str | None = None) -> dict[str, Any]:
    league = current_league(db, league_id)
    draft = DraftService(db).pause(league.id)
    db.commit()
    return serialize(draft)


@router.post("/resume")
async def resume_draft(
    request: Request,
    db: DbSession,
    _: AdminAccess,
    league_id: str | None = None,
) -> dict[str, Any]:
    league = current_league(db, league_id)
    turn = DraftService(db).resume(league.id)
    db.commit()
    request.app.state.draft_runner.start(league.id)
    return {
        "draft": public_draft(turn.draft),
        "team": serialize(turn.team),
        "pick": public_draft_pick(turn.pick),
    }


@router.get("/order")
def get_draft_order(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    draft = _draft(db, league_id)
    teams = {
        team.id: team for team in db.scalars(select(Team).where(Team.league_id == draft.league_id))
    }
    return [
        {"position": index, "team": serialize(teams[team_id])}
        for index, team_id in enumerate(draft.order, 1)
    ]


@router.put("/order")
def set_draft_order(
    payload: DraftOrderRequest,
    db: DbSession,
    _: AdminAccess,
    league_id: str | None = None,
) -> dict[str, Any]:
    league = current_league(db, league_id)
    draft = DraftService(db).set_order(league.id, payload.team_ids)
    db.commit()
    return serialize(draft)


@router.get("/picks")
def get_draft_picks(
    db: DbSession,
    league_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    draft = _draft(db, league_id)
    total = db.scalar(select(func.count(DraftPick.id)).where(DraftPick.draft_id == draft.id)) or 0
    picks = db.scalars(
        select(DraftPick)
        .where(DraftPick.draft_id == draft.id)
        .options(selectinload(DraftPick.team), selectinload(DraftPick.player))
        .order_by(DraftPick.pick_number)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [
            public_draft_pick(pick)
            | {
                "team": serialize(pick.team),
                "player": serialize(pick.player) if pick.state == "REVEALED" else None,
            }
            for pick in picks
        ],
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@router.get("/picks/{pick_id}")
def get_draft_pick(db: DbSession, pick_id: str) -> dict[str, Any]:
    pick = db.scalar(
        select(DraftPick)
        .where(DraftPick.id == pick_id)
        .options(selectinload(DraftPick.team), selectinload(DraftPick.player))
    )
    if not pick:
        raise NotFoundError("draft pick", pick_id)
    return public_draft_pick(pick) | {
        "team": serialize(pick.team),
        "player": serialize(pick.player) if pick.state == "REVEALED" else None,
    }


@router.get("/current")
def get_current_pick(db: DbSession, league_id: str | None = None) -> dict[str, Any] | None:
    draft = _draft(db, league_id)
    turn = DraftService(db).current(draft.league_id)
    db.commit()
    if turn is None:
        return None
    return {
        "draft": public_draft(turn.draft),
        "team": serialize(turn.team),
        "pick": public_draft_pick(turn.pick),
    }


@router.get("/available")
def get_available_players(
    db: DbSession,
    league_id: str | None = None,
    position: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    draft = _draft(db, league_id)
    pending = select(DraftPick.player_id).where(
        DraftPick.league_id == draft.league_id,
        DraftPick.player_id.is_not(None),
        DraftPick.state != "REVEALED",
    )
    owned = select(RosterAssignment.player_id).where(
        RosterAssignment.league_id == draft.league_id,
        RosterAssignment.player_id.not_in(pending),
    )
    filters: list[ColumnElement[bool]] = [Player.active.is_(True), Player.id.not_in(owned)]
    if position:
        filters.append(Player.position == position.upper())
    total = db.scalar(select(func.count(Player.id)).where(*filters)) or 0
    items = db.scalars(
        select(Player).where(*filters).order_by(Player.full_name).offset(offset).limit(limit)
    ).all()
    return {
        "items": serialize(items),
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@router.get("/events")
def get_draft_events(
    db: DbSession,
    league_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
) -> list[dict[str, Any]]:
    draft = _draft(db, league_id)
    return serialize(
        db.scalars(
            select(LeagueEvent)
            .where(
                LeagueEvent.league_id == draft.league_id,
                LeagueEvent.event_type.like("DRAFT%")
                | (LeagueEvent.event_type == "ON_THE_CLOCK")
                | (LeagueEvent.event_type == "LLM_THINKING"),
            )
            .order_by(LeagueEvent.occurred_at.desc())
            .limit(limit)
        ).all()
    )


@router.post("/admin/pick")
async def admin_pick(
    payload: AdminDraftPickRequest,
    request: Request,
    db: DbSession,
    _: AdminAccess,
    league_id: str | None = None,
) -> dict[str, Any]:
    draft = _draft(db, league_id)
    pick = DraftService(db).admin_pick(
        draft.league_id,
        payload.player_id,
        public_reasoning=payload.public_reasoning,
        confidence=payload.confidence,
        reveal_delay_seconds=0,
    )
    DraftService(db).reveal_pick(pick.id, force=True)
    db.commit()
    request.app.state.draft_runner.start(draft.league_id)
    return serialize(pick)


@router.post("/admin/undo")
def undo_pick(db: DbSession, _: AdminAccess, league_id: str | None = None) -> dict[str, Any]:
    draft = _draft(db, league_id)
    pick = DraftService(db).undo_last_pick(draft.league_id)
    db.commit()
    return serialize(pick)
