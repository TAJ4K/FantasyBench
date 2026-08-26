from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.nfl.contracts import NFLGameRecord, NFLPlayerRecord, NFLStatRecord


class FixtureNFLProvider:
    """Deterministic, network-free NFL provider for development and backtests."""

    name = "fixture"

    def __init__(
        self,
        *,
        players: list[NFLPlayerRecord] | None = None,
        games: list[NFLGameRecord] | None = None,
        stats: list[NFLStatRecord] | None = None,
    ) -> None:
        self.players = list(players or [])
        self.games = list(games or [])
        self.stats = list(stats or [])

    @classmethod
    def from_json(cls, path: str | Path) -> FixtureNFLProvider:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        players = [NFLPlayerRecord(**item) for item in payload.get("players", [])]
        games = [NFLGameRecord(**_parse_game(item)) for item in payload.get("games", [])]
        stats = [NFLStatRecord(**_parse_stat(item)) for item in payload.get("stats", [])]
        return cls(players=players, games=games, stats=stats)

    async def get_players(self, season: int) -> list[NFLPlayerRecord]:
        return list(self.players)

    async def get_schedule(self, season: int) -> list[NFLGameRecord]:
        return [game for game in self.games if game.season == season]

    async def get_week_stats(self, season: int, week: int) -> list[NFLStatRecord]:
        return [stat for stat in self.stats if stat.season == season and stat.week == week]

    async def get_injuries(self, season: int, week: int) -> list[NFLPlayerRecord]:
        return [player for player in self.players if player.injury_status]


def _parse_game(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    if isinstance(result.get("kickoff_at"), str):
        result["kickoff_at"] = datetime.fromisoformat(result["kickoff_at"].replace("Z", "+00:00"))
    return result


def _parse_stat(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    if isinstance(result.get("updated_at"), str):
        result["updated_at"] = datetime.fromisoformat(result["updated_at"].replace("Z", "+00:00"))
    return result
