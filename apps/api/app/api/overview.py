from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession
from app.api.query import current_league
from app.api.serialization import public_draft, public_draft_pick, serialize
from app.models.entities import (
    Draft,
    DraftPick,
    LeagueEvent,
    LLMRun,
    ManagerMemory,
    Matchup,
    NflGame,
    Player,
    RosterAssignment,
    Team,
    TradeThread,
    WaiverClaim,
    WaiverPeriod,
)
from app.services.competition import standings

router = APIRouter(prefix="/api/v1", tags=["spectator"])


def _event_kind(event_type: str) -> str:
    if event_type.startswith("DRAFT") or event_type in {"ON_THE_CLOCK", "LLM_THINKING"}:
        return "DRAFT"
    if event_type.startswith("WAIVER") or event_type.startswith("PLAYER_"):
        return "WAIVER"
    if event_type.startswith("TRADE"):
        return "TRADE"
    if event_type.startswith("LINEUP"):
        return "LINEUP"
    return "SYSTEM"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _upcoming_actions(
    db: DbSession, league_id: str, season: int, week: int
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    actions: list[dict[str, Any]] = []
    periods = db.scalars(
        select(WaiverPeriod).where(
            WaiverPeriod.league_id == league_id,
            WaiverPeriod.status == "OPEN",
        )
    ).all()
    for period in periods:
        deadline = _as_utc(period.deadline_at)
        processing = _as_utc(period.processing_at or period.deadline_at)
        if deadline > now:
            actions.append(
                {
                    "kind": "WAIVER",
                    "action": "CLAIMS_LOCK",
                    "scheduled_at": deadline.isoformat(),
                    "week": period.week,
                    "waiver_period_id": period.id,
                }
            )
        if processing > now:
            actions.append(
                {
                    "kind": "WAIVER",
                    "action": "WAIVERS_PROCESS",
                    "scheduled_at": processing.isoformat(),
                    "week": period.week,
                    "waiver_period_id": period.id,
                }
            )

    if week > 0:
        games = db.scalars(
            select(NflGame)
            .where(
                NflGame.season == season,
                NflGame.week == week,
                NflGame.kickoff_at > now,
            )
            .order_by(NflGame.kickoff_at)
        ).all()
        for game in games:
            actions.append(
                {
                    "kind": "LOCK",
                    "action": "GAME_LOCK",
                    "scheduled_at": _as_utc(game.kickoff_at).isoformat(),
                    "week": game.week,
                    "game_id": game.id,
                    "home_team": game.home_team,
                    "away_team": game.away_team,
                }
            )

    expiring_trades = db.scalars(
        select(TradeThread).where(
            TradeThread.league_id == league_id,
            TradeThread.status.in_(("PROPOSED", "COUNTERED")),
            TradeThread.expires_at.is_not(None),
            TradeThread.expires_at > now,
        )
    ).all()
    for trade in expiring_trades:
        assert trade.expires_at is not None
        actions.append(
            {
                "kind": "TRADE",
                "action": "TRADE_EXPIRES",
                "scheduled_at": _as_utc(trade.expires_at).isoformat(),
                "trade_id": trade.id,
            }
        )
    return sorted(actions, key=lambda item: (item["scheduled_at"], item["action"]))


def _team_usage(db: DbSession, league_id: str) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        select(
            LLMRun.team_id,
            func.count(LLMRun.id),
            func.coalesce(func.sum(LLMRun.cost_usd), 0),
            func.coalesce(func.avg(LLMRun.latency_ms), 0),
            func.count(LLMRun.id).filter(LLMRun.success.is_(False)),
        )
        .where(LLMRun.league_id == league_id)
        .group_by(LLMRun.team_id)
    ).all()
    return {
        row[0]: {
            "requests": row[1],
            "cost_usd": float(row[2]),
            "average_latency_ms": float(row[3]),
            "errors": row[4],
        }
        for row in rows
    }


