from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.contracts import LLMProvider, LLMRequest
from app.agents.memory import ManagerMemoryService
from app.agents.prompts import build_prompt
from app.agents.service import LLMInvocationService
from app.agents.tools import LeagueToolbox
from app.core.config import Settings
from app.models.base import utcnow
from app.models.entities import (
    League,
    LineupDecision,
    Player,
    RosterAssignment,
    Team,
    TradeAsset,
    TradeOffer,
    TradeThread,
    WaiverPeriod,
)
from app.schemas.decisions import (
    LineupDecisionResponse,
    TradeProposalDecision,
    TradeResponseDecision,
    WaiverDecision,
)
from app.services.events import emit_event
from app.services.guards import ensure_league_unlocked
from app.services.rosters import RosterService
from app.services.trades import accept_trade, counter_trade, propose_trade, reject_trade
from app.services.transactions import add_free_agent
from app.services.waivers import submit_claims

logger = logging.getLogger(__name__)


class ManagerAutomation:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings

    async def set_all_lineups(self, league_id: str, week: int) -> dict[str, str]:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            team_ids = list(
                db.scalars(
                    select(Team.id).where(Team.league_id == league_id).order_by(Team.draft_position)
                )
            )
            db.commit()
        results: dict[str, str] = {}
        for team_id in team_ids:
            try:
                await self._set_team_lineup(league_id, team_id, week)
                results[team_id] = "COMPLETE"
            except Exception as exc:
                results[team_id] = f"FAILED: {exc}"
                logger.exception(
                    "lineup_decision_failed",
                    extra={"league_id": league_id, "team_id": team_id, "week": week},
                )
        return results

    async def _set_team_lineup(self, league_id: str, team_id: str, week: int) -> None:
        with self.session_factory() as db:
            league = db.get(League, league_id)
            team = db.get(Team, team_id)
            if not league or not team:
                raise ValueError("league or team does not exist")
            rows = db.execute(
                select(RosterAssignment, Player)
                .join(Player, Player.id == RosterAssignment.player_id)
                .where(RosterAssignment.team_id == team_id)
            ).all()
            roster_service = RosterService(db)
            context = {
                "week": week,
                "team_id": team_id,
                "lineup_slots": _lineup_slots(league.roster_config),
                "current_lineup": {
                    assignment.position_slot: player.id
                    for assignment, player in rows
                    if assignment.slot_type == "STARTER" and assignment.position_slot
                },
                "locked_slots": {
                    assignment.position_slot: player.id
                    for assignment, player in rows
                    if assignment.slot_type == "STARTER"
                    and assignment.position_slot
                    and roster_service.is_player_locked(league, player)
                },
                "roster": [
                    {
                        "player_id": player.id,
                        "name": player.full_name,
                        "position": player.position,
                        "injury_status": player.injury_status,
                        "bye_week": player.bye_week,
                        "projection": (player.metadata_json or {}).get("projection"),
                        "rank": (player.metadata_json or {}).get("rank", 10**9),
                        "locked": roster_service.is_player_locked(league, player),
                    }
                    for _, player in rows
                ],
            }
            prompt = build_prompt("lineup", context)
            request = _request(
                team,
                league_id,
                "LINEUP",
                prompt.version,
                prompt.system,
                prompt.user,
                LineupDecisionResponse,
                context,
                self.settings,
            )
            result = await self._invocation(db).invoke(request)
            decision = LineupDecisionResponse.model_validate(result.parsed)
        with self.session_factory() as db:
            RosterService(db).set_lineup(team_id, decision.lineup)
            record = LineupDecision(
                league_id=league_id,
                team_id=team_id,
                week=week,
                lineup=decision.lineup,
                public_reasoning=decision.public_reasoning,
                source="MANAGER",
            )
            db.add(record)
            emit_event(
                db,
                league_id,
                "LINEUP_CHANGED",
                aggregate_type="TEAM",
                aggregate_id=team_id,
                team_id=team_id,
                data={"week": week, "lineup": decision.lineup},
                commentary=decision.public_reasoning,
            )
            db.commit()
        self._record_memory(
            league_id,
            team_id,
            f"Set week {week} lineup: {decision.public_reasoning}",
            valued_player_ids=list(decision.lineup.values()),
        )

    async def collect_waiver_claims(self, waiver_period_id: str) -> dict[str, str]:
        with self.session_factory() as db:
            period = db.get(WaiverPeriod, waiver_period_id)
            if not period or period.status != "OPEN":
                return {}
            ensure_league_unlocked(db, period.league_id)
            teams = list(
                db.scalars(
                    select(Team)
                    .where(Team.league_id == period.league_id)
                    .order_by(Team.draft_position)
                )
            )
            db.commit()

        async def collect(team_id: str) -> tuple[str, str]:
            try:
                await self._collect_team_waivers(period.id, team_id)
                return team_id, "COMPLETE"
            except Exception as exc:
                logger.exception(
                    "waiver_decision_failed",
                    extra={"period_id": period.id, "team_id": team_id},
                )
                return team_id, f"FAILED: {exc}"

        pairs = await asyncio.gather(*(collect(team.id) for team in teams))
        return dict(pairs)

    async def _collect_team_waivers(self, period_id: str, team_id: str) -> None:
        collection_started_at = utcnow()
        with self.session_factory() as db:
            period = db.get(WaiverPeriod, period_id)
            team = db.get(Team, team_id)
            if not period or not team:
                raise ValueError("waiver period or team does not exist")
            owned_ids = select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == period.league_id
            )
            available = list(
                db.scalars(
                    select(Player)
                    .where(Player.active.is_(True), Player.id.not_in(owned_ids))
                    .limit(100)
                )
            )
            roster = db.execute(
                select(RosterAssignment, Player)
                .join(Player, Player.id == RosterAssignment.player_id)
                .where(RosterAssignment.team_id == team_id)
            ).all()
            context = {
                "waiver_period_id": period.id,
                "week": period.week,
                "waiver_priority": team.waiver_priority,
                "waiver_rule": "CONTINUAL_ROLLING",
                "available_players": [_context_player(player) for player in available],
                "droppable_players": [_context_player(player) for _, player in roster],
            }
            prompt = build_prompt("waiver", context)
            request = _request(
                team,
                period.league_id,
                "WAIVER",
                prompt.version,
                prompt.system,
                prompt.user,
                WaiverDecision,
                context,
                self.settings,
            )
            result = await self._invocation(db).invoke(request)
            decision = WaiverDecision.model_validate(result.parsed)
        with self.session_factory() as db:
            claims = submit_claims(
                db,
                waiver_period_id=period_id,
                team_id=team_id,
                claims=decision.claims,
                public_reasoning=decision.public_reasoning,
                collection_started_at=collection_started_at,
            )
            emit_event(
                db,
                period.league_id,
                "WAIVER_SUBMITTED",
                team_id=team_id,
                data={"period_id": period_id, "claims": len(claims)},
                commentary=decision.public_reasoning,
                visibility="PRIVATE",
            )
            db.commit()
        self._record_memory(
            period.league_id,
            team_id,
            f"Submitted week {period.week} waivers: {decision.public_reasoning}",
            valued_player_ids=[claim.add_player_id for claim in decision.claims],
        )

    async def review_free_agents(self, league_id: str, week: int) -> dict[str, str]:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            team_ids = list(
                db.scalars(
                    select(Team.id).where(Team.league_id == league_id).order_by(Team.draft_position)
                )
            )
            db.commit()
        results: dict[str, str] = {}
        for team_id in team_ids:
            try:
                changed = await self._review_team_free_agents(league_id, team_id, week)
                results[team_id] = "COMPLETE" if changed else "PASS"
            except Exception as exc:
                results[team_id] = f"FAILED: {exc}"
                logger.exception(
                    "free_agent_decision_failed",
                    extra={"league_id": league_id, "team_id": team_id, "week": week},
                )
        return results

    async def _review_team_free_agents(self, league_id: str, team_id: str, week: int) -> bool:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            team = db.get(Team, team_id)
            if team is None:
                raise ValueError("team does not exist")
            toolbox = LeagueToolbox(db, league_id, team_id)
            context = {
                "week": week,
                "available_players": toolbox.get_available_players(limit=100),
                "droppable_players": toolbox.get_roster(),
                "decision_mode": "instant_free_agency",
            }
            prompt = build_prompt("waiver", context)
            request = _request(
                team,
                league_id,
                "FREE_AGENT",
                "free_agent_v1",
                prompt.system,
                prompt.user,
                WaiverDecision,
                context,
                self.settings,
            )
            result = await self._invocation(db).invoke(request)
            decision = WaiverDecision.model_validate(result.parsed)
        if not decision.claims:
            return False
        claim = decision.claims[0]
        with self.session_factory() as db:
            assignment, _ = add_free_agent(
                db,
                league_id=league_id,
                team_id=team_id,
                add_player_id=claim.add_player_id,
                drop_player_id=claim.drop_player_id,
                week=week,
                idempotency_key=(
                    f"manager-free-agent:{league_id}:{week}:{team_id}:{claim.add_player_id}"
                ),
            )
            emit_event(
                db,
                league_id,
                "PLAYER_ADDED",
                aggregate_type="TEAM",
                aggregate_id=team_id,
                team_id=team_id,
                data={
                    "player_id": assignment.player_id,
                    "drop_player_id": claim.drop_player_id,
                    "source": "MANAGER_FREE_AGENT_REVIEW",
                },
                commentary=decision.public_reasoning,
            )
            db.commit()
        self._record_memory(
            league_id,
            team_id,
            f"Added free agent {claim.add_player_id}: {decision.public_reasoning}",
            valued_player_ids=[claim.add_player_id],
        )
        return True

    async def review_trades(self, league_id: str) -> dict[str, str]:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            pending_offer_ids = list(
                db.scalars(
                    select(TradeOffer.id)
                    .join(TradeThread, TradeThread.id == TradeOffer.thread_id)
                    .where(
                        TradeThread.league_id == league_id,
                        TradeThread.status.in_(("PROPOSED", "COUNTERED")),
                        TradeOffer.status.in_(("PROPOSED", "COUNTERED")),
                        TradeOffer.sequence == TradeThread.negotiation_rounds,
                    )
                    .order_by(TradeOffer.created_at)
                )
            )
            team_ids = list(
                db.scalars(
                    select(Team.id).where(Team.league_id == league_id).order_by(Team.draft_position)
                )
            )
            db.commit()
        results: dict[str, str] = {}
        for offer_id in pending_offer_ids:
            try:
                action = await self._respond_to_trade(league_id, offer_id)
                results[f"offer:{offer_id}"] = action
            except Exception as exc:
                results[f"offer:{offer_id}"] = f"FAILED: {exc}"
                logger.exception(
                    "trade_response_failed",
                    extra={"league_id": league_id, "offer_id": offer_id},
                )
        for team_id in team_ids:
            try:
                proposed = await self._consider_trade_proposal(league_id, team_id)
                results[f"team:{team_id}"] = "PROPOSED" if proposed else "PASS"
            except Exception as exc:
                results[f"team:{team_id}"] = f"FAILED: {exc}"
                logger.exception(
                    "trade_proposal_failed",
                    extra={"league_id": league_id, "team_id": team_id},
                )
        return results

    async def _respond_to_trade(self, league_id: str, offer_id: str) -> str:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            offer = db.get(TradeOffer, offer_id)
            if offer is None:
                raise ValueError("trade offer does not exist")
            team = db.get(Team, offer.recipient_team_id)
            if team is None:
                raise ValueError("trade recipient does not exist")
            assets = list(db.scalars(select(TradeAsset).where(TradeAsset.offer_id == offer.id)))
            toolbox = LeagueToolbox(db, league_id, team.id)
            context = {
                "offer": {
                    "offer_id": offer.id,
                    "proposer_team_id": offer.proposer_team_id,
                    "recipient_team_id": offer.recipient_team_id,
                    "message": offer.message,
                    "assets": [
                        {
                            "player_id": asset.player_id,
                            "from_team_id": asset.from_team_id,
                            "to_team_id": asset.to_team_id,
                        }
                        for asset in assets
                    ],
                },
                "my_roster": toolbox.get_roster(),
                "other_roster": toolbox.get_roster(offer.proposer_team_id),
                "standings": toolbox.get_standings(),
                "max_negotiation_rounds": self.settings.max_trade_negotiation_rounds,
            }
            prompt = build_prompt("trade", context)
            request = _request(
                team,
                league_id,
                "TRADE_RESPONSE",
                "trade_response_v1",
                prompt.system,
                prompt.user,
                TradeResponseDecision,
                context,
                self.settings,
            )
            result = await self._invocation(db).invoke(request)
            decision = TradeResponseDecision.model_validate(result.parsed)
        with self.session_factory() as db:
            if decision.action == "accept":
                thread = accept_trade(db, offer_id=offer_id, accepting_team_id=team.id)
                event_type = "TRADE_ACCEPTED"
            elif decision.action == "counter":
                thread, _ = counter_trade(
                    db,
                    offer_id=offer_id,
                    countering_team_id=team.id,
                    send_player_ids=[asset.id for asset in decision.send],
                    receive_player_ids=[asset.id for asset in decision.receive],
                    message=decision.message,
                    public_reasoning=decision.public_reasoning,
                    max_rounds=self.settings.max_trade_negotiation_rounds,
                )
                event_type = "TRADE_COUNTERED"
            else:
                thread = reject_trade(db, offer_id=offer_id, rejecting_team_id=team.id)
                event_type = "TRADE_REJECTED"
            emit_event(
                db,
                league_id,
                event_type,
                aggregate_type="TRADE",
                aggregate_id=thread.id,
                team_id=team.id,
                commentary=decision.public_reasoning,
            )
            db.commit()
        self._record_memory(
            league_id,
            team.id,
            f"Trade response {decision.action}: {decision.public_reasoning}",
            valued_player_ids=[asset.id for asset in decision.receive],
        )
        return decision.action.upper()

    async def _consider_trade_proposal(self, league_id: str, team_id: str) -> bool:
        with self.session_factory() as db:
            ensure_league_unlocked(db, league_id)
            if db.scalar(
                select(TradeThread.id).where(
                    TradeThread.league_id == league_id,
                    TradeThread.status.in_(("PROPOSED", "COUNTERED")),
                    (TradeThread.initiator_team_id == team_id)
                    | (TradeThread.recipient_team_id == team_id),
                )
            ):
                db.commit()
                return False
            team = db.get(Team, team_id)
            if team is None:
                raise ValueError("team does not exist")
            toolbox = LeagueToolbox(db, league_id, team_id)
            other_teams = list(
                db.scalars(select(Team).where(Team.league_id == league_id, Team.id != team_id))
            )
            context = {
                "my_roster": toolbox.get_roster(),
                "other_rosters": {other.id: toolbox.get_roster(other.id) for other in other_teams},
                "standings": toolbox.get_standings(),
            }
            prompt = build_prompt("trade", context)
            request = _request(
                team,
                league_id,
                "TRADE_PROPOSAL",
                "trade_proposal_v1",
                prompt.system,
                prompt.user,
                TradeProposalDecision,
                context,
                self.settings,
            )
            result = await self._invocation(db).invoke(request)
            decision = TradeProposalDecision.model_validate(result.parsed)
        if decision.action == "pass":
            return False
        assert decision.to_team_id is not None
        with self.session_factory() as db:
            thread, offer = propose_trade(
                db,
                league_id=league_id,
                proposer_team_id=team_id,
                recipient_team_id=decision.to_team_id,
                send_player_ids=[asset.id for asset in decision.send],
                receive_player_ids=[asset.id for asset in decision.receive],
                message=decision.message,
                public_reasoning=decision.public_reasoning,
            )
            emit_event(
                db,
                league_id,
                "TRADE_PROPOSED",
                aggregate_type="TRADE",
                aggregate_id=thread.id,
                team_id=team_id,
                data={"offer_id": offer.id, "to_team_id": decision.to_team_id},
                commentary=decision.public_reasoning,
            )
            db.commit()
        self._record_memory(
            league_id,
            team_id,
            f"Proposed trade: {decision.public_reasoning}",
            valued_player_ids=[asset.id for asset in decision.receive],
        )
        return True

    def _record_memory(
        self,
        league_id: str,
        team_id: str,
        decision: str,
        *,
        valued_player_ids: list[str] | None = None,
    ) -> None:
        try:
            with self.session_factory() as db:
                ManagerMemoryService(db).record_decision(
                    league_id,
                    team_id,
                    decision,
                    valued_player_ids=valued_player_ids,
                )
        except Exception:
            logger.exception(
                "manager_memory_update_failed",
                extra={"league_id": league_id, "team_id": team_id},
            )

    def _invocation(self, db: Session) -> LLMInvocationService:
        return LLMInvocationService(
            db,
            self.provider,
            daily_budget_usd=self.settings.openrouter_daily_budget_usd,
            season_budget_usd=self.settings.openrouter_season_budget_usd,
            max_single_request_usd=self.settings.openrouter_max_single_request_usd,
        )


