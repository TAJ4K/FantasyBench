from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.agents.contracts import LLMRequest
from app.agents.prompts import build_prompt
from app.agents.service import LLMInvocationService
from app.api.deps import AdminAccess, AppSettings, DbSession
from app.api.query import current_league
from app.api.serialization import serialize
from app.core.errors import ConflictError, NotFoundError
from app.fixtures.players import seed_fixture_players
from app.models.entities import (
    Draft,
    FantasyWeek,
    League,
    LineupDecision,
    LLMRun,
    Matchup,
    PlayerWeekStat,
    Team,
    WaiverClaim,
    WaiverPeriod,
)
from app.models.enums import LeagueStatus
from app.nfl import NFLDataSyncService, NflverseProvider, SleeperProvider
from app.schemas.api import (
    AddDropRequest,
    DropRequest,
    LeagueInitializeRequest,
    LineupSetRequest,
    StatsLoadRequest,
    TradeActionRequest,
    TradeCounterRequest,
    TradeProposalRequest,
    TriggerDecisionRequest,
    WaiverClaimsRequest,
    WaiverPeriodRequest,
)
from app.schemas.decisions import (
    LineupDecisionResponse,
    MemorySummary,
    TradeProposalDecision,
    TradeResponseDecision,
    WaiverDecision,
)
from app.services.competition import calculate_matchup, complete_matchup, playoff_seeds
from app.services.draft import DraftService, randomized_order
from app.services.events import emit_event
from app.services.guards import ensure_league_unlocked
from app.services.initialization import initialize_league
from app.services.playoffs import advance_playoffs, seed_playoffs
from app.services.rosters import RosterService
from app.services.scoring import persist_player_score
from app.services.trades import (
    accept_trade,
    cancel_trade,
    counter_trade,
    propose_trade,
    reject_trade,
)
from app.services.transactions import add_free_agent, create_transaction
from app.services.waivers import process_waivers, submit_claims

router = APIRouter(prefix="/api/v1")


@router.post("/admin/initialize", tags=["admin"])
def initialize(
    payload: LeagueInitializeRequest,
    db: DbSession,
    _: AdminAccess,
    app_settings: AppSettings,
) -> dict[str, Any]:
    if app_settings.app_env == "production" and payload.seed_fixture_players:
        raise ConflictError(
            "FIXTURE_PLAYERS_FORBIDDEN",
            "Fixture player seeding is disabled in production; synchronize real players instead.",
        )
    league = initialize_league(
        db,
        name=payload.name,
        nfl_season=payload.nfl_season,
        settings={
            "waiver_period_hours": app_settings.waiver_period_hours,
            "waiver_processing_grace_minutes": (app_settings.waiver_processing_grace_minutes),
        },
    )
    seeded = seed_fixture_players(db) if payload.seed_fixture_players else 0
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    if payload.randomize_draft_order and draft and draft.status == "NOT_STARTED":
        seed = payload.random_seed if payload.random_seed is not None else payload.nfl_season
        DraftService(db).set_order(
            league.id, randomized_order(list(draft.order), seed), random_seed=seed
        )
    emit_event(
        db,
        league.id,
        "LEAGUE_INITIALIZED",
        aggregate_type="LEAGUE",
        aggregate_id=league.id,
        data={"nfl_season": league.nfl_season, "fixture_players_seeded": seeded},
    )
    db.commit()
    return {
        "league": serialize(league),
        "draft": serialize(draft),
        "fixture_players_seeded": seeded,
    }


