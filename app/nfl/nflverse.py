from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.nfl.contracts import NFLGameRecord, NFLPlayerRecord, NFLStatRecord


class NflverseProvider:
    """Open nflverse schedule and weekly-stat adapter.

    The URLs follow nflreadr's official loaders. Player identity is the stable
    GSIS id; team-defense rows use ``DST:<team>`` and are resolved internally.
    """

    name = "nflverse"
    schedule_url = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
    stats_url = (
        "https://github.com/nflverse/nflverse-data/releases/download/"
        "stats_player/stats_player_week_{season}.csv"
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=True
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_players(self, season: int) -> list[NFLPlayerRecord]:
        del season
        return []

    async def get_injuries(self, season: int, week: int) -> list[NFLPlayerRecord]:
        del season, week
        return []

    async def get_schedule(self, season: int) -> list[NFLGameRecord]:
        rows = await self._csv_rows(self.schedule_url)
        records: list[NFLGameRecord] = []
        for row in rows:
            if _integer(row.get("season")) != season:
                continue
            provider_id = _text(row.get("game_id")) or _text(row.get("old_game_id"))
            home = _team(row.get("home_team"))
            away = _team(row.get("away_team"))
            week = _integer(row.get("week"))
            gameday = _date(row.get("gameday"))
            if not provider_id or not home or not away or not week or gameday is None:
                continue
            kickoff = _kickoff(gameday, row.get("gametime"))
            home_score = _number(row.get("home_score"))
            away_score = _number(row.get("away_score"))
            status = "FINAL" if home_score is not None and away_score is not None else "SCHEDULED"
            records.append(
                NFLGameRecord(
                    provider_id=provider_id,
                    season=season,
                    week=week,
                    kickoff_at=kickoff,
                    home_team=home,
                    away_team=away,
                    status=status,
                    payload={
                        "game_type": row.get("game_type"),
                        "home_score": home_score,
                        "away_score": away_score,
                        "source": "nflverse/nfldata",
                    },
                )
            )
        return records

    async def get_week_stats(self, season: int, week: int) -> list[NFLStatRecord]:
        stats_rows = await self._csv_rows(self.stats_url.format(season=season))
        schedule_rows = await self._csv_rows(self.schedule_url)
        points_allowed = _points_allowed(schedule_rows, season, week)
        records: list[NFLStatRecord] = []
        dst: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in stats_rows:
            if _integer(row.get("season")) != season or _integer(row.get("week")) != week:
                continue
            if str(row.get("season_type") or "REG").upper() not in {"REG", "POST"}:
                continue
            player_id = _text(row.get("player_id"))
            if player_id:
                records.append(
                    NFLStatRecord(
                        provider_player_id=player_id,
                        season=season,
                        week=week,
                        stats=_fantasy_stats(row),
                    )
                )
            team = _team(row.get("team"))
            if not team:
                continue
            team_stats = dst[team]
            team_stats["dst_sacks"] += _first_number(row, "def_sacks", "defense_sacks")
            team_stats["dst_interceptions"] += _first_number(
                row, "def_interceptions", "defense_interceptions"
            )
            team_stats["dst_fumble_recoveries"] += _first_number(
                row, "fumble_recovery_opp", "def_fumble_recoveries"
            )
            team_stats["dst_touchdowns"] += _first_number(
                row, "def_tds", "fumble_recovery_tds", "special_teams_tds"
            )
            team_stats["dst_safeties"] += _first_number(row, "def_safeties")
            team_stats["dst_blocked_kicks"] += _first_number(row, "def_blocked_kicks")
        for team, team_stats in dst.items():
            if team in points_allowed:
                team_stats["dst_points_allowed"] = points_allowed[team]
            records.append(
                NFLStatRecord(
                    provider_player_id=f"DST:{team}",
                    season=season,
                    week=week,
                    stats=dict(team_stats),
                )
            )
        return records

    async def _csv_rows(self, url: str) -> list[dict[str, str]]:
        response = await self._client.get(url)
        response.raise_for_status()
        return list(csv.DictReader(io.StringIO(response.text)))


def _fantasy_stats(row: dict[str, str]) -> dict[str, float]:
    values = {
        "passing_yards": _first_number(row, "passing_yards"),
        "passing_touchdowns": _first_number(row, "passing_tds", "passing_touchdowns"),
        "interceptions": _first_number(row, "interceptions", "passing_interceptions"),
        "passing_two_point_conversions": _first_number(row, "passing_2pt_conversions"),
        "rushing_yards": _first_number(row, "rushing_yards"),
        "rushing_touchdowns": _first_number(row, "rushing_tds", "rushing_touchdowns"),
        "rushing_two_point_conversions": _first_number(row, "rushing_2pt_conversions"),
        "receptions": _first_number(row, "receptions"),
        "receiving_yards": _first_number(row, "receiving_yards"),
        "receiving_touchdowns": _first_number(row, "receiving_tds", "receiving_touchdowns"),
        "receiving_two_point_conversions": _first_number(row, "receiving_2pt_conversions"),
        "fumbles_lost": _first_number(
            row,
            "rushing_fumbles_lost",
            "receiving_fumbles_lost",
            "sack_fumbles_lost",
        ),
        "extra_points_made": _first_number(row, "pat_made", "extra_points_made"),
        "field_goals_0_39": _first_number(row, "fg_made_0_19", "fg_made_20_29", "fg_made_30_39"),
        "field_goals_40_49": _first_number(row, "fg_made_40_49"),
        "field_goals_50_plus": _first_number(row, "fg_made_50_59", "fg_made_60_"),
    }
    return {key: value for key, value in values.items() if value}


def _points_allowed(rows: list[dict[str, str]], season: int, week: int) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        if _integer(row.get("season")) != season or _integer(row.get("week")) != week:
            continue
        home = _team(row.get("home_team"))
        away = _team(row.get("away_team"))
        home_score = _number(row.get("home_score"))
        away_score = _number(row.get("away_score"))
        if home and away and home_score is not None and away_score is not None:
            result[home] = away_score
            result[away] = home_score
    return result


def _first_number(row: dict[str, str], *keys: str) -> float:
    return sum(_number(row.get(key)) or 0.0 for key in keys)


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _number(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in {"", "na", "nan", "none"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _team(value: Any) -> str | None:
    team = _text(value)
    if not team:
        return None
    return {"LA": "LAR", "JAC": "JAX"}.get(team.upper(), team.upper())


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _kickoff(gameday: date, value: Any) -> datetime:
    raw = str(value or "12:00").strip()
    try:
        local_time = time.fromisoformat(raw)
    except ValueError:
        local_time = time(hour=12)
    eastern = datetime.combine(gameday, local_time, tzinfo=ZoneInfo("America/New_York"))
    return eastern.astimezone(UTC)
