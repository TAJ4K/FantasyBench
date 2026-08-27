from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models.entities import League, LLMRun, NflGame, TradeThread, WaiverPeriod
from app.schemas.api import LeagueInitializeRequest


def test_fixture_players_are_opt_in() -> None:
    assert LeagueInitializeRequest(nfl_season=2026).seed_fixture_players is False


def test_health_readiness_auth_and_initialization_contract(
    app_client: TestClient, admin_headers: dict[str, str]
) -> None:
    assert app_client.get("/health").json() == {"status": "ok"}
    assert app_client.get("/ready").json() == {"status": "ready", "database": "ok"}

    payload = {"name": "API League", "nfl_season": 2026, "seed_fixture_players": True}
    unauthorized = app_client.post("/api/v1/admin/initialize", json=payload)
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    initialized = app_client.post("/api/v1/admin/initialize", json=payload, headers=admin_headers)
    assert initialized.status_code == 200, initialized.text
    body = initialized.json()
    assert body["draft"]["status"] == "NOT_STARTED"

    teams = app_client.get("/api/v1/teams").json()
    assert len(teams) == 8
    assert len({team["model_identifier"] for team in teams}) == 8
    assert app_client.get("/api/v1/draft").json()["picks_made"] == 0
    assert app_client.get("/api/v1/league/status").json()["draft_status"] == "NOT_STARTED"

    # Initialization is idempotent and, critically, never substitutes for START DRAFT.
    again = app_client.post("/api/v1/admin/initialize", json=payload, headers=admin_headers)
    assert again.status_code == 200, again.text
    assert again.json()["league"]["id"] == body["league"]["id"]
    assert again.json()["draft"]["status"] == "NOT_STARTED"


def test_major_empty_state_reads_are_frontend_safe(
    app_client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = app_client.post(
        "/api/v1/admin/initialize",
        json={"nfl_season": 2026, "seed_fixture_players": True},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    expected_types = {
        "/api/v1/league": dict,
        "/api/v1/league/settings": dict,
        "/api/v1/league/actions": list,
        "/api/v1/overview": dict,
        "/api/v1/teams": list,
        "/api/v1/players": dict,
        "/api/v1/draft/order": list,
        "/api/v1/draft/picks": dict,
        "/api/v1/draft/available": dict,
        "/api/v1/transactions": dict,
        "/api/v1/events": dict,
        "/api/v1/matchups": list,
        "/api/v1/standings": list,
        "/api/v1/llm/usage": dict,
        "/api/v1/llm/runs": dict,
        "/api/v1/trades": list,
    }
    for path, expected_type in expected_types.items():
        result = app_client.get(path)
        assert result.status_code == 200, f"{path}: {result.text}"
        assert isinstance(result.json(), expected_type), path


def test_spectator_overview_is_complete_and_frontend_safe(
    app_client: TestClient, admin_headers: dict[str, str]
) -> None:
    initialized = app_client.post(
        "/api/v1/admin/initialize",
        json={"nfl_season": 2026, "seed_fixture_players": True},
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text

    response = app_client.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    overview = response.json()

    assert overview["league"]["status"] == "PRE_DRAFT"
    assert overview["draft"]["picks_made"] == 0
    assert overview["draft"]["total_picks"] == 120
    assert overview["matchups"] == []
    assert overview["draft_picks"] == []
    assert overview["upcoming_actions"] == []
    assert overview["metrics"] == {
        "league_points": 0.0,
        "public_decisions": 0,
        "current_week_decisions": 0,
        "llm_usage": {
            "requests": 0,
            "cost_usd": 0,
            "errors": 0,
            "success_rate": None,
            "points_per_dollar": None,
        },
    }
    assert {team["name"] for team in overview["teams"]} == {
        "Good Company",
        "The Long Context",
        "Deep Value",
        "First Principles",
        "Flash Forward",
        "Gradient Ascent",
        "Latent Upside",
        "Moonshot Capital",
    }
    assert all(team["roster"] == [] for team in overview["teams"])
    assert all(team["usage"]["points_per_dollar"] is None for team in overview["teams"])
    assert all(team["manager_profile"]["withheld"] is False for team in overview["teams"])
    assert overview["events"][0]["event_type"] == "LEAGUE_INITIALIZED"
    assert overview["events"][0]["kind"] == "SYSTEM"


def test_upcoming_actions_uses_persisted_deadlines(
    app_client: TestClient, admin_headers: dict[str, str], engine: Engine
) -> None:
    initialized = app_client.post(
        "/api/v1/admin/initialize",
        json={"nfl_season": 2026},
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    now = datetime.now(UTC)
    with Session(engine) as db:
        league = db.scalar(select(League))
        assert league is not None
        league.current_week = 1
        db.add(
            WaiverPeriod(
                league_id=league.id,
                season=league.nfl_season,
                week=1,
                status="OPEN",
                deadline_at=now + timedelta(hours=1),
                processing_at=now + timedelta(hours=2),
            )
        )
        db.add(
            NflGame(
                season=league.nfl_season,
                week=1,
                provider_game_id="future-game",
                kickoff_at=now + timedelta(hours=3),
                home_team="SEA",
                away_team="SF",
            )
        )
        db.add(
            TradeThread(
                league_id=league.id,
                initiator_team_id=league.teams[0].id,
                recipient_team_id=league.teams[1].id,
                status="PROPOSED",
                expires_at=now + timedelta(hours=4),
            )
        )
        db.commit()

    actions = app_client.get("/api/v1/league/actions").json()
    assert [item["action"] for item in actions] == [
        "CLAIMS_LOCK",
        "WAIVERS_PROCESS",
        "GAME_LOCK",
        "TRADE_EXPIRES",
    ]


def test_overview_points_per_dollar_tracks_live_points_and_spend(
    app_client: TestClient, admin_headers: dict[str, str], engine: Engine
) -> None:
    initialized = app_client.post(
        "/api/v1/admin/initialize",
        json={"nfl_season": 2026},
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    with Session(engine) as db:
        league = db.scalar(select(League))
        assert league is not None
        team = league.teams[0]
        team.points_for = 250.0
        db.add(
            LLMRun(
                league_id=league.id,
                team_id=team.id,
                model=team.model_identifier,
                decision_type="LINEUP",
                prompt_version="test",
                cost_usd=2.5,
                success=True,
            )
        )
        db.commit()

    overview = app_client.get("/api/v1/overview").json()
    team = next(item for item in overview["teams"] if item["points_for"] == 250.0)
    assert team["usage"]["points_per_dollar"] == 100.0
    assert overview["metrics"]["llm_usage"]["points_per_dollar"] == 100.0