def _lineup_slots(roster_config: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    flex = list(roster_config.get("flex_eligible", ["RB", "WR", "TE"]))
    for position, count_value in roster_config.get("starters", {}).items():
        count = int(count_value)
        for index in range(1, count + 1):
            name = position if count == 1 else f"{position}{index}"
            result.append(
                {
                    "slot": name,
                    "eligible_positions": flex if position == "FLEX" else [position],
                }
            )
    return result


def _context_player(player: Player) -> dict[str, Any]:
    return {
        "player_id": player.id,
        "name": player.full_name,
        "position": player.position,
        "rank": (player.metadata_json or {}).get("rank", 10**9),
        "projection": (player.metadata_json or {}).get("projection"),
        "injury_status": player.injury_status,
    }


def _request(
    team: Team,
    league_id: str,
    decision_type: str,
    prompt_version: str,
    system_prompt: str,
    user_prompt: str,
    response_model: Any,
    context: dict[str, Any],
    settings: Settings,
) -> LLMRequest:
    return LLMRequest(
        league_id=league_id,
        team_id=team.id,
        model=team.model_identifier,
        decision_type=decision_type,
        prompt_version=prompt_version,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=response_model,
        reasoning_effort=(team.reasoning_config or {}).get("effort"),
        temperature=settings.openrouter_temperature,
        max_tokens=settings.openrouter_max_tokens,
        metadata={"context": context},
    )
