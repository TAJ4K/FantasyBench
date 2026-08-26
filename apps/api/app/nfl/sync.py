from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import NflGame, Player, PlayerWeekStat
from app.nfl.contracts import NFLDataProvider, NFLGameRecord, NFLPlayerRecord, NFLStatRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


class NFLDataSyncService:
    """Fetch-first, transactional upserts; provider failures leave the database untouched."""

    def __init__(self, session: Session, provider: NFLDataProvider) -> None:
        self.session = session
        self.provider = provider

    async def sync_players(self, season: int) -> SyncResult:
        records = await self.provider.get_players(season)
        return self._transaction(lambda: self._upsert_players(records))

    async def sync_schedule(self, season: int) -> SyncResult:
        records = await self.provider.get_schedule(season)
        return self._transaction(lambda: self._upsert_schedule(records))

    async def sync_week_stats(self, season: int, week: int) -> SyncResult:
        records = await self.provider.get_week_stats(season, week)
        return self._transaction(lambda: self._upsert_stats(records))

    async def sync_injuries(self, season: int, week: int) -> SyncResult:
        records = await self.provider.get_injuries(season, week)
        return self._transaction(lambda: self._upsert_players(records))

    def _transaction(self, operation: Callable[[], SyncResult]) -> SyncResult:
        try:
            result = operation()
            self.session.commit()
            logger.info(
                "nfl_sync_completed",
                extra={"provider": self.provider.name, **result.__dict__},
            )
            return result
        except Exception:
            self.session.rollback()
            logger.exception("nfl_sync_failed", extra={"provider": self.provider.name})
            raise

    def _upsert_players(self, records: list[NFLPlayerRecord]) -> SyncResult:
        players = list(self.session.scalars(select(Player)))
        by_gsis = {p.gsis_id: p for p in players if p.gsis_id}
        by_provider: dict[str, Player] = {}
        for player in players:
            if self.provider.name == "sleeper" and player.sleeper_id:
                by_provider[player.sleeper_id] = player
            external_id = (player.external_ids or {}).get(self.provider.name)
            if external_id:
                by_provider[str(external_id)] = player
        inserted = updated = skipped = 0
        seen: set[str] = set()
        for record in records:
            identity = record.gsis_id or record.provider_id
            if identity in seen:
                skipped += 1
                continue
            seen.add(identity)
            existing_player = (
                by_gsis.get(record.gsis_id) if record.gsis_id else None
            ) or by_provider.get(record.provider_id)
            if existing_player is None:
                existing_player = Player(full_name=record.full_name, position=record.position)
                self.session.add(existing_player)
                inserted += 1
            else:
                updated += 1
            self._apply_player(existing_player, record)
        return SyncResult(inserted, updated, skipped)

    def _apply_player(self, player: Player, record: NFLPlayerRecord) -> None:
        player.full_name = record.full_name
        player.first_name = record.first_name
        player.last_name = record.last_name
        player.position = record.position
        player.nfl_team = record.nfl_team
        player.status = record.status
        player.active = record.active
        player.injury_status = record.injury_status
        player.bye_week = record.bye_week
        player.gsis_id = record.gsis_id or player.gsis_id
        external = dict(player.external_ids or {})
        external.update(record.external_ids)
        external[self.provider.name] = record.provider_id
        player.external_ids = external
        if self.provider.name == "sleeper":
            player.sleeper_id = record.provider_id
        player.metadata_json = {**(player.metadata_json or {}), **record.metadata}

    def _upsert_schedule(self, records: list[NFLGameRecord]) -> SyncResult:
        seasons = {record.season for record in records}
        existing = (
            list(self.session.scalars(select(NflGame).where(NflGame.season.in_(seasons))))
            if seasons
            else []
        )
        indexed = {(game.season, game.provider_game_id): game for game in existing}
        inserted = updated = skipped = 0
        seen: set[tuple[int, str]] = set()
        for record in records:
            key = (record.season, record.provider_id)
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            game = indexed.get(key)
            if game is None:
                game = NflGame(season=record.season, provider_game_id=record.provider_id)
                self.session.add(game)
                inserted += 1
            else:
                updated += 1
            game.week = record.week
            game.kickoff_at = record.kickoff_at
            game.home_team = record.home_team
            game.away_team = record.away_team
            game.status = record.status
            game.payload = record.payload
        return SyncResult(inserted, updated, skipped)

    def _upsert_stats(self, records: list[NFLStatRecord]) -> SyncResult:
        players = list(self.session.scalars(select(Player)))
        provider_players = {
            str((player.external_ids or {}).get(self.provider.name)): player
            for player in players
            if (player.external_ids or {}).get(self.provider.name)
        }
        if self.provider.name == "sleeper":
            provider_players.update({p.sleeper_id: p for p in players if p.sleeper_id})
        if self.provider.name == "nflverse":
            provider_players.update({p.gsis_id: p for p in players if p.gsis_id})
            provider_players.update(
                {
                    f"DST:{player.nfl_team}": player
                    for player in players
                    if player.position == "DST" and player.nfl_team
                }
            )
        player_ids = {p.id for p in provider_players.values()}
        existing = (
            list(
                self.session.scalars(
                    select(PlayerWeekStat).where(PlayerWeekStat.player_id.in_(player_ids))
                )
            )
            if player_ids
            else []
        )
        indexed = {(stat.player_id, stat.season, stat.week): stat for stat in existing}
        inserted = updated = skipped = 0
        for record in records:
            player = provider_players.get(record.provider_player_id)
            if player is None:
                skipped += 1
                continue
            key = (player.id, record.season, record.week)
            stat = indexed.get(key)
            if stat is None:
                stat = PlayerWeekStat(
                    player_id=player.id,
                    season=record.season,
                    week=record.week,
                    provider=self.provider.name,
                )
                self.session.add(stat)
                indexed[key] = stat
                inserted += 1
            else:
                updated += 1
            stat.provider = self.provider.name
            stat.raw_stats = record.stats
            stat.source_updated_at = record.updated_at
        return SyncResult(inserted, updated, skipped)
