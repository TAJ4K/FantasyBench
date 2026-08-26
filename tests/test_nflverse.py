from __future__ import annotations

import httpx
import pytest

from app.nfl.nflverse import NflverseProvider


@pytest.mark.asyncio
async def test_nflverse_schedule_and_stats_mapping() -> None:
    schedule = (
        ",".join(
            [
                "game_id",
                "season",
                "week",
                "gameday",
                "gametime",
                "home_team",
                "away_team",
                "home_score",
                "away_score",
                "game_type",
            ]
        )
        + "\n2025_01_SF_SEA,2025,1,2025-09-07,16:05,SEA,SF,24,17,REG\n"
    )
    stats = ",".join(
        [
            "player_id",
            "season",
            "week",
            "season_type",
            "team",
            "passing_yards",
            "passing_tds",
            "interceptions",
            "rushing_yards",
            "receptions",
            "receiving_yards",
            "receiving_tds",
            "rushing_fumbles_lost",
            "def_sacks",
            "def_interceptions",
            "fumble_recovery_opp",
            "def_tds",
            "def_safeties",
        ]
    )
    stats += "\n00-1,2025,1,REG,SEA,250,2,1,10,0,0,0,1,0,0,0,0,0"
    stats += "\n00-2,2025,1,REG,SEA,0,0,0,0,7,95,1,0,2,1,1,1,1\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stats if "stats_player" in str(request.url) else schedule)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    provider = NflverseProvider(client=client)
    games = await provider.get_schedule(2025)
    records = await provider.get_week_stats(2025, 1)
    await client.aclose()

    assert len(games) == 1 and games[0].status == "FINAL"
    quarterback = next(record for record in records if record.provider_player_id == "00-1")
    assert quarterback.stats["passing_yards"] == 250
    assert quarterback.stats["passing_touchdowns"] == 2
    assert quarterback.stats["fumbles_lost"] == 1
    defense = next(record for record in records if record.provider_player_id == "DST:SEA")
    assert defense.stats["dst_sacks"] == 2
    assert defense.stats["dst_interceptions"] == 1
    assert defense.stats["dst_points_allowed"] == 17
