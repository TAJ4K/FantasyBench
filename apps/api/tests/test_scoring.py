from app.core.defaults import DEFAULT_SCORING_CONFIG
from app.services.scoring import score_stats


def test_full_ppr_kicker_and_dst_breakdown_is_reproducible() -> None:
    stats = {
        "passing_yards": 287,
        "passing_touchdowns": 2,
        "interceptions": 1,
        "rushing_yards": 22,
        "receptions": 3,
        "field_goals_40_49": 2,
        "dst_sacks": 3,
        "dst_points_allowed": 6,
    }
    first = score_stats(stats, DEFAULT_SCORING_CONFIG)
    second = score_stats(dict(reversed(list(stats.items()))), DEFAULT_SCORING_CONFIG)
    assert first == second
    assert first.breakdown["passing_yards"] == 11.48
    assert first.breakdown["field_goals_40_49"] == 8.0
    assert first.breakdown["dst_points_allowed"] == 7.0
    assert first.total == 40.68


def test_generic_custom_bucket() -> None:
    result = score_stats(
        {"bonus_distance": 55},
        {"linear": {}, "buckets": {"bonus_distance": [{"min": 50, "max": None, "points": 5}]}},
    )
    assert result.total == 5