def _recent_form(db: DbSession, league_id: str) -> dict[str, list[str]]:
    completed = db.scalars(
        select(Matchup)
        .where(Matchup.league_id == league_id, Matchup.status == "COMPLETE")
        .order_by(Matchup.week.desc(), Matchup.matchup_number.desc())
    ).all()
    form: dict[str, list[str]] = {}
    for matchup in completed:
        for team_id in (matchup.home_team_id, matchup.away_team_id):
            if team_id is None or len(form.setdefault(team_id, [])) >= 7:
                continue
            if matchup.winner_team_id is None:
                result = "T"
            else:
                result = "W" if matchup.winner_team_id == team_id else "L"
            form[team_id].append(result)
    return {team_id: list(reversed(results)) for team_id, results in form.items()}


def _manager_profile(db: DbSession, team: Team) -> dict[str, Any]:
    has_hidden_draft = bool(
        db.scalar(
            select(DraftPick.id)
            .where(
                DraftPick.team_id == team.id,
                DraftPick.player_id.is_not(None),
                DraftPick.state != "REVEALED",
            )
            .limit(1)
        )
    )
    has_hidden_waivers = bool(
        db.scalar(
            select(WaiverClaim.id)
            .join(WaiverPeriod, WaiverPeriod.id == WaiverClaim.waiver_period_id)
            .where(
                WaiverClaim.team_id == team.id,
                WaiverPeriod.status != "PROCESSED",
            )
            .limit(1)
        )
    )
    if has_hidden_draft or has_hidden_waivers:
        return {"summary": {}, "withheld": True}
    memory = db.scalar(select(ManagerMemory).where(ManagerMemory.team_id == team.id))
    return {"summary": serialize(memory.summary) if memory else {}, "withheld": False}