@router.put("/admin/league/lock", tags=["admin"])
def set_league_lock(
    db: DbSession, _: AdminAccess, locked: bool, league_id: str | None = None
) -> dict[str, Any]:
    selected = current_league(db, league_id)
    league = db.scalar(select(League).where(League.id == selected.id).with_for_update())
    assert league is not None
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    if locked and draft is not None and draft.status == "ACTIVE":
        DraftService(db).pause(league.id)
    league.locked = locked
    if locked:
        league.status = LeagueStatus.LOCKED.value
    elif (league.settings or {}).get("champion_team_id"):
        league.status = LeagueStatus.COMPLETE.value
    elif league.current_week in (league.settings or {}).get("playoff_weeks", []):
        league.status = LeagueStatus.PLAYOFFS.value
    elif draft is not None and draft.status == "COMPLETED":
        league.status = LeagueStatus.REGULAR_SEASON.value
    elif draft is not None and draft.status in {"ACTIVE", "PAUSED"}:
        league.status = LeagueStatus.DRAFTING.value
    else:
        league.status = LeagueStatus.PRE_DRAFT.value
    emit_event(db, league.id, "LEAGUE_LOCKED" if locked else "LEAGUE_UNLOCKED")
    db.commit()
    return serialize(league)


@router.put("/admin/teams/{team_id}/lineup", tags=["admin", "lineups"])
def set_lineup(
    team_id: str,
    payload: LineupSetRequest,
    db: DbSession,
    _: AdminAccess,
    week: int | None = None,
) -> dict[str, Any]:
    team = db.get(Team, team_id)
    if not team:
        raise NotFoundError("team", team_id)
    league = db.get(League, team.league_id)
    assert league is not None
    target_week = week or max(1, league.current_week)
    assignments = RosterService(db).set_lineup(team_id, payload.lineup)
    decision = LineupDecision(
        league_id=league.id,
        team_id=team_id,
        week=target_week,
        lineup=payload.lineup,
        public_reasoning=payload.public_reasoning,
        source="ADMIN",
    )
    db.add(decision)
    emit_event(
        db,
        league.id,
        "LINEUP_CHANGED",
        aggregate_type="TEAM",
        aggregate_id=team_id,
        team_id=team_id,
        data={"week": target_week, "lineup": payload.lineup},
        commentary=payload.public_reasoning,
    )
    db.commit()
    return {"decision": serialize(decision), "assignments": serialize(assignments)}


