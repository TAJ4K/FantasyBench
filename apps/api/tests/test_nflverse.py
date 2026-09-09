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
    assert provider.stats_available
    assert not provider.week_stats_complete  # SF's rows have not arrived yet.


@pytest.mark.asyncio
async def test_unpublished_stats_wait_but_other_provider_errors_fail() -> None:
    for status in (404, 503):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, status=status: httpx.Response(status))
        ) as client:
            provider = NflverseProvider(client=client)
            if status == 404:
                assert await provider.get_week_stats(2026, 1) == []
                assert not provider.stats_available and not provider.week_stats_complete
            else:
                with pytest.raises(httpx.HTTPStatusError):
                    await provider.get_week_stats(2026, 1)


@pytest.mark.asyncio
async def test_current_scoring_columns_and_complete_regular_week() -> None:
    schedule = (
        "season,week,home_team,away_team,home_score,away_score,game_type\n"
        "2026,1,SEA,NE,20,10,REG\n2026,1,KC,BUF,30,20,POST\n"
    )
    stats = (
        "player_id,season,week,season_type,team,receptions,passing_tds,passing_touchdowns,"
        "fumbles_lost_total,rushing_fumbles_lost,fg_made_0_19,fg_made_30_39,"
        "def_punt_blocks,def_pat_blocks,def_fg_blocks\n"
        "00-1,2026,1,REG,SEA,7,2,2,2,1,1,2,1,1,1\n"
        "00-2,2026,1,REG,NE,0,0,0,0,0,0,0,0,0,0\n"
        "00-3,2026,1,POST,KC,9,0,0,0,0,0,0,0,0,0\n"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text=stats if "stats_player" in str(request.url) else schedule
            )
        )
    ) as client:
        provider = NflverseProvider(client=client)
        records = {r.provider_player_id: r for r in await provider.get_week_stats(2026, 1)}
    assert provider.week_stats_complete
    assert "00-3" not in records
    assert records["00-1"].stats["passing_touchdowns"] == 2
    assert records["00-1"].stats["fumbles_lost"] == 2
    assert records["00-1"].stats["field_goals_0_39"] == 3
    assert records["00-1"].stats["receptions"] == 7
    assert records["DST:SEA"].stats["dst_blocked_kicks"] == 3


@pytest.mark.asyncio
async def test_identity_enrichment_and_injury_clear(db) -> None:
    from sqlalchemy import select

    from app.models.entities import Player
    from app.nfl.sleeper import SleeperProvider
    from app.nfl.sync import NFLDataSyncService

    player = Player(
        full_name="Bijan Robinson",
        position="RB",
        sleeper_id="9509",
        injury_status="Questionable",
        nfl_team="ATL",
        active=True,
    )
    db.add(player)
    db.commit()
    identities = "sleeper_id,gsis_id\n9509,00-0038542\n999,NA\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=identities))
    ) as client:
        provider = NflverseProvider(client=client)
        mapping = await provider.get_player_identities()
        result = NFLDataSyncService(db, provider).sync_player_identities(mapping)
    assert result.updated == 1 and player.gsis_id == "00-0038542"
    assert player.injury_status == "Questionable" and player.nfl_team == "ATL"
    assert len(list(db.scalars(select(Player)))) == 1
    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "9509": {
                        "full_name": "Bijan Robinson",
                        "position": "RB",
                        "team": "ATL",
                        "active": True,
                        "injury_status": None,
                    }
                },
            )
        ),
    ) as client:
        await NFLDataSyncService(db, SleeperProvider(client=client)).sync_injuries(2026, 1)
    assert player.injury_status is None
    assert player.gsis_id == "00-0038542"
    player.gsis_id = " 00-0038542"
    db.commit()
    assert NFLDataSyncService(db, provider).sync_player_identities(mapping).updated == 1
    assert player.gsis_id == "00-0038542"


@pytest.mark.asyncio
async def test_scheduler_does_not_finalize_partial_stats(engine, monkeypatch) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.jobs.scheduler import LeagueScheduler
    from app.models.entities import League, Matchup, NflGame
    from app.services.initialization import initialize_league

    class PartialProvider:
        name = "nflverse"
        stats_available = True
        week_stats_complete = False

        async def get_player_identities(self):
            return {}

        async def get_week_stats(self, season, week):
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr("app.jobs.scheduler.NflverseProvider", PartialProvider)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026)
        league.status = "REGULAR_SEASON"
        league.current_week = 1
        league_id = league.id
        db.add(
            NflGame(
                season=2026,
                week=1,
                provider_game_id="first",
                kickoff_at=datetime.now(UTC),
                home_team="SEA",
                away_team="NE",
                status="FINAL",
            )
        )
        db.commit()
    scheduler = LeagueScheduler(factory, SimpleNamespace(), SimpleNamespace(), 60)
    result = await scheduler._sync_stats_and_score(league_id, 1)
    assert result["week_completed"] == "false"
    with factory() as db:
        assert db.get(League, league_id).current_week == 1
        assert all(m.status != "COMPLETE" for m in db.scalars(select(Matchup)))
