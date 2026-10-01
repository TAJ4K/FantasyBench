from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.tools import LeagueToolbox
from app.core.config import Settings
from app.models.base import utcnow
from app.models.entities import LeagueEvent, NflGame, Player, PlayerNews, Team, TradeThread


class ScheduledReviews:
    """Reuse completed scheduled reviews across jobs, never manual commissioner requests."""

    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def run(
        self, team_id: str, kind: str, operation: Callable[[], Awaitable[bool | None]],
    ) -> str:
        # The supported single-worker deployment can launch overlapping kickoff jobs.
        async with self._locks.setdefault((team_id, kind), asyncio.Lock()):
            now = utcnow()
            with self.factory() as db:
                team = db.get(Team, team_id)
                if team is None:
                    raise ValueError("team does not exist")
                if kind == "TRADE_PROPOSAL" and db.scalar(select(TradeThread.id).where(
                    TradeThread.league_id == team.league_id,
                    TradeThread.status.in_(("PROPOSED", "COUNTERED")),
                    or_(TradeThread.initiator_team_id == team_id,
                        TradeThread.recipient_team_id == team_id),
                )):
                    return "SKIPPED: pending trade"
                state = self._snapshot(db, team, kind)
                prior = (team.manager_state or {}).get("scheduled_reviews", {}).get(kind, {})
                reviewed_at = _timestamp(prior.get("reviewed_at"))
                if kind == "TRADE_PROPOSAL" and not prior:
                    # Apply the cadence immediately on upgrade using completed actions,
                    # without treating a failed model invocation as a completed review.
                    previous = db.scalar(select(LeagueEvent).where(
                        LeagueEvent.league_id == team.league_id,
                        LeagueEvent.team_id == team.id,
                        LeagueEvent.event_type.in_(("TRADE_REVIEWED", "TRADE_PROPOSED")),
                    ).order_by(LeagueEvent.occurred_at.desc()).limit(1))
                    week_start = db.scalar(select(LeagueEvent.occurred_at).where(
                        LeagueEvent.league_id == team.league_id,
                        LeagueEvent.event_type == "WEEK_STARTED",
                        LeagueEvent.data["week"].as_integer() == team.league.current_week,
                    ).order_by(LeagueEvent.occurred_at.desc()).limit(1))
                    if previous and week_start and previous.occurred_at >= week_start:
                        previous_at = _timestamp(previous.occurred_at.isoformat())
                        if previous_at and timedelta(0) <= now - previous_at < timedelta(
                            hours=self.settings.trade_proposal_interval_hours,
                        ):
                            return "SKIPPED: trade proposal cooldown"
                if reviewed_at is not None and reviewed_at <= now:
                    age = now - reviewed_at
                    same_week = prior.get("week") == state["week"]
                    same_policy = prior.get("policy") == state["policy"]
                    if same_week and same_policy:
                        if kind == "TRADE_PROPOSAL" and age < timedelta(
                            hours=self.settings.trade_proposal_interval_hours,
                        ):
                            return "SKIPPED: trade proposal cooldown"
                        max_age = 24 if kind == "LINEUP" else 168
                        if (
                            age < timedelta(hours=max_age)
                            and prior.get("fingerprint") == _hash(state)
                        ):
                            return "SKIPPED: unchanged review inputs"
            result = await operation()
            with self.factory() as db:
                team = db.scalar(select(Team).where(Team.id == team_id).with_for_update())
                if team is None:
                    raise ValueError("team does not exist")
                if kind == "LINEUP":
                    after = self._snapshot(db, team, kind)
                    # The manager's own slot changes should not invalidate its review.
                    # Keep the original evidence so changes arriving during the call
                    # still trigger a fresh review at the next scheduled opportunity.
                    state["assignments"] = after["assignments"]
                stamps = dict((team.manager_state or {}).get("scheduled_reviews", {}))
                stamps[kind] = {
                    "week": state["week"], "policy": state["policy"],
                    "fingerprint": _hash(state), "reviewed_at": now.isoformat(),
                }
                team.manager_state = {**(team.manager_state or {}), "scheduled_reviews": stamps}
                db.commit()
            return "COMPLETE" if kind == "LINEUP" else ("PROPOSED" if result else "PASS")

    def _snapshot(self, db: Session, team: Team, kind: str) -> dict[str, Any]:
        toolbox = LeagueToolbox(db, team.league_id, team.id)
        league = team.league
        team_ids = (
            list(db.scalars(select(Team.id).where(Team.league_id == league.id).order_by(Team.id)))
            if kind == "TRADE_PROPOSAL" else [team.id]
        )
        rosters = {team_id: toolbox.get_roster(team_id) for team_id in team_ids}
        assignments = []
        nfl_teams = set()
        for team_id, roster in rosters.items():
            roster.sort(key=lambda row: row["player_id"])
            for player in roster:
                if player.get("nfl_team"):
                    nfl_teams.add(player["nfl_team"])
                assignments.append([
                    team_id, player["player_id"], player.pop("slot_type"),
                    player.pop("position_slot"),
                ])
        teammates = list(db.scalars(select(Player).where(
            Player.nfl_team.in_(nfl_teams),
            Player.position.in_(("QB", "RB", "WR", "TE", "K", "DST")),
        ).order_by(Player.id)))
        news = db.scalars(select(PlayerNews).where(or_(
            PlayerNews.player_id.in_([p.id for p in teammates]),
            PlayerNews.player_id.is_(None),
        )).order_by(PlayerNews.id)).all()
        games = db.scalars(select(NflGame).where(
            NflGame.season == league.nfl_season, NflGame.week == league.current_week,
            or_(NflGame.home_team.in_(nfl_teams), NflGame.away_team.in_(nfl_teams)),
        ).order_by(NflGame.id)).all()
        return _stable({
            "policy": [
                "scheduled_review_v1", team.model_identifier, team.reasoning_config,
                self.settings.manager_research_rounds, self.settings.openrouter_temperature,
                self.settings.openrouter_max_tokens, self.settings.trade_max_tokens,
            ],
            "season": league.nfl_season, "week": league.current_week,
            "roster_config": league.roster_config, "scoring": league.scoring_config,
            "rosters": rosters, "assignments": assignments,
            "standings": toolbox.get_standings() if kind == "TRADE_PROPOSAL" else None,
            "teammates": [[p.id, p.nfl_team, p.status, p.active, p.injury_status,
                           p.metadata_json] for p in teammates],
            "news": [[n.id, n.headline, n.summary, n.published_at] for n in news],
            "games": [[g.id, g.home_team, g.away_team, g.kickoff_at] for g in games],
        })


def _stable(value: Any) -> Any:
    # The stats sync refreshes timestamps even when production is identical.
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key != "updated_at"}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
