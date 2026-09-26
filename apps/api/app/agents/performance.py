from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import League, Player, PlayerWeekStat
from app.services.scoring import score_stats


class PerformanceSnapshot:
    """League-scored evidence, including unrostered players, without future-week leakage."""

    def __init__(self, db: Session, league: League) -> None:
        self.season = league.nfl_season
        self.week = league.current_week
        self.logs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.summaries: dict[str, dict[str, Any]] = {}
        positions = {
            pid: position for pid, position in db.execute(select(Player.id, Player.position)).all()
        }
        rows = db.scalars(
            select(PlayerWeekStat)
            .where(
                PlayerWeekStat.season == self.season,
                PlayerWeekStat.week > 0,
                PlayerWeekStat.week <= self.week,
            )
            .order_by(PlayerWeekStat.week.desc())
        ).all()
        for row in rows:
            self.logs[row.player_id].append(
                {
                    "week": row.week,
                    "fantasy_points": score_stats(row.raw_stats, league.scoring_config).total,
                    "stats": row.raw_stats,
                    "provisional": row.week == self.week or row.provider == "sleeper",
                    "current_week": row.week == self.week,
                    "source": row.provider,
                    "updated_at": (row.source_updated_at or row.updated_at).isoformat(),
                }
            )
        for player_id, logs in self.logs.items():
            completed = [row for row in logs if row["week"] < self.week]
            total = round(sum(row["fantasy_points"] for row in completed), 4)
            recent = completed[:3]
            self.summaries[player_id] = {
                "season": self.season,
                "through_week": max(0, self.week - 1),
                "games_with_stats": len(completed),
                "season_points": total if completed else None,
                "points_per_recorded_game": round(total / len(completed), 2) if completed else None,
                "last_3_games_average": round(
                    sum(r["fantasy_points"] for r in recent) / len(recent), 2
                )
                if recent
                else None,
                "recent_weekly_points": [
                    {
                        k: r[k]
                        for k in ("week", "fantasy_points", "provisional", "source", "updated_at")
                    }
                    for r in recent
                ],
                "current_week_points": next(
                    (r["fantasy_points"] for r in logs if r["current_week"]), None
                ),
                "position_rank_by_total": None,
            }
        for position in set(positions.values()):
            ranked = sorted(
                (
                    pid
                    for pid, summary in self.summaries.items()
                    if positions.get(pid) == position and summary["games_with_stats"]
                ),
                key=lambda pid: -self.summaries[pid]["season_points"],
            )
            previous = None
            rank = 0
            for index, pid in enumerate(ranked, 1):
                points = self.summaries[pid]["season_points"]
                if points != previous:
                    rank = index
                self.summaries[pid]["position_rank_by_total"] = rank
                previous = points

    def summary(self, player_id: str) -> dict[str, Any]:
        return self.summaries.get(
            player_id,
            {
                "season": self.season,
                "through_week": max(0, self.week - 1),
                "games_with_stats": 0,
                "season_points": None,
                "points_per_recorded_game": None,
                "last_3_games_average": None,
                "recent_weekly_points": [],
                "current_week_points": None,
                "position_rank_by_total": None,
            },
        )