@router.get("/league/actions")
def get_upcoming_actions(
    db: DbSession,
    league_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return _upcoming_actions(
        db, league.id, league.nfl_season, league.current_week
    )[:limit]


@router.get("/overview")
def get_spectator_overview(
    db: DbSession,
    league_id: str | None = None,
    event_limit: int = Query(50, ge=1, le=200),
    draft_pick_limit: int = Query(48, ge=1, le=120),
) -> dict[str, Any]:
    """Return the complete public read model needed by the spectator frontend."""
    league = current_league(db, league_id)
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    teams = list(
        db.scalars(select(Team).where(Team.league_id == league.id).order_by(Team.draft_position))
    )
    team_by_id = {team.id: team for team in teams}
    table = standings(db, league_id=league.id)
    standing_by_team = {row["team_id"]: row for row in table}
    usage_by_team = _team_usage(db, league.id)
    recent_form = _recent_form(db, league.id)
    for team in teams:
        usage = usage_by_team.setdefault(
            team.id,
            {"requests": 0, "cost_usd": 0.0, "average_latency_ms": 0.0, "errors": 0},
        )
        cost = float(usage["cost_usd"])
        usage["points_per_dollar"] = (
            round(float(standing_by_team[team.id]["points_for"]) / cost, 2)
            if cost > 0
            else None
        )

    pending_players = select(DraftPick.player_id).where(
        DraftPick.league_id == league.id,
        DraftPick.player_id.is_not(None),
        DraftPick.state != "REVEALED",
    )
    roster_rows = db.scalars(
        select(RosterAssignment)
        .where(
            RosterAssignment.league_id == league.id,
            RosterAssignment.player_id.not_in(pending_players),
        )
        .options(selectinload(RosterAssignment.player))
        .order_by(
            RosterAssignment.team_id,
            case(
                (RosterAssignment.slot_type == "STARTER", 0),
                (RosterAssignment.slot_type == "BENCH", 1),
                else_=2,
            ),
            RosterAssignment.position_slot,
        )
    ).all()
    rosters: dict[str, list[dict[str, Any]]] = {team.id: [] for team in teams}
    for assignment in roster_rows:
        rosters[assignment.team_id].append(
            serialize(assignment) | {"player": serialize(assignment.player)}
        )

    team_payload = [
        serialize(team)
        | {
            "standing": standing_by_team[team.id],
            "recent_form": recent_form.get(team.id, []),
            "roster": rosters[team.id],
            "manager_profile": _manager_profile(db, team),
            "usage": usage_by_team[team.id],
        }
        for team in sorted(teams, key=lambda item: standing_by_team[item.id]["rank"])
    ]

    matchups = db.scalars(
        select(Matchup)
        .where(Matchup.league_id == league.id, Matchup.week == league.current_week)
        .order_by(Matchup.matchup_number)
    ).all()
    matchup_payload = [
        serialize(matchup)
        | {
            "home_team": serialize(
                team_by_id.get(matchup.home_team_id) if matchup.home_team_id else None
            ),
            "away_team": serialize(
                team_by_id.get(matchup.away_team_id) if matchup.away_team_id else None
            ),
        }
        for matchup in matchups
    ]

    events = list(
        db.scalars(
            select(LeagueEvent)
            .where(LeagueEvent.league_id == league.id, LeagueEvent.visibility == "PUBLIC")
            .order_by(LeagueEvent.occurred_at.desc())
            .limit(event_limit)
        )
    )
    referenced_player_ids = {
        str(player_id)
        for event in events
        for player_id in (
            (event.data or {}).get("player_id"),
            (event.data or {}).get("drop_player_id"),
        )
        if player_id
    }
    players_by_id = (
        {
            player.id: player
            for player in db.scalars(select(Player).where(Player.id.in_(referenced_player_ids)))
        }
        if referenced_player_ids
        else {}
    )
    event_payload: list[dict[str, Any]] = []
    for event in events:
        player_id = (event.data or {}).get("player_id")
        drop_player_id = (event.data or {}).get("drop_player_id")
        event_payload.append(
            serialize(event)
            | {
                "kind": _event_kind(event.event_type),
                "team": serialize(team_by_id.get(event.team_id) if event.team_id else None),
                "player": serialize(
                    players_by_id.get(str(player_id)) if player_id is not None else None
                ),
                "dropped_player": serialize(
                    players_by_id.get(str(drop_player_id))
                    if drop_player_id is not None
                    else None
                ),
            }
        )

    revealed_picks = db.scalars(
        select(DraftPick)
        .where(DraftPick.league_id == league.id, DraftPick.state == "REVEALED")
        .options(selectinload(DraftPick.team), selectinload(DraftPick.player))
        .order_by(DraftPick.pick_number)
        .limit(draft_pick_limit)
    ).all()
    pick_payload = [
        public_draft_pick(pick)
        | {"team": serialize(pick.team), "player": serialize(pick.player)}
        for pick in revealed_picks
    ]

    total_usage: dict[str, Any] = {
        "requests": sum(item["requests"] for item in usage_by_team.values()),
        "cost_usd": round(sum(item["cost_usd"] for item in usage_by_team.values()), 6),
        "errors": sum(item["errors"] for item in usage_by_team.values()),
    }
    total_usage["success_rate"] = (
        round((total_usage["requests"] - total_usage["errors"]) / total_usage["requests"], 4)
        if total_usage["requests"]
        else None
    )
    league_points = round(sum(team.points_for for team in teams), 4)
    total_usage["points_per_dollar"] = (
        round(league_points / total_usage["cost_usd"], 2)
        if total_usage["cost_usd"] > 0
        else None
    )
    decision_rows = db.execute(
        select(LeagueEvent.event_type, LeagueEvent.data).where(
            LeagueEvent.league_id == league.id,
            LeagueEvent.visibility == "PUBLIC",
            or_(
                LeagueEvent.event_type.like("DRAFT%"),
                LeagueEvent.event_type.like("WAIVER%"),
                LeagueEvent.event_type.like("PLAYER_%"),
                LeagueEvent.event_type.like("TRADE%"),
                LeagueEvent.event_type.like("LINEUP%"),
            ),
        )
    ).all()
    current_week_decisions = sum(
        1 for row in decision_rows if (row.data or {}).get("week") == league.current_week
    )
    picks_made = (
        db.scalar(
            select(func.count(DraftPick.id)).where(
                DraftPick.league_id == league.id, DraftPick.state == "REVEALED"
            )
        )
        or 0
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "league": serialize(league),
        "draft": (
            public_draft(draft)
            | {
                "picks_made": picks_made,
                "total_picks": draft.rounds * len(draft.order),
            }
            if draft
            else None
        ),
        "metrics": {
            "league_points": league_points,
            "public_decisions": len(decision_rows),
            "current_week_decisions": current_week_decisions,
            "llm_usage": total_usage,
        },
        "teams": team_payload,
        "matchups": matchup_payload,
        "events": event_payload,
        "draft_picks": pick_payload,
        "upcoming_actions": _upcoming_actions(
            db, league.id, league.nfl_season, league.current_week
        )[:50],
    }
