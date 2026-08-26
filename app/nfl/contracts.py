from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class NFLPlayerRecord:
    provider_id: str
    full_name: str
    position: str
    nfl_team: str | None
    active: bool = True
    status: str = "ACTIVE"
    injury_status: str | None = None
    bye_week: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    gsis_id: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NFLGameRecord:
    provider_id: str
    season: int
    week: int
    kickoff_at: datetime
    home_team: str
    away_team: str
    status: str = "SCHEDULED"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NFLStatRecord:
    provider_player_id: str
    season: int
    week: int
    stats: dict[str, float]
    updated_at: datetime | None = None


class NFLDataProvider(Protocol):
    name: str

    async def get_players(self, season: int) -> list[NFLPlayerRecord]: ...

    async def get_schedule(self, season: int) -> list[NFLGameRecord]: ...

    async def get_week_stats(self, season: int, week: int) -> list[NFLStatRecord]: ...

    async def get_injuries(self, season: int, week: int) -> list[NFLPlayerRecord]: ...
