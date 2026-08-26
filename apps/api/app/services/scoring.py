from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PlayerFantasyScore


@dataclass(frozen=True)
class ScoreResult:
    total: float
    breakdown: dict[str, float]
    scoring_config_hash: str


def scoring_config_hash(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def score_stats(raw_stats: Mapping[str, float], config: Mapping[str, Any]) -> ScoreResult:
    """Score arbitrary linear statistics plus configurable bucket rules."""
    breakdown: dict[str, float] = {}
    for stat, multiplier in config.get("linear", {}).items():
        value = float(raw_stats.get(stat, 0) or 0)
        points = round(value * float(multiplier), 4)
        if value or points:
            breakdown[stat] = points

    points_allowed = raw_stats.get("dst_points_allowed")
    if points_allowed is not None:
        value = float(points_allowed)
        for bucket in config.get("dst_points_allowed", []):
            minimum = float(bucket.get("min", float("-inf")))
            maximum = bucket.get("max")
            if value >= minimum and (maximum is None or value <= float(maximum)):
                breakdown["dst_points_allowed"] = float(bucket["points"])
                break

    # Additional bucket families (for example long field goals) can be configured
    # without changing the engine: {"buckets": {"stat": [{min,max,points}]}}.
    for stat, buckets in config.get("buckets", {}).items():
        if stat not in raw_stats:
            continue
        value = float(raw_stats[stat])
        for bucket in buckets:
            minimum = float(bucket.get("min", float("-inf")))
            maximum = bucket.get("max")
            if value >= minimum and (maximum is None or value <= float(maximum)):
                breakdown[stat] = float(bucket["points"])
                break

    total = round(sum(breakdown.values()), 4)
    return ScoreResult(total, breakdown, scoring_config_hash(config))


def persist_player_score(
    db: Session,
    *,
    league_id: str,
    player_id: str,
    season: int,
    week: int,
    raw_stats: Mapping[str, float],
    scoring_config: Mapping[str, Any],
) -> PlayerFantasyScore:
    result = score_stats(raw_stats, scoring_config)
    score = db.scalar(
        select(PlayerFantasyScore).where(
            PlayerFantasyScore.league_id == league_id,
            PlayerFantasyScore.player_id == player_id,
            PlayerFantasyScore.season == season,
            PlayerFantasyScore.week == week,
        )
    )
    if score is None:
        score = PlayerFantasyScore(
            league_id=league_id, player_id=player_id, season=season, week=week
        )
        db.add(score)
    score.raw_stats = {key: float(value) for key, value in raw_stats.items()}
    score.breakdown = result.breakdown
    score.total = result.total
    score.scoring_config_hash = result.scoring_config_hash
    db.flush()
    return score
