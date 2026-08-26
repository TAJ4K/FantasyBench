from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.entities import (
    League,
    Matchup,
    Player,
    PlayerNews,
    PlayerWeekStat,
    RosterAssignment,
    Team,
    TradeOffer,
    TradeThread,
    Transaction,
)
from app.services.competition import standings
from app.services.draft import DraftService
from app.services.rosters import RosterService
from app.services.trades import accept_trade, counter_trade, propose_trade, reject_trade
from app.services.transactions import add_free_agent, create_transaction
from app.services.waivers import submit_claims


class LeagueToolbox:
    """Identical, team-scoped information and action surface for every manager.

    Action methods flush but do not commit; orchestration owns the transaction.
    """

    def __init__(self, db: Session, league_id: str, team_id: str) -> None:
        self.db = db
        self.league_id = league_id
        self.team_id = team_id
        team = db.get(Team, team_id)
        if team is None or team.league_id != league_id:
            raise NotFoundError("team", team_id)

    def get_league_state(self) -> dict[str, Any]:
        league = self._league()
        return {
            "id": league.id,
            "status": league.status,
            "current_week": league.current_week,
            "settings": league.settings,
            "roster_config": league.roster_config,
            "scoring_config": league.scoring_config,
        }

    def get_roster(self, team_id: str | None = None) -> list[dict[str, Any]]:
        selected = team_id or self.team_id
        self._league_team(selected)
        rows = self.db.execute(
            select(RosterAssignment, Player)
            .join(Player, Player.id == RosterAssignment.player_id)
            .where(RosterAssignment.team_id == selected)
        ).all()
        return [
            {
                "player_id": player.id,
                "name": player.full_name,
                "position": player.position,
                "nfl_team": player.nfl_team,
                "injury_status": player.injury_status,
                "bye_week": player.bye_week,
                "rank": (player.metadata_json or {}).get("rank", 10**9),
                "projection": (player.metadata_json or {}).get("projection"),
                "slot_type": assignment.slot_type,
                "position_slot": assignment.position_slot,
            }
            for assignment, player in rows
        ]

    def get_available_players(
        self, *, position: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        owned = select(RosterAssignment.player_id).where(
            RosterAssignment.league_id == self.league_id
        )
        query = select(Player).where(Player.active.is_(True), Player.id.not_in(owned))
        if position:
            query = query.where(Player.position == position.upper())
        players = self.db.scalars(query.order_by(Player.full_name).limit(min(limit, 500))).all()
        return [self._player_summary(player) for player in players]

    def get_player(self, player_id: str) -> dict[str, Any]:
        return self._player_summary(self._player(player_id))

    def get_player_recent_stats(self, player_id: str, *, limit: int = 4) -> list[dict[str, Any]]:
        self._player(player_id)
        stats = self.db.scalars(
            select(PlayerWeekStat)
            .where(PlayerWeekStat.player_id == player_id)
            .order_by(PlayerWeekStat.season.desc(), PlayerWeekStat.week.desc())
            .limit(limit)
        ).all()
        return [
            {"season": stat.season, "week": stat.week, "stats": stat.raw_stats} for stat in stats
        ]

    def get_player_season_stats(self, player_id: str, season: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.get_player_recent_stats(player_id, limit=30):
            if row["season"] != season:
                continue
            for key, value in row["stats"].items():
                totals[key] = totals.get(key, 0.0) + float(value)
        return totals

    def get_player_projection(self, player_id: str) -> Any:
        return (self._player(player_id).metadata_json or {}).get("projection")

    def get_player_news(self, player_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        self._player(player_id)
        news = self.db.scalars(
            select(PlayerNews)
            .where(PlayerNews.player_id == player_id)
            .order_by(PlayerNews.published_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "headline": item.headline,
                "summary": item.summary,
                "url": item.url,
                "published_at": item.published_at.isoformat(),
            }
            for item in news
        ]

    def get_injury_status(self, player_id: str) -> dict[str, Any]:
        player = self._player(player_id)
        return {
            "player_id": player.id,
            "status": player.status,
            "injury_status": player.injury_status,
        }

    def get_depth_chart(self, nfl_team: str) -> list[dict[str, Any]]:
        players = self.db.scalars(
            select(Player).where(Player.nfl_team == nfl_team.upper(), Player.active.is_(True))
        ).all()
        return sorted(
            [self._player_summary(player) for player in players],
            key=lambda item: int((item["metadata"] or {}).get("depth_chart_order", 999)),
        )

    def get_matchup(self, *, team_id: str | None = None, week: int) -> dict[str, Any] | None:
        selected = team_id or self.team_id
        self._league_team(selected)
        matchup = self.db.scalar(
            select(Matchup).where(
                Matchup.league_id == self.league_id,
                Matchup.week == week,
                or_(Matchup.home_team_id == selected, Matchup.away_team_id == selected),
            )
        )
        if not matchup:
            return None
        return {
            "id": matchup.id,
            "week": matchup.week,
            "home_team_id": matchup.home_team_id,
            "away_team_id": matchup.away_team_id,
            "home_score": matchup.home_score,
            "away_score": matchup.away_score,
            "status": matchup.status,
        }

    def get_standings(self) -> list[dict[str, Any]]:
        return standings(self.db, league_id=self.league_id)

    def get_transactions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        items = self.db.scalars(
            select(Transaction)
            .where(Transaction.league_id == self.league_id)
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": item.id,
                "team_id": item.team_id,
                "player_id": item.player_id,
                "type": item.transaction_type,
                "week": item.week,
                "details": item.details,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in items
        ]

    def get_trade_offers(self) -> list[dict[str, Any]]:
        offers = self.db.execute(
            select(TradeOffer, TradeThread)
            .join(TradeThread, TradeThread.id == TradeOffer.thread_id)
            .where(
                TradeThread.league_id == self.league_id,
                or_(
                    TradeOffer.proposer_team_id == self.team_id,
                    TradeOffer.recipient_team_id == self.team_id,
                ),
            )
            .order_by(TradeOffer.created_at.desc())
        ).all()
        return [
            {
                "thread_id": thread.id,
                "offer_id": offer.id,
                "status": thread.status,
                "proposer_team_id": offer.proposer_team_id,
                "recipient_team_id": offer.recipient_team_id,
                "message": offer.message,
            }
            for offer, thread in offers
        ]

    def draft_player(self, player_id: str, **decision: Any) -> Any:
        return DraftService(self.db).make_pick(self.league_id, player_id, **decision)

    def set_lineup(self, lineup: dict[str, str]) -> list[RosterAssignment]:
        return RosterService(self.db).set_lineup(self.team_id, lineup)

    def submit_waiver_claims(self, waiver_period_id: str, claims: list[dict[str, Any]]) -> Any:
        return submit_claims(
            self.db,
            waiver_period_id=waiver_period_id,
            team_id=self.team_id,
            claims=claims,
        )

    def add_free_agent(
        self, player_id: str, *, drop_player_id: str | None, idempotency_key: str
    ) -> Any:
        return add_free_agent(
            self.db,
            league_id=self.league_id,
            team_id=self.team_id,
            add_player_id=player_id,
            drop_player_id=drop_player_id,
            idempotency_key=idempotency_key,
        )

    def drop_player(self, player_id: str, *, idempotency_key: str) -> None:
        RosterService(self.db).drop_player(self.team_id, player_id)
        create_transaction(
            self.db,
            league_id=self.league_id,
            team_id=self.team_id,
            player_id=player_id,
            transaction_type="DROP",
            idempotency_key=idempotency_key,
        )

    def propose_trade(
        self, to_team_id: str, send: list[str], receive: list[str], message: str
    ) -> Any:
        return propose_trade(
            self.db,
            league_id=self.league_id,
            proposer_team_id=self.team_id,
            recipient_team_id=to_team_id,
            send_player_ids=send,
            receive_player_ids=receive,
            message=message,
        )

    def counter_trade(
        self, offer_id: str, send: list[str], receive: list[str], message: str
    ) -> Any:
        return counter_trade(
            self.db,
            offer_id=offer_id,
            countering_team_id=self.team_id,
            send_player_ids=send,
            receive_player_ids=receive,
            message=message,
        )

    def accept_trade(self, offer_id: str) -> TradeThread:
        return accept_trade(self.db, offer_id=offer_id, accepting_team_id=self.team_id)

    def reject_trade(self, offer_id: str) -> TradeThread:
        return reject_trade(self.db, offer_id=offer_id, rejecting_team_id=self.team_id)

    def _league(self) -> League:
        league = self.db.get(League, self.league_id)
        if not league:
            raise NotFoundError("league", self.league_id)
        return league

    def _league_team(self, team_id: str) -> Team:
        team = self.db.get(Team, team_id)
        if not team or team.league_id != self.league_id:
            raise NotFoundError("team", team_id)
        return team

    def _player(self, player_id: str) -> Player:
        player = self.db.get(Player, player_id)
        if not player:
            raise NotFoundError("player", player_id)
        return player

    @staticmethod
    def _player_summary(player: Player) -> dict[str, Any]:
        return {
            "player_id": player.id,
            "name": player.full_name,
            "position": player.position,
            "nfl_team": player.nfl_team,
            "status": player.status,
            "injury_status": player.injury_status,
            "bye_week": player.bye_week,
            "rank": (player.metadata_json or {}).get("rank", 10**9),
            "projection": (player.metadata_json or {}).get("projection"),
            "metadata": player.metadata_json,
        }
