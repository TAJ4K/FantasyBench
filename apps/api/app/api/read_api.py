from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession
from app.api.query import current_league
from app.api.serialization import public_draft_pick, public_llm_run, serialize
from app.core.errors import NotFoundError
from app.db.session import SessionLocal
from app.models.entities import (
    Draft,
    DraftPick,
    LeagueEvent,
    LineupDecision,
    LLMRun,
    ManagerMemory,
    Matchup,
    Player,
    PlayerFantasyScore,
    PlayerNews,
    PlayerWeekStat,
    RosterAssignment,
    Team,
    TradeOffer,
    TradeThread,
    Transaction,
    WaiverClaim,
    WaiverPeriod,
)
from app.services.competition import standings

router = APIRouter(prefix="/api/v1")


def _page(items: Sequence[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": serialize(items),
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@router.get("/league", tags=["league"])
def get_league(db: DbSession, league_id: str | None = None) -> dict[str, Any]:
    league = current_league(db, league_id)
    return serialize(league)


@router.get("/league/settings", tags=["league"])
def get_league_settings(db: DbSession, league_id: str | None = None) -> dict[str, Any]:
    league = current_league(db, league_id)
    return {
        "league_id": league.id,
        "settings": league.settings,
        "roster_config": league.roster_config,
        "scoring_config": league.scoring_config,
    }


@router.get("/league/status", tags=["league"])
def get_league_status(db: DbSession, league_id: str | None = None) -> dict[str, Any]:
    league = current_league(db, league_id)
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    return {
        "league_id": league.id,
        "status": league.status,
        "locked": league.locked,
        "current_week": league.current_week,
        "draft_status": draft.status if draft else None,
        "draft_current_pick": draft.current_pick_number if draft else None,
    }


@router.get("/teams", tags=["teams"])
def list_teams(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    teams = db.scalars(
        select(Team).where(Team.league_id == league.id).order_by(Team.draft_position)
    ).all()
    return serialize(teams)


def _team(db: DbSession, team_id: str) -> Team:
    team = db.get(Team, team_id)
    if not team:
        raise NotFoundError("team", team_id)
    return team


def _pending_draft_players(league_id: str) -> Any:
    """Player ids whose selections are intentionally still private."""
    return select(DraftPick.player_id).where(
        DraftPick.league_id == league_id,
        DraftPick.player_id.is_not(None),
        DraftPick.state != "REVEALED",
    )


def _public_transaction_filter() -> Any:
    """Draft audit rows become public only with their matching revealed pick."""
    return or_(
        Transaction.transaction_type != "DRAFT",
        exists(
            select(DraftPick.id).where(
                DraftPick.league_id == Transaction.league_id,
                DraftPick.team_id == Transaction.team_id,
                DraftPick.player_id == Transaction.player_id,
                DraftPick.state == "REVEALED",
            )
        ),
    )


def _llm_run_is_public(db: DbSession, run: LLMRun) -> bool:
    if run.decision_type == "DRAFT":
        return bool(
            db.scalar(
                select(DraftPick.id).where(
                    DraftPick.llm_run_id == run.id,
                    DraftPick.state == "REVEALED",
                )
            )
        )
    if run.decision_type == "WAIVER":
        context = ((run.request_payload or {}).get("metadata") or {}).get("context") or {}
        period_id = context.get("waiver_period_id")
        query = select(WaiverPeriod.id).where(
            WaiverPeriod.league_id == run.league_id,
            WaiverPeriod.status == "PROCESSED",
        )
        if period_id:
            query = query.where(WaiverPeriod.id == str(period_id))
        elif context.get("week") is not None:
            query = query.where(WaiverPeriod.week == int(context["week"]))
        else:
            return False
        return bool(db.scalar(query.limit(1)))
    return True


@router.get("/teams/{team_id}", tags=["teams"])
def get_team(db: DbSession, team_id: str) -> dict[str, Any]:
    return serialize(_team(db, team_id))


@router.get("/teams/{team_id}/roster", tags=["teams"])
def get_team_roster(db: DbSession, team_id: str) -> list[dict[str, Any]]:
    team = _team(db, team_id)
    assignments = db.scalars(
        select(RosterAssignment)
        .where(
            RosterAssignment.team_id == team_id,
            RosterAssignment.player_id.not_in(_pending_draft_players(team.league_id)),
        )
        .options(selectinload(RosterAssignment.player))
        .order_by(RosterAssignment.slot_type, RosterAssignment.position_slot)
    ).all()
    return [serialize(item) | {"player": serialize(item.player)} for item in assignments]


@router.get("/teams/{team_id}/lineup", tags=["teams"])
def get_team_lineup(db: DbSession, team_id: str, week: int | None = None) -> dict[str, Any]:
    team = _team(db, team_id)
    target_week = week or current_league(db, team.league_id).current_week
    assignments = db.scalars(
        select(RosterAssignment)
        .where(
            RosterAssignment.team_id == team_id,
            RosterAssignment.slot_type == "STARTER",
        )
        .options(selectinload(RosterAssignment.player))
    ).all()
    decision = db.scalar(
        select(LineupDecision)
        .where(LineupDecision.team_id == team_id, LineupDecision.week == target_week)
        .order_by(LineupDecision.created_at.desc())
        .limit(1)
    )
    return {
        "team_id": team_id,
        "week": target_week,
        "starters": [serialize(item) | {"player": serialize(item.player)} for item in assignments],
        "latest_decision": serialize(decision),
    }


@router.get("/teams/{team_id}/transactions", tags=["teams"])
def get_team_transactions(
    db: DbSession, team_id: str, limit: int = Query(50, ge=1, le=200)
) -> list[dict[str, Any]]:
    _team(db, team_id)
    return serialize(
        db.scalars(
            select(Transaction)
            .where(Transaction.team_id == team_id, _public_transaction_filter())
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        ).all()
    )


@router.get("/teams/{team_id}/draft-picks", tags=["teams"])
def get_team_draft_picks(db: DbSession, team_id: str) -> list[dict[str, Any]]:
    _team(db, team_id)
    picks = db.scalars(
        select(DraftPick)
        .where(DraftPick.team_id == team_id, DraftPick.state != "UNDONE")
        .options(selectinload(DraftPick.player))
        .order_by(DraftPick.pick_number)
    ).all()
    return [
        public_draft_pick(pick)
        | {"player": serialize(pick.player) if pick.state == "REVEALED" else None}
        for pick in picks
    ]


@router.get("/teams/{team_id}/waivers", tags=["teams"])
def get_team_waivers(db: DbSession, team_id: str) -> list[dict[str, Any]]:
    _team(db, team_id)
    return serialize(
        db.scalars(
            select(WaiverClaim)
            .join(WaiverPeriod, WaiverPeriod.id == WaiverClaim.waiver_period_id)
            .where(WaiverClaim.team_id == team_id, WaiverPeriod.status == "PROCESSED")
            .order_by(WaiverClaim.created_at.desc())
        ).all()
    )


@router.get("/teams/{team_id}/trades", tags=["teams"])
def get_team_trades(db: DbSession, team_id: str) -> list[dict[str, Any]]:
    _team(db, team_id)
    return serialize(
        db.scalars(
            select(TradeThread)
            .where(
                or_(
                    TradeThread.initiator_team_id == team_id,
                    TradeThread.recipient_team_id == team_id,
                )
            )
            .order_by(TradeThread.updated_at.desc())
        ).all()
    )


@router.get("/teams/{team_id}/decisions", tags=["teams"])
def get_team_decisions(
    db: DbSession,
    team_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    _team(db, team_id)
    runs = db.scalars(
        select(LLMRun).where(LLMRun.team_id == team_id).order_by(LLMRun.started_at.desc())
    ).all()
    public_runs = [run for run in runs if _llm_run_is_public(db, run)]
    return [public_llm_run(run) for run in public_runs[offset : offset + limit]]


@router.get("/teams/{team_id}/llm-usage", tags=["teams"])
def get_team_llm_usage(db: DbSession, team_id: str) -> dict[str, Any]:
    _team(db, team_id)
    row = db.execute(
        select(
            func.count(LLMRun.id),
            func.coalesce(func.sum(LLMRun.input_tokens), 0),
            func.coalesce(func.sum(LLMRun.output_tokens), 0),
            func.coalesce(func.sum(LLMRun.reasoning_tokens), 0),
            func.coalesce(func.sum(LLMRun.cost_usd), 0),
            func.coalesce(func.avg(LLMRun.latency_ms), 0),
            func.count(LLMRun.id).filter(LLMRun.success.is_(False)),
        ).where(LLMRun.team_id == team_id)
    ).one()
    return {
        "team_id": team_id,
        "requests": row[0],
        "input_tokens": row[1],
        "output_tokens": row[2],
        "reasoning_tokens": row[3],
        "cost_usd": float(row[4]),
        "average_latency_ms": float(row[5]),
        "errors": row[6],
    }


@router.get("/players", tags=["players"])
def list_players(
    db: DbSession,
    league_id: str | None = None,
    position: str | None = None,
    nfl_team: str | None = None,
    available: bool | None = None,
    injured: bool | None = None,
    active: bool | None = None,
    name: str | None = None,
    owned_by: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    league = current_league(db, league_id)
    query = select(Player)
    count_query = select(func.count(Player.id))
    filters = []
    if position:
        filters.append(Player.position == position.upper())
    if nfl_team:
        filters.append(Player.nfl_team == nfl_team.upper())
    if injured is not None:
        filters.append(
            Player.injury_status.is_not(None) if injured else Player.injury_status.is_(None)
        )
    if active is not None:
        filters.append(Player.active == active)
    if name:
        filters.append(func.lower(Player.full_name).contains(name.lower()))
    ownership = select(RosterAssignment.player_id).where(
        RosterAssignment.league_id == league.id,
        RosterAssignment.player_id.not_in(_pending_draft_players(league.id)),
    )
    if available is True:
        filters.append(Player.id.not_in(ownership))
    elif available is False:
        filters.append(Player.id.in_(ownership))
    if owned_by:
        filters.append(
            Player.id.in_(
                select(RosterAssignment.player_id)
                .where(RosterAssignment.team_id == owned_by)
                .where(RosterAssignment.player_id.not_in(_pending_draft_players(league.id)))
            )
        )
    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))
    total = db.scalar(count_query) or 0
    players = db.scalars(query.order_by(Player.full_name).offset(offset).limit(limit)).all()
    owner_rows = (
        db.execute(
            select(RosterAssignment.player_id, RosterAssignment.team_id).where(
                RosterAssignment.league_id == league.id,
                RosterAssignment.player_id.in_([player.id for player in players]),
                RosterAssignment.player_id.not_in(_pending_draft_players(league.id)),
            )
        ).all()
        if players
        else []
    )
    owners: dict[str, str] = {row.player_id: row.team_id for row in owner_rows}
    items = [serialize(player) | {"owned_by": owners.get(player.id)} for player in players]
    return _page(items, total, limit, offset)


def _player(db: DbSession, player_id: str) -> Player:
    player = db.get(Player, player_id)
    if not player:
        raise NotFoundError("player", player_id)
    return player


@router.get("/players/{player_id}", tags=["players"])
def get_player(db: DbSession, player_id: str) -> dict[str, Any]:
    return serialize(_player(db, player_id))


@router.get("/players/{player_id}/stats", tags=["players"])
@router.get("/players/{player_id}/game-log", tags=["players"])
def get_player_stats(
    db: DbSession,
    player_id: str,
    season: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    _player(db, player_id)
    query = select(PlayerWeekStat).where(PlayerWeekStat.player_id == player_id)
    if season:
        query = query.where(PlayerWeekStat.season == season)
    return serialize(
        db.scalars(
            query.order_by(PlayerWeekStat.season.desc(), PlayerWeekStat.week)
            .offset(offset)
            .limit(limit)
        ).all()
    )


@router.get("/players/{player_id}/news", tags=["players"])
def get_player_news(
    db: DbSession,
    player_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    _player(db, player_id)
    return serialize(
        db.scalars(
            select(PlayerNews)
            .where(PlayerNews.player_id == player_id)
            .order_by(PlayerNews.published_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


@router.get("/players/{player_id}/fantasy", tags=["players"])
def get_player_fantasy(
    db: DbSession,
    player_id: str,
    league_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    _player(db, player_id)
    league = current_league(db, league_id)
    return serialize(
        db.scalars(
            select(PlayerFantasyScore)
            .where(
                PlayerFantasyScore.player_id == player_id, PlayerFantasyScore.league_id == league.id
            )
            .order_by(PlayerFantasyScore.season.desc(), PlayerFantasyScore.week)
            .offset(offset)
            .limit(limit)
        ).all()
    )


@router.get("/transactions", tags=["transactions"])
def list_transactions(
    db: DbSession,
    league_id: str | None = None,
    team: str | None = None,
    player: str | None = None,
    transaction_type: str | None = None,
    week: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    league = current_league(db, league_id)
    filters = [Transaction.league_id == league.id, _public_transaction_filter()]
    if team:
        filters.append(Transaction.team_id == team)
    if player:
        filters.append(Transaction.player_id == player)
    if transaction_type:
        filters.append(Transaction.transaction_type == transaction_type.upper())
    if week is not None:
        filters.append(Transaction.week == week)
    if date_from:
        filters.append(Transaction.occurred_at >= date_from)
    if date_to:
        filters.append(Transaction.occurred_at <= date_to)
    total = db.scalar(select(func.count(Transaction.id)).where(*filters)) or 0
    items = db.scalars(
        select(Transaction)
        .where(*filters)
        .order_by(Transaction.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return _page(items, total, limit, offset)


@router.get("/transactions/{transaction_id}", tags=["transactions"])
def get_transaction(db: DbSession, transaction_id: str) -> dict[str, Any]:
    item = db.get(Transaction, transaction_id)
    if not item:
        raise NotFoundError("transaction", transaction_id)
    if item.transaction_type == "DRAFT" and not db.scalar(
        select(DraftPick.id).where(
            DraftPick.league_id == item.league_id,
            DraftPick.team_id == item.team_id,
            DraftPick.player_id == item.player_id,
            DraftPick.state == "REVEALED",
        )
    ):
        raise NotFoundError("transaction", transaction_id)
    return serialize(item)


@router.get("/events", tags=["events"])
@router.get("/league/events", tags=["league"])
def list_events(
    db: DbSession,
    league_id: str | None = None,
    event_type: str | None = None,
    team_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    league = current_league(db, league_id)
    filters = [LeagueEvent.league_id == league.id, LeagueEvent.visibility == "PUBLIC"]
    if event_type:
        filters.append(LeagueEvent.event_type == event_type)
    if team_id:
        filters.append(LeagueEvent.team_id == team_id)
    total = db.scalar(select(func.count(LeagueEvent.id)).where(*filters)) or 0
    items = db.scalars(
        select(LeagueEvent)
        .where(*filters)
        .order_by(LeagueEvent.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return _page(items, total, limit, offset)


@router.get("/events/stream", tags=["events"])
async def stream_events(league_id: str, after: datetime | None = None) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        cursor = after
        while True:
            with SessionLocal() as session:
                query = select(LeagueEvent).where(
                    LeagueEvent.league_id == league_id,
                    LeagueEvent.visibility == "PUBLIC",
                )
                if cursor:
                    query = query.where(LeagueEvent.occurred_at > cursor)
                events = session.scalars(query.order_by(LeagueEvent.occurred_at).limit(100)).all()
                for event in events:
                    cursor = event.occurred_at
                    event_json = json.dumps(serialize(event))
                    yield f"id: {event.id}\nevent: {event.event_type}\ndata: {event_json}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/schedule", tags=["competition"])
def get_schedule(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return serialize(
        db.scalars(
            select(Matchup)
            .where(Matchup.league_id == league.id)
            .order_by(Matchup.week, Matchup.matchup_number)
        ).all()
    )


@router.get("/schedule/{week}", tags=["competition"])
@router.get("/matchups/{week}", tags=["competition"])
def get_week_matchups(
    db: DbSession, week: int, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return serialize(
        db.scalars(
            select(Matchup)
            .where(Matchup.league_id == league.id, Matchup.week == week)
            .order_by(Matchup.matchup_number)
        ).all()
    )


@router.get("/matchups", tags=["competition"])
def get_matchups(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    return get_schedule(db, league_id)


@router.get("/matchups/{week}/{matchup_id}", tags=["competition"])
def get_matchup(db: DbSession, week: int, matchup_id: str) -> dict[str, Any]:
    matchup = db.get(Matchup, matchup_id)
    if not matchup or matchup.week != week:
        raise NotFoundError("matchup", matchup_id)
    return serialize(matchup)


@router.get("/standings", tags=["competition"])
def get_standings(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return standings(db, league_id=league.id)


@router.get("/weeks/{week}/scores", tags=["scoring"])
def get_week_scores(db: DbSession, week: int, league_id: str | None = None) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return serialize(
        db.scalars(
            select(PlayerFantasyScore)
            .where(PlayerFantasyScore.league_id == league.id, PlayerFantasyScore.week == week)
            .order_by(PlayerFantasyScore.total.desc())
        ).all()
    )


@router.get("/weeks/{week}/players/{player_id}/score", tags=["scoring"])
def get_player_week_score(
    db: DbSession, week: int, player_id: str, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    score = db.scalar(
        select(PlayerFantasyScore).where(
            PlayerFantasyScore.league_id == league.id,
            PlayerFantasyScore.week == week,
            PlayerFantasyScore.player_id == player_id,
        )
    )
    if not score:
        raise NotFoundError("player fantasy score", f"{player_id}/week/{week}")
    return serialize(score)


def _usage_group(db: DbSession, league_id: str, column: Any) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            column,
            func.count(LLMRun.id),
            func.coalesce(func.sum(LLMRun.input_tokens), 0),
            func.coalesce(func.sum(LLMRun.output_tokens), 0),
            func.coalesce(func.sum(LLMRun.reasoning_tokens), 0),
            func.coalesce(func.sum(LLMRun.cost_usd), 0),
            func.coalesce(func.avg(LLMRun.latency_ms), 0),
            func.count(LLMRun.id).filter(LLMRun.success.is_(False)),
        )
        .where(LLMRun.league_id == league_id)
        .group_by(column)
    ).all()
    return [
        {
            "key": row[0],
            "requests": row[1],
            "input_tokens": row[2],
            "output_tokens": row[3],
            "reasoning_tokens": row[4],
            "cost_usd": float(row[5]),
            "average_latency_ms": float(row[6]),
            "errors": row[7],
        }
        for row in rows
    ]


@router.get("/llm/runs", tags=["llm"])
def list_llm_runs(
    db: DbSession,
    league_id: str | None = None,
    team_id: str | None = None,
    model: str | None = None,
    decision_type: str | None = None,
    success: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    league = current_league(db, league_id)
    filters = [LLMRun.league_id == league.id]
    if team_id:
        filters.append(LLMRun.team_id == team_id)
    if model:
        filters.append(LLMRun.model == model)
    if decision_type:
        filters.append(LLMRun.decision_type == decision_type.upper())
    if success is not None:
        filters.append(LLMRun.success == success)
    runs = db.scalars(select(LLMRun).where(*filters).order_by(LLMRun.started_at.desc())).all()
    public_runs = [run for run in runs if _llm_run_is_public(db, run)]
    return _page(
        [public_llm_run(run) for run in public_runs[offset : offset + limit]],
        len(public_runs),
        limit,
        offset,
    )


@router.get("/llm/usage", tags=["llm"])
def llm_usage(db: DbSession, league_id: str | None = None) -> dict[str, Any]:
    league = current_league(db, league_id)
    grouped = _usage_group(db, league.id, LLMRun.model)
    return {
        "league_id": league.id,
        "requests": sum(item["requests"] for item in grouped),
        "input_tokens": sum(item["input_tokens"] for item in grouped),
        "output_tokens": sum(item["output_tokens"] for item in grouped),
        "reasoning_tokens": sum(item["reasoning_tokens"] for item in grouped),
        "cost_usd": sum(item["cost_usd"] for item in grouped),
        "errors": sum(item["errors"] for item in grouped),
    }


@router.get("/llm/usage/teams", tags=["llm"])
def llm_usage_teams(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    return _usage_group(db, current_league(db, league_id).id, LLMRun.team_id)


@router.get("/llm/usage/models", tags=["llm"])
def llm_usage_models(db: DbSession, league_id: str | None = None) -> list[dict[str, Any]]:
    return _usage_group(db, current_league(db, league_id).id, LLMRun.model)


@router.get("/manager-memory/{team_id}", tags=["llm"])
def manager_memory(db: DbSession, team_id: str) -> dict[str, Any]:
    team = _team(db, team_id)
    has_private_draft = bool(
        db.scalar(
            select(DraftPick.id)
            .where(
                DraftPick.team_id == team_id,
                DraftPick.player_id.is_not(None),
                DraftPick.state != "REVEALED",
            )
            .limit(1)
        )
    )
    has_private_waiver = bool(
        db.scalar(
            select(WaiverClaim.id)
            .join(WaiverPeriod, WaiverPeriod.id == WaiverClaim.waiver_period_id)
            .where(
                WaiverClaim.team_id == team_id,
                WaiverPeriod.league_id == team.league_id,
                WaiverPeriod.status != "PROCESSED",
            )
            .limit(1)
        )
    )
    if has_private_draft or has_private_waiver:
        return {"team_id": team_id, "summary": {}, "withheld": True}
    memory = db.scalar(select(ManagerMemory).where(ManagerMemory.team_id == team_id))
    return serialize(memory) if memory else {"team_id": team_id, "summary": {}}


@router.get("/trades", tags=["trades"])
def list_trades(
    db: DbSession, league_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    query = select(TradeThread).where(TradeThread.league_id == league.id)
    if status:
        query = query.where(TradeThread.status == status.upper())
    return serialize(db.scalars(query.order_by(TradeThread.updated_at.desc())).all())


@router.get("/trades/{trade_id}", tags=["trades"])
def get_trade(db: DbSession, trade_id: str) -> dict[str, Any]:
    thread = db.get(TradeThread, trade_id)
    if not thread:
        raise NotFoundError("trade", trade_id)
    offers = db.scalars(
        select(TradeOffer).where(TradeOffer.thread_id == trade_id).order_by(TradeOffer.sequence)
    ).all()
    return serialize(thread) | {"offers": serialize(offers)}
