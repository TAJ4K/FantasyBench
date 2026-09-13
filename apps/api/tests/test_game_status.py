from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.jobs.scheduler import LeagueScheduler
from app.models.entities import League, NflGame, Player, RosterAssignment
from app.nfl.contracts import NFLGameRecord
from app.nfl.game_status import EspnGameStatusProvider
from app.services.initialization import initialize_league


def scoreboard_event(state, name, completed):
    return {
        "date": "2026-09-13T20:25Z",
        "status": {"type": {
            "state": state, "name": name, "completed": completed, "shortDetail": "3:12 - 4th",
        }},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"abbreviation": "PHI"}},
            {"homeAway": "away", "team": {"abbreviation": "WSH"}},
        ]}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("state,name,completed,expected", [
    ("in", "STATUS_IN_PROGRESS", False, "LIVE"),
    ("in", "STATUS_HALFTIME", False, "LIVE"),
    ("in", "STATUS_END_PERIOD", False, "LIVE"),
    ("post", "STATUS_FINAL", True, "FINAL"),
    ("pre", "STATUS_SCHEDULED", False, "SCHEDULED"),
    ("in", "STATUS_DELAYED", False, "DELAYED"),
    ("pre", "STATUS_POSTPONED", False, "POSTPONED"),
    ("in", "STATUS_SUSPENDED", False, "SUSPENDED"),
    ("post", "STATUS_CANCELED", True, "CANCELLED"),
])
async def test_explicit_game_states_and_team_aliases(state, name, completed, expected):
    game = NFLGameRecord(
        "2026_01_WAS_PHI", 2026, 1, datetime(2026, 9, 13, 20, 25, tzinfo=UTC), "PHI", "WAS",
    )
    event = scoreboard_event(state, name, completed)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"events": [event]})
    )) as client:
        result = await EspnGameStatusProvider(client=client).update_games([game])
    assert result[0].status == expected
    assert result[0].provider_id == game.provider_id
    assert result[0].payload["game_status"]["detail"] == "3:12 - 4th"
    assert result[0].payload["game_status"]["source"] == "espn"
    assert game.status == "SCHEDULED"


@pytest.mark.asyncio
async def test_scoreboard_does_not_infer_live_from_kickoff_or_match_another_week():
    game = NFLGameRecord(
        "other-week", 2026, 2, datetime(2026, 9, 20, 20, 25, tzinfo=UTC), "PHI", "WAS",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "events": [scoreboard_event("in", "STATUS_IN_PROGRESS", False)],
        })
    )) as client:
        assert await EspnGameStatusProvider(client=client).update_games([game]) == [game]


@pytest.mark.asyncio
async def test_schedule_sync_applies_status_and_clears_badges_on_feed_failure(engine, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026)
        league.current_week = 1
        league_id = league.id
        db.commit()

    class ScheduleProvider:
        name = "nflverse"

        async def get_schedule(self, season):
            return [NFLGameRecord(
                "2026_01_WAS_PHI", season, 1,
                datetime(2026, 9, 13, 20, 25, tzinfo=UTC), "PHI", "WAS",
            )]

        async def aclose(self):
            pass

    fail = False
    def handler(request):
        return httpx.Response(503) if fail else httpx.Response(200, json={
            "events": [scoreboard_event("in", "STATUS_IN_PROGRESS", False)],
        })

    monkeypatch.setattr("app.jobs.scheduler.NflverseProvider", ScheduleProvider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            "app.jobs.scheduler.EspnGameStatusProvider",
            lambda: EspnGameStatusProvider(client=client),
        )
        scheduler = LeagueScheduler(factory, SimpleNamespace(), SimpleNamespace(), 5)
        await scheduler._sync_schedule(league_id)
        with factory() as db:
            game = db.scalar(select(NflGame))
            assert game.status == "LIVE"
            assert game.payload["game_status"]["source"] == "espn"
        fail = True
        await scheduler._sync_schedule(league_id)
        with factory() as db:
            game = db.scalar(select(NflGame))
            assert game.status == "SCHEDULED"
            assert "game_status" not in game.payload


def test_roster_live_indicator_is_scoped_to_current_fresh_games(app_client, admin_headers, engine):
    app_client.post("/api/v1/admin/initialize", json={"nfl_season": 2026}, headers=admin_headers)
    now = datetime.now(UTC)
    with sessionmaker(engine)() as db:
        league = db.scalar(select(League))
        league.current_week = 1
        team = league.teams[0]
        team_id = team.id
        rows = [
            ("PHI", "WAS", "LIVE", 2026, 1, now),
            ("SEA", "NE", "FINAL", 2026, 1, now),
            ("DAL", "NYG", "SCHEDULED", 2026, 1, now),
            ("GB", "MIN", "LIVE", 2026, 1, now - timedelta(minutes=11)),
            ("KC", "BUF", "LIVE", 2026, 2, now),
            ("LAR", "SF", "LIVE", 2025, 1, now),
            ("MIA", "LV", "POSTPONED", 2026, 1, now),
        ]
        for home, away, status, season, week, checked_at in rows:
            db.add(NflGame(
                provider_game_id=f"{season}_{week}_{home}_{away}", season=season, week=week,
                home_team=home, away_team=away, status=status, kickoff_at=now - timedelta(hours=2),
                payload={"game_status": {"checked_at": checked_at.isoformat(), "detail": "Q4"}},
            ))
        nfl_teams = ["PHI", "WSH", "SEA", "DAL", "GB", "KC", "LAR", "MIA", None]
        for index, nfl_team in enumerate(nfl_teams):
            player = Player(full_name=f"Player {index}", position="WR", nfl_team=nfl_team)
            db.add(player)
            db.flush()
            db.add(RosterAssignment(
                league_id=league.id, team_id=team.id, player_id=player.id, slot_type="BENCH",
                acquired_via="DRAFT",
            ))
        db.commit()
    overview = app_client.get("/api/v1/overview").json()
    roster = next(team for team in overview["teams"] if team["id"] == team_id)["roster"]
    assert {row["player"]["nfl_team"] for row in roster if row["live_game"]} == {"PHI", "WSH"}
    game = next(row["live_game"] for row in roster if row["live_game"])
    assert game == {"home_team": "PHI", "away_team": "WAS", "detail": "Q4"}
