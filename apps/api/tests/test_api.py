from __future__ import annotations

from fastapi.testclient import TestClient

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