@router.post("/free-agents/add", tags=["transactions"])
def free_agent_add(
    payload: AddDropRequest, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    return _add_drop(db, league, payload, bypass_waivers=False)


@router.post("/admin/rosters/add-drop", tags=["admin"])
def commissioner_add_drop(
    payload: AddDropRequest, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    return _add_drop(db, league, payload, bypass_waivers=True)


def _add_drop(
    db: DbSession,
    league: League,
    payload: AddDropRequest,
    *,
    bypass_waivers: bool,
) -> dict[str, Any]:
    assignment, records = add_free_agent(
        db,
        league_id=league.id,
        team_id=payload.team_id,
        add_player_id=payload.add_player_id,
        drop_player_id=payload.drop_player_id,
        week=league.current_week,
        idempotency_key=payload.idempotency_key,
        bypass_waivers=bypass_waivers,
    )
    emit_event(
        db,
        league.id,
        "PLAYER_ADDED",
        aggregate_type="TEAM",
        aggregate_id=payload.team_id,
        team_id=payload.team_id,
        data={"player_id": payload.add_player_id, "drop_player_id": payload.drop_player_id},
    )
    db.commit()
    return {"assignment": serialize(assignment), "transactions": serialize(records)}


@router.post("/admin/rosters/drop", tags=["admin"])
def force_drop(
    payload: DropRequest, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    team = db.scalar(select(Team).where(Team.id == payload.team_id, Team.league_id == league.id))
    if team is None:
        raise NotFoundError("Team", payload.team_id)
    RosterService(db).drop_player(payload.team_id, payload.player_id)
    record = create_transaction(
        db,
        league_id=league.id,
        team_id=payload.team_id,
        player_id=payload.player_id,
        transaction_type="COMMISSIONER_DROP",
        week=league.current_week,
        idempotency_key=payload.idempotency_key,
        details={"source": "ADMIN"},
    )
    emit_event(
        db,
        league.id,
        "PLAYER_DROPPED",
        team_id=payload.team_id,
        data={"player_id": payload.player_id},
    )
    db.commit()
    return {"removed_player_id": payload.player_id, "transaction": serialize(record)}


@router.post("/admin/waivers/periods", tags=["admin", "waivers"])
def create_waiver_period(
    payload: WaiverPeriodRequest,
    db: DbSession,
    _: AdminAccess,
    app_settings: AppSettings,
    league_id: str | None = None,
) -> dict[str, Any]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    existing = db.scalar(
        select(WaiverPeriod).where(
            WaiverPeriod.league_id == league.id,
            WaiverPeriod.season == league.nfl_season,
            WaiverPeriod.week == payload.week,
        )
    )
    if existing:
        return serialize(existing)
    period = WaiverPeriod(
        league_id=league.id,
        season=league.nfl_season,
        week=payload.week,
        deadline_at=payload.deadline_at,
        processing_at=payload.deadline_at
        + timedelta(minutes=app_settings.waiver_processing_grace_minutes),
        status="OPEN",
    )
    db.add(period)
    db.commit()
    return serialize(period)


@router.post("/waivers/{waiver_period_id}/claims", tags=["waivers"])
def create_waiver_claims(
    waiver_period_id: str,
    payload: WaiverClaimsRequest,
    db: DbSession,
    _: AdminAccess,
) -> list[dict[str, Any]]:
    claims = submit_claims(
        db,
        waiver_period_id=waiver_period_id,
        team_id=payload.team_id,
        claims=payload.claims,
        public_reasoning=payload.public_reasoning,
    )
    period = db.get(WaiverPeriod, waiver_period_id)
    assert period is not None
    emit_event(
        db,
        period.league_id,
        "WAIVER_SUBMITTED",
        team_id=payload.team_id,
        data={"period_id": period.id, "claims": len(claims)},
        commentary=payload.public_reasoning,
        visibility="PRIVATE",
    )
    db.commit()
    return serialize(claims)


@router.get("/admin/waivers/{waiver_period_id}/claims", tags=["admin", "waivers"])
def get_admin_waiver_claims(
    waiver_period_id: str,
    db: DbSession,
    _: AdminAccess,
) -> list[dict[str, Any]]:
    period = db.get(WaiverPeriod, waiver_period_id)
    if period is None:
        raise NotFoundError("WaiverPeriod", waiver_period_id)
    claims = db.scalars(
        select(WaiverClaim)
        .where(WaiverClaim.waiver_period_id == waiver_period_id)
        .order_by(WaiverClaim.team_id, WaiverClaim.priority)
    ).all()
    return serialize(claims)


@router.post("/admin/waivers/{waiver_period_id}/process", tags=["admin", "waivers"])
def run_waivers(
    waiver_period_id: str,
    db: DbSession,
    _: AdminAccess,
    idempotency_key: str | None = None,
) -> list[dict[str, Any]]:
    claims = process_waivers(
        db,
        waiver_period_id=waiver_period_id,
        idempotency_key=idempotency_key or f"admin:{waiver_period_id}",
    )
    period = db.get(WaiverPeriod, waiver_period_id)
    assert period is not None
    emit_event(
        db,
        period.league_id,
        "WAIVER_PROCESSED",
        aggregate_type="WAIVER_PERIOD",
        aggregate_id=period.id,
        data={"winners": [claim.id for claim in claims if claim.status == "WON"]},
    )
    db.commit()
    return serialize(claims)


@router.post("/trades", tags=["trades"])
def create_trade(
    payload: TradeProposalRequest, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=payload.from_team_id,
        recipient_team_id=payload.to_team_id,
        send_player_ids=[asset.id for asset in payload.send],
        receive_player_ids=[asset.id for asset in payload.receive],
        message=payload.message,
        public_reasoning=payload.public_reasoning,
        expires_at=payload.expires_at,
    )
    emit_event(
        db,
        league.id,
        "TRADE_PROPOSED",
        aggregate_type="TRADE",
        aggregate_id=thread.id,
        team_id=payload.from_team_id,
        data={"offer_id": offer.id, "to_team_id": payload.to_team_id},
        commentary=payload.public_reasoning,
    )
    db.commit()
    return {"thread": serialize(thread), "offer": serialize(offer)}


@router.post("/trades/offers/{offer_id}/counter", tags=["trades"])
def trade_counter(
    offer_id: str, payload: TradeCounterRequest, request: Request, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    thread, offer = counter_trade(
        db,
        offer_id=offer_id,
        countering_team_id=payload.proposer_team_id,
        send_player_ids=[asset.id for asset in payload.send],
        receive_player_ids=[asset.id for asset in payload.receive],
        message=payload.message,
        public_reasoning=payload.public_reasoning,
        max_rounds=request.app.state.settings.max_trade_negotiation_rounds,
    )
    emit_event(
        db,
        thread.league_id,
        "TRADE_COUNTERED",
        aggregate_type="TRADE",
        aggregate_id=thread.id,
        team_id=payload.proposer_team_id,
        data={"offer_id": offer.id},
        commentary=payload.public_reasoning,
    )
    db.commit()
    return {"thread": serialize(thread), "offer": serialize(offer)}


@router.post("/trades/offers/{offer_id}/accept", tags=["trades"])
def trade_accept(
    offer_id: str, payload: TradeActionRequest, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    thread = accept_trade(db, offer_id=offer_id, accepting_team_id=payload.team_id)
    emit_event(
        db,
        thread.league_id,
        "TRADE_ACCEPTED",
        aggregate_type="TRADE",
        aggregate_id=thread.id,
        team_id=payload.team_id,
        commentary=payload.message,
    )
    db.commit()
    return serialize(thread)


@router.post("/trades/offers/{offer_id}/reject", tags=["trades"])
def trade_reject(
    offer_id: str, payload: TradeActionRequest, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    thread = reject_trade(db, offer_id=offer_id, rejecting_team_id=payload.team_id)
    emit_event(
        db,
        thread.league_id,
        "TRADE_REJECTED",
        aggregate_type="TRADE",
        aggregate_id=thread.id,
        team_id=payload.team_id,
        commentary=payload.message,
    )
    db.commit()
    return serialize(thread)


@router.post("/trades/offers/{offer_id}/cancel", tags=["trades"])
def trade_cancel(
    offer_id: str, payload: TradeActionRequest, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    thread = cancel_trade(db, offer_id=offer_id, cancelling_team_id=payload.team_id)
    emit_event(
        db,
        thread.league_id,
        "TRADE_CANCELLED",
        aggregate_type="TRADE",
        aggregate_id=thread.id,
        team_id=payload.team_id,
        commentary=payload.message,
    )
    db.commit()
    return serialize(thread)


@router.post("/admin/stats/load", tags=["admin", "scoring"])
def load_stats(
    payload: StatsLoadRequest, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    scores = []
    for item in payload.stats:
        existing = db.scalar(
            select(PlayerWeekStat).where(
                PlayerWeekStat.player_id == item.player_id,
                PlayerWeekStat.season == league.nfl_season,
                PlayerWeekStat.week == payload.week,
            )
        )
        if existing:
            existing.raw_stats = item.stats
            existing.provider = payload.provider
        else:
            db.add(
                PlayerWeekStat(
                    player_id=item.player_id,
                    season=league.nfl_season,
                    week=payload.week,
                    provider=payload.provider,
                    raw_stats=item.stats,
                )
            )
        scores.append(
            persist_player_score(
                db,
                league_id=league.id,
                player_id=item.player_id,
                season=league.nfl_season,
                week=payload.week,
                raw_stats=item.stats,
                scoring_config=league.scoring_config,
            )
        )
    emit_event(
        db, league.id, "SCORING_RECALCULATED", data={"week": payload.week, "players": len(scores)}
    )
    db.commit()
    return serialize(scores)


@router.post("/admin/scoring/recalculate", tags=["admin", "scoring"])
def recalculate_scoring(
    db: DbSession, _: AdminAccess, week: int, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    stats = db.scalars(
        select(PlayerWeekStat).where(
            PlayerWeekStat.season == league.nfl_season, PlayerWeekStat.week == week
        )
    ).all()
    scores = [
        persist_player_score(
            db,
            league_id=league.id,
            player_id=stat.player_id,
            season=league.nfl_season,
            week=week,
            raw_stats=stat.raw_stats,
            scoring_config=league.scoring_config,
        )
        for stat in stats
    ]
    db.commit()
    return serialize(scores)


@router.post("/admin/weeks/{week}/calculate", tags=["admin", "competition"])
def calculate_week(
    week: int, db: DbSession, _: AdminAccess, complete: bool = False, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    matchups = db.scalars(
        select(Matchup).where(Matchup.league_id == league.id, Matchup.week == week)
    ).all()
    results = []
    for matchup in matchups:
        results.append(
            serialize(complete_matchup(db, matchup_id=matchup.id, season=league.nfl_season))
            if complete
            else serialize(calculate_matchup(db, matchup_id=matchup.id, season=league.nfl_season))
        )
    if complete:
        emit_event(db, league.id, "WEEK_COMPLETED", data={"week": week})
    db.commit()
    return results


@router.post("/admin/weeks/{week}/advance", tags=["admin", "competition"])
def advance_week(
    week: int, db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    if week < league.current_week or week > 18:
        raise ConflictError(
            "INVALID_WEEK", "Week advancement must move forward within the NFL season."
        )
    league.current_week = week
    fantasy_week = db.scalar(
        select(FantasyWeek).where(FantasyWeek.league_id == league.id, FantasyWeek.week == week)
    )
    if not fantasy_week:
        fantasy_week = FantasyWeek(
            league_id=league.id,
            week=week,
            status="ACTIVE",
            is_playoff=week in league.settings.get("playoff_weeks", []),
        )
        db.add(fantasy_week)
    else:
        fantasy_week.status = "ACTIVE"
    if week in league.settings.get("playoff_weeks", []):
        league.status = LeagueStatus.PLAYOFFS.value
    emit_event(db, league.id, "WEEK_STARTED", data={"week": week})
    db.commit()
    return {"league": serialize(league), "week": serialize(fantasy_week)}


@router.get("/admin/playoffs/seeds", tags=["admin", "competition"])
def get_playoff_seeds(
    db: DbSession, _: AdminAccess, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    return playoff_seeds(
        db, league_id=league.id, team_count=int(league.settings.get("playoff_team_count", 4))
    )


@router.post("/admin/playoffs/seed", tags=["admin", "competition"])
def create_playoffs(
    db: DbSession, _: AdminAccess, league_id: str | None = None
) -> list[dict[str, Any]]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    matchups = seed_playoffs(db, league_id=league.id)
    db.commit()
    return serialize(matchups)


@router.post("/admin/playoffs/advance", tags=["admin", "competition"])
def advance_playoff_bracket(
    db: DbSession, _: AdminAccess, league_id: str | None = None
) -> dict[str, Any]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    matchup = advance_playoffs(db, league_id=league.id)
    db.commit()
    return serialize(matchup)


@router.post("/admin/nfl/sync", tags=["admin", "nfl"])
async def sync_nfl(
    db: DbSession,
    _: AdminAccess,
    category: str = "players",
    week: int | None = None,
    league_id: str | None = None,
) -> dict[str, Any]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    provider = NflverseProvider() if category in {"schedule", "stats"} else SleeperProvider()
    try:
        service = NFLDataSyncService(db, provider)
        if category == "players":
            result = await service.sync_players(league.nfl_season)
        elif category == "injuries":
            result = await service.sync_injuries(
                league.nfl_season, week or max(1, league.current_week)
            )
        elif category == "schedule":
            result = await service.sync_schedule(league.nfl_season)
        elif category == "stats":
            result = await service.sync_week_stats(
                league.nfl_season, week or max(1, league.current_week)
            )
        else:
            raise ConflictError(
                "INVALID_SYNC_CATEGORY", "category must be players, injuries, schedule, or stats"
            )
        return serialize(result)
    finally:
        await provider.aclose()


@router.post("/admin/decisions/trigger", tags=["admin", "llm"])
async def trigger_decision(
    payload: TriggerDecisionRequest, request: Request, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    team = db.get(Team, payload.team_id)
    if not team:
        raise NotFoundError("team", payload.team_id)
    ensure_league_unlocked(db, team.league_id)
    db.commit()
    if payload.decision_type == "DRAFT":
        request.app.state.draft_runner.start(team.league_id)
        return {"status": "draft runner triggered", "team_id": team.id}
    response_models: dict[str, type[BaseModel]] = {
        "WAIVER": WaiverDecision,
        "LINEUP": LineupDecisionResponse,
        "TRADE": TradeProposalDecision,
        "MEMORY": MemorySummary,
        "COMMENTARY": MemorySummary,
    }
    response_model = response_models[payload.decision_type]
    prompt = build_prompt(payload.decision_type, payload.context)
    llm_request = LLMRequest(
        league_id=team.league_id,
        team_id=team.id,
        model=team.model_identifier,
        decision_type=payload.decision_type,
        prompt_version=prompt.version,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        response_model=response_model,
        reasoning_effort=(team.reasoning_config or {}).get("effort"),
        metadata={"context": payload.context},
    )
    service = LLMInvocationService(
        db,
        request.app.state.llm_provider,
        daily_budget_usd=request.app.state.settings.openrouter_daily_budget_usd,
        season_budget_usd=request.app.state.settings.openrouter_season_budget_usd,
        max_single_request_usd=request.app.state.settings.openrouter_max_single_request_usd,
    )
    result = await service.invoke(llm_request)
    return {
        "decision": result.parsed.model_dump(mode="json"),
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "cost_usd": result.cost_usd,
            "request_id": result.request_id,
        },
    }


@router.post("/admin/decisions/{run_id}/retry", tags=["admin", "llm"])
async def retry_failed_decision(
    run_id: str,
    request: Request,
    db: DbSession,
    _: AdminAccess,
) -> dict[str, Any]:
    run = db.get(LLMRun, run_id)
    if run is None:
        raise NotFoundError("LLMRun", run_id)
    if run.success:
        raise ConflictError("DECISION_ALREADY_SUCCEEDED", "Only a failed decision can be retried.")
    team = db.get(Team, run.team_id)
    if team is None:
        raise NotFoundError("team", run.team_id)
    ensure_league_unlocked(db, run.league_id)
    if run.decision_type == "DRAFT":
        draft = db.scalar(select(Draft).where(Draft.league_id == run.league_id))
        if draft is None or draft.status not in {"ACTIVE", "PAUSED"}:
            raise ConflictError(
                "DRAFT_NOT_RETRYABLE", "The draft must be active or paused to retry a pick."
            )
        if draft.status == "PAUSED":
            DraftService(db).resume(run.league_id)
        db.commit()
        request.app.state.draft_runner.start(run.league_id)
        return {"status": "draft retry started", "prior_run_id": run.id}

    response_models: dict[str, type[BaseModel]] = {
        "WAIVER": WaiverDecision,
        "FREE_AGENT": WaiverDecision,
        "LINEUP": LineupDecisionResponse,
        "TRADE": TradeProposalDecision,
        "TRADE_PROPOSAL": TradeProposalDecision,
        "TRADE_RESPONSE": TradeResponseDecision,
        "MEMORY": MemorySummary,
        "COMMENTARY": MemorySummary,
    }
    response_model = response_models.get(run.decision_type)
    if response_model is None:
        raise ConflictError(
            "DECISION_NOT_RETRYABLE", f"Unsupported decision type {run.decision_type!r}."
        )
    payload = run.request_payload or {}
    if not isinstance(payload.get("system_prompt"), str) or not isinstance(
        payload.get("user_prompt"), str
    ):
        raise ConflictError(
            "RETRY_CONTEXT_MISSING", "The failed run does not contain a replayable prompt."
        )
    metadata_value = payload.get("metadata")
    replay_metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    llm_request = LLMRequest(
        league_id=run.league_id,
        team_id=run.team_id,
        model=run.model,
        decision_type=run.decision_type,
        prompt_version=run.prompt_version,
        system_prompt=payload["system_prompt"],
        user_prompt=payload["user_prompt"],
        response_model=response_model,
        reasoning_effort=payload.get("reasoning_effort"),
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        metadata=replay_metadata,
    )
    db.commit()
    result = await LLMInvocationService(
        db,
        request.app.state.llm_provider,
        daily_budget_usd=request.app.state.settings.openrouter_daily_budget_usd,
        season_budget_usd=request.app.state.settings.openrouter_season_budget_usd,
        max_single_request_usd=request.app.state.settings.openrouter_max_single_request_usd,
    ).invoke(llm_request)
    return {
        "prior_run_id": run_id,
        "decision": result.parsed.model_dump(mode="json"),
        "request_id": result.request_id,
    }


@router.post("/admin/lineups/review", tags=["admin", "llm", "lineups"])
async def run_lineup_review(
    request: Request,
    db: DbSession,
    _: AdminAccess,
    week: int | None = None,
    league_id: str | None = None,
) -> dict[str, str]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    return await request.app.state.manager_automation.set_all_lineups(
        league.id, week or max(1, league.current_week)
    )


@router.post("/admin/free-agents/review", tags=["admin", "llm", "transactions"])
async def run_free_agent_review(
    request: Request,
    db: DbSession,
    _: AdminAccess,
    week: int | None = None,
    league_id: str | None = None,
) -> dict[str, str]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    db.commit()
    return await request.app.state.manager_automation.review_free_agents(
        league.id, week or max(1, league.current_week)
    )


@router.post("/admin/trades/review", tags=["admin", "llm", "trades"])
async def run_trade_review(
    request: Request,
    db: DbSession,
    _: AdminAccess,
    league_id: str | None = None,
) -> dict[str, str]:
    league = current_league(db, league_id)
    ensure_league_unlocked(db, league.id)
    db.commit()
    return await request.app.state.manager_automation.review_trades(league.id)


@router.post("/admin/waivers/{waiver_period_id}/collect", tags=["admin", "llm", "waivers"])
async def collect_manager_waivers(
    waiver_period_id: str,
    request: Request,
    db: DbSession,
    _: AdminAccess,
) -> dict[str, str]:
    period = db.get(WaiverPeriod, waiver_period_id)
    if period is None:
        raise NotFoundError("WaiverPeriod", waiver_period_id)
    ensure_league_unlocked(db, period.league_id)
    db.commit()
    return await request.app.state.manager_automation.collect_waiver_claims(waiver_period_id)


@router.post("/admin/manager-memory/{team_id}/reset", tags=["admin", "llm"])
def reset_manager_memory(
    team_id: str, request: Request, db: DbSession, _: AdminAccess
) -> dict[str, Any]:
    team = db.get(Team, team_id)
    if not team:
        raise NotFoundError("team", team_id)
    ensure_league_unlocked(db, team.league_id)
    memory = request.app.state.memory_service_factory(db).reset(team.league_id, team_id)
    return serialize(memory)
