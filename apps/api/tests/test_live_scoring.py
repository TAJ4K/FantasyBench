from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.defaults import DEFAULT_SCORING_CONFIG
from app.jobs.scheduler import LeagueScheduler
from app.models.entities import (
    JobRun,
    League,
    LineupDecision,
    Matchup,
    NflGame,
    Player,
    PlayerFantasyScore,
    PlayerWeekStat,
    Team,
)
from app.nfl.contracts import NFLStatRecord
from app.nfl.sleeper_stats import SleeperStatsProvider
from app.services.initialization import initialize_league
from app.services.rosters import RosterService
from app.services.scoring import score_stats


def feed_row(player_id, position, stats, **extra):
    return {
        "player_id": player_id, "player": {"position": position}, "season": "2026",
        "week": 1, "season_type": "regular", "sport": "nfl", "category": "stat",
        "stats": stats, "updated_at": 1789338936451, **extra,
    }


@pytest.mark.asyncio
async def test_live_stat_mapping_uses_league_rules_and_excludes_placeholders():
    rows = [
        feed_row("qb", "QB", {
            "gp": 1, "pass_yd": 250, "pass_td": 2, "pass_int": 1, "pass_2pt": 1,
            "rush_yd": 10, "rush_td": 1, "rush_2pt": 1, "fum_lost": 1,
            "pts_ppr": 999,
        }),
        feed_row("wr", "WR", {"gp": 1, "rec": 7, "rec_yd": 95, "rec_td": 1, "rec_2pt": 1}),
        feed_row("negative", "RB", {"gp": 1, "rush_yd": -5}),
        feed_row("zero", "TE", {"gp": 1}),
        feed_row("k", "K", {
            "gp": 1, "xpm": 2, "fgm_0_19": 1, "fgm_20_29": 1, "fgm_30_39": 1,
            "fgm_40_49": 1, "fgm_50p": 2, "fgm_50_59": 1, "fgm_60p": 1,
        }),
        feed_row("SEA", "DEF", {
            "gp": 1, "sack": 3, "int": 2, "fum_rec": 1, "def_st_fum_rec": 1,
            "def_td": 1, "def_st_td": 1, "safe": 1, "blk_kick": 1, "pts_allow": 0,
            "td": 5,
        }),
        feed_row("empty", "WR", {"gms_active": 1}),
        feed_row("future", "QB", {"gp": 1}, week=2),
        feed_row("past", "QB", {"gp": 1}, season="2025"),
        feed_row("projection", "QB", {"gp": 1}, category="proj"),
        feed_row("TEAM_SEA", "TEAM", {"gp": 1, "pass_yd": 999}),
        feed_row("idp", "LB", {"gp": 1, "sack": 1}),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=rows)
    )) as client:
        records = await SleeperStatsProvider(client=client).get_week_stats(2026, 1)
    scores = {
        r.provider_player_id: score_stats(r.stats, DEFAULT_SCORING_CONFIG).total for r in records
    }
    assert scores == {"qb": 25, "wr": 24.5, "negative": -0.5, "zero": 0, "k": 25, "SEA": 37}
    assert all(record.updated_at.tzinfo == UTC for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, [feed_row("qb", "QB", {"gp": 1, "pass_yd": "NaN"})]])
