from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.nfl.contracts import NFLStatRecord
from app.nfl.sleeper import SleeperProvider


class SleeperStatsProvider(SleeperProvider):
    """Provisional game-day stats from Sleeper's public, undocumented stats feed.

    Reuse Sleeper player IDs, but score raw statistics with this league's rules.
    This feed never authorizes completing a fantasy week.
    """

    async def get_week_stats(self, season: int, week: int) -> list[NFLStatRecord]:
        response = await self._client.get(
            f"https://api.sleeper.com/stats/nfl/{season}/{week}",
            params={"season_type": "regular"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Sleeper stats response must be a list")
        records: list[NFLStatRecord] = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("Sleeper stats row must be an object")
            if (
                str(row.get("season")) != str(season)
                or row.get("week") != week
                or row.get("season_type") != "regular"
                or row.get("category") != "stat"
                or row.get("sport") != "nfl"
            ):
                continue
            position = (row.get("player") or {}).get("position")
            if position not in {"QB", "RB", "WR", "TE", "K", "DEF"}:
                continue
            player_id = str(row.get("player_id") or "")
            raw = row.get("stats")
            if not player_id or not isinstance(raw, dict):
                raise ValueError("Sleeper stats row is missing its player ID or stats")
            # Active-roster placeholders appear before kickoff. They are not scores.
            if _number(raw, "gp") <= 0:
                continue
            updated = row.get("updated_at")
            records.append(NFLStatRecord(
                provider_player_id=player_id,
                season=season,
                week=week,
                stats=_fantasy_stats(raw, defense=position == "DEF"),
                updated_at=datetime.fromtimestamp(float(updated) / 1000, UTC)
                if updated is not None else None,
            ))
        return records


def _number(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key, 0)
    if isinstance(value, bool):
        raise ValueError(f"Invalid Sleeper stat: {key}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Invalid Sleeper stat: {key}")
    return number


def _fantasy_stats(raw: dict[str, Any], *, defense: bool) -> dict[str, float]:
    if defense:
        # `td` is a team total, not a defensive touchdown. Use the specific fields.
        return {
            "dst_sacks": _number(raw, "sack"),
            "dst_interceptions": _number(raw, "int"),
            "dst_fumble_recoveries": _number(raw, "fum_rec") + _number(raw, "def_st_fum_rec"),
            "dst_touchdowns": _number(raw, "def_td") + _number(raw, "def_st_td"),
            "dst_safeties": _number(raw, "safe"),
            "dst_blocked_kicks": _number(raw, "blk_kick"),
            **({"dst_points_allowed": _number(raw, "pts_allow")} if "pts_allow" in raw else {}),
        }
    mapping = {
        "passing_yards": "pass_yd",
        "passing_touchdowns": "pass_td",
        "interceptions": "pass_int",
        "passing_two_point_conversions": "pass_2pt",
        "rushing_yards": "rush_yd",
        "rushing_touchdowns": "rush_td",
        "rushing_two_point_conversions": "rush_2pt",
        "receptions": "rec",
        "receiving_yards": "rec_yd",
        "receiving_touchdowns": "rec_td",
        "receiving_two_point_conversions": "rec_2pt",
        "fumbles_lost": "fum_lost",
        "extra_points_made": "xpm",
        "field_goals_40_49": "fgm_40_49",
    }
    return {
        **{target: _number(raw, source) for target, source in mapping.items()},
        "field_goals_0_39": sum(_number(raw, key) for key in (
            "fgm_0_19", "fgm_20_29", "fgm_30_39",
        )),
        "field_goals_50_plus": _number(raw, "fgm_50p") if "fgm_50p" in raw else (
            _number(raw, "fgm_50_59") + _number(raw, "fgm_60p")
        ),
    }
