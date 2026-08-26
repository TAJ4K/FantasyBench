from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ROSTER_CONFIG: dict[str, Any] = {
    "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1},
    "bench": 6,
    "ir": 1,
    "flex_eligible": ["RB", "WR", "TE"],
}

DEFAULT_SCORING_CONFIG: dict[str, Any] = {
    "linear": {
        "passing_yards": 0.04,
        "passing_touchdowns": 4.0,
        "interceptions": -2.0,
        "passing_two_point_conversions": 2.0,
        "rushing_yards": 0.1,
        "rushing_touchdowns": 6.0,
        "rushing_two_point_conversions": 2.0,
        "receptions": 1.0,
        "receiving_yards": 0.1,
        "receiving_touchdowns": 6.0,
        "receiving_two_point_conversions": 2.0,
        "fumbles_lost": -2.0,
        "extra_points_made": 1.0,
        "field_goals_0_39": 3.0,
        "field_goals_40_49": 4.0,
        "field_goals_50_plus": 5.0,
        "dst_sacks": 1.0,
        "dst_interceptions": 2.0,
        "dst_fumble_recoveries": 2.0,
        "dst_touchdowns": 6.0,
        "dst_safeties": 2.0,
        "dst_blocked_kicks": 2.0,
    },
    "dst_points_allowed": [
        {"min": 0, "max": 0, "points": 10.0},
        {"min": 1, "max": 6, "points": 7.0},
        {"min": 7, "max": 13, "points": 4.0},
        {"min": 14, "max": 20, "points": 1.0},
        {"min": 21, "max": 27, "points": 0.0},
        {"min": 28, "max": 34, "points": -1.0},
        {"min": 35, "max": None, "points": -4.0},
    ],
}

DEFAULT_LEAGUE_SETTINGS: dict[str, Any] = {
    "teams": 8,
    "ppr": True,
    "draft_type": "SNAKE",
    "draft_rounds": 15,
    "faab_starting_budget": 100,
    "waiver_tiebreaker": "ROLLING_PRIORITY",
    "waiver_period_hours": 24,
    "waiver_processing_grace_minutes": 30,
    "regular_season_weeks": 14,
    "playoff_team_count": 4,
    "playoff_weeks": [16, 17],
    "standings_tiebreakers": ["WIN_PERCENTAGE", "POINTS_FOR", "HEAD_TO_HEAD"],
}


@dataclass(frozen=True)
class ManagerDefinition:
    key: str
    display_name: str
    model: str
    reasoning_effort: str | None


# Slugs verified against OpenRouter's public model catalog on 2026-08-26.
DEFAULT_MANAGERS: tuple[ManagerDefinition, ...] = (
    ManagerDefinition("gpt", "GPT 5.6 Sol Light", "openai/gpt-5.6-sol", "low"),
    ManagerDefinition("claude", "Claude Opus 5 low", "anthropic/claude-opus-5", "low"),
    ManagerDefinition("glm", "GLM 5.3", "z-ai/glm-5.3", None),
    ManagerDefinition("deepseek", "DeepSeek v4 Pro", "deepseek/deepseek-v4-pro", None),
    ManagerDefinition("qwen", "Qwen 3.8 Max", "qwen/qwen3.8-max", None),
    ManagerDefinition("grok", "Grok 4.6", "x-ai/grok-4.6", None),
    ManagerDefinition("gemini", "Gemini 3.7 Flash", "google/gemini-3.7-flash", None),
    ManagerDefinition("kimi", "Kimi k3", "moonshotai/kimi-k3", None),
)