async def test_invalid_live_feed_fails_before_writing(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client:
        with pytest.raises(ValueError):
            await SleeperStatsProvider(client=client).get_week_stats(2026, 1)


@pytest.mark.asyncio
async def test_live_scoring_refresh_corrections_outage_and_finalization(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    timestamp = datetime(2026, 9, 13, 20, tzinfo=UTC)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026)
        league.status = "REGULAR_SEASON"
        league.current_week = 1
        matchup = db.scalar(select(Matchup).where(Matchup.week == 1))
        team_id, league_id = matchup.home_team_id, league.id
        starter = Player(full_name="Live QB", position="QB", sleeper_id="qb", gsis_id="00-1")
        bench = Player(full_name="Bench RB", position="RB", sleeper_id="rb", gsis_id="00-2")
        db.add_all([starter, bench])
        db.flush()
        for player in (starter, bench):
            RosterService(db).add_player(team_id, player.id, acquired_via="DRAFT")
        db.add(LineupDecision(
            league_id=league_id, team_id=team_id, week=1, lineup={"QB": starter.id},
        ))
        db.add(NflGame(
            season=2026, week=1, provider_game_id="live", kickoff_at=timestamp,
            home_team="SEA", away_team="NE", status="LIVE",
        ))
        starter_id, bench_id = starter.id, bench.id
        db.commit()

    class LiveProvider:
        name = "sleeper"
        yards = 100
        updated_at = timestamp
        fail = False
        calls = 0

        async def get_week_stats(self, season, week):
            LiveProvider.calls += 1
            if self.fail:
                raise httpx.ReadTimeout("temporary live feed outage")
            return [
                NFLStatRecord("qb", season, week, {"passing_yards": self.yards}, self.updated_at),
                NFLStatRecord("rb", season, week, {"rushing_yards": 1000}, self.updated_at),
            ]

        async def aclose(self):
            pass

    class FinalProvider:
        name = "nflverse"
        stats_available = True
        week_stats_complete = False

        async def get_player_identities(self):
            return {}

        async def get_week_stats(self, season, week):
            return [NFLStatRecord("00-1", season, week, {"passing_yards": 250})]

        async def aclose(self):
            pass

    monkeypatch.setattr("app.jobs.scheduler.SleeperStatsProvider", LiveProvider)
    monkeypatch.setattr("app.jobs.scheduler.NflverseProvider", FinalProvider)
    scheduler = LeagueScheduler(factory, SimpleNamespace(), SimpleNamespace(), 5)

    def snapshot():
        with factory() as db:
            scores = {s.player_id: s.total for s in db.scalars(select(PlayerFantasyScore))}
            matchup = db.scalar(select(Matchup).where(
                Matchup.home_team_id == team_id, Matchup.week == 1,
            ))
            return scores, matchup.home_score, db.get(League, league_id).current_week

    result = await scheduler._sync_stats_and_score(league_id, 1)
    assert result["source"] == "sleeper" and result["week_completed"] == "false"
    assert snapshot() == ({starter_id: 4, bench_id: 100}, 4, 1)
    LiveProvider.yards = 200
    LiveProvider.updated_at += timedelta(minutes=5)
    await scheduler._sync_stats_and_score(league_id, 1)
    assert snapshot() == ({starter_id: 8, bench_id: 100}, 8, 1)
    # An older request arriving late must not undo the newer snapshot.
    LiveProvider.yards = 50
    LiveProvider.updated_at = timestamp
    await scheduler._sync_stats_and_score(league_id, 1)
    assert snapshot()[1] == 8
    LiveProvider.yards = -10
    LiveProvider.updated_at += timedelta(minutes=10)
    await scheduler._sync_stats_and_score(league_id, 1)
    assert snapshot()[1] == -0.4
    LiveProvider.fail = True
    with pytest.raises(httpx.ReadTimeout):
        await scheduler._sync_stats_and_score(league_id, 1)
    assert snapshot()[1] == -0.4
    LiveProvider.fail = False
    with factory() as db:
        db.scalar(select(NflGame)).status = "FINAL"
        db.commit()
    await scheduler._sync_stats_and_score(league_id, 1)
    assert snapshot()[1:] == (-0.4, 1)  # Partial final feed cannot overwrite live stats.
    FinalProvider.week_stats_complete = True
    result = await scheduler._sync_stats_and_score(league_id, 1)
    assert result["source"] == "nflverse" and result["week_completed"] == "true"
    assert snapshot()[1:] == (10, 2)
    calls = LiveProvider.calls
    await scheduler._sync_stats_and_score(league_id, 1)
    assert LiveProvider.calls == calls
    with factory() as db:
        assert db.get(Team, team_id).points_for == 10
        assert db.get(Team, team_id).wins == 1
        stat = db.scalar(select(PlayerWeekStat).where(PlayerWeekStat.player_id == starter_id))
        assert stat.provider == "nflverse"


@pytest.mark.asyncio
async def test_scoring_job_runs_once_per_five_minute_bucket(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    class Clock:
        now_value = datetime(2026, 9, 13, 20, tzinfo=UTC)

        @classmethod
        def now(cls, tz):
            return cls.now_value

    monkeypatch.setattr("app.jobs.scheduler.datetime", Clock)
    scheduler = LeagueScheduler(
        factory, SimpleNamespace(start=lambda _: None), SimpleNamespace(settings=Settings()), 5,
    )
    async def noop(*args):
        pass
    monkeypatch.setattr(scheduler, "_execute_manager_job", noop)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026)
        league.status = "REGULAR_SEASON"
        league.current_week = 1
        db.commit()
    for minutes, expected_jobs in [(0, 1), (4, 1), (5, 2), (5, 2), (10, 3)]:
        Clock.now_value = datetime(2026, 9, 13, 20, tzinfo=UTC) + timedelta(minutes=minutes)
        scheduler.tick()
        with factory() as db:
            jobs = list(db.scalars(select(JobRun).where(JobRun.job_name == "nfl_stats_scoring")))
            assert len(jobs) == expected_jobs
    await scheduler.stop()
