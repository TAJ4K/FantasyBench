from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.prompts import AVAILABILITY_GUIDANCE
from app.core.config import Settings
from app.jobs.manager_automation import ManagerAutomation
from app.services.initialization import initialize_league


@pytest.mark.asyncio
async def test_admin_message_and_availability_reach_every_manager(engine):
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        league = initialize_league(db, nfl_season=2026)
        league_id = league.id
        team_ids = {team.id for team in league.teams}
        db.commit()
    captured = []

    class CaptureProvider:
        async def decide(self, request):
            captured.append(request)
            raise RuntimeError("Stop after capturing prompt")

    automation = ManagerAutomation(factory, CaptureProvider(), Settings(app_env="test"))
    message = "Commissioner: review player availability before choosing starters."
    results = await automation.set_all_lineups(league_id, 1, admin_message=message)
    assert set(results) == team_ids
    assert len(captured) == 8
    assert {request.team_id for request in captured} == team_ids
    for request in captured:
        assert message in request.user_prompt
        assert AVAILABILITY_GUIDANCE in request.system_prompt
        assert request.prompt_version == "lineup_v3"


def test_lineup_review_accepts_admin_message(app_client, admin_headers):
    league = app_client.post(
        "/api/v1/admin/initialize", headers=admin_headers, json={"nfl_season": 2026}
    ).json()["league"]
    automation = AsyncMock()
    automation.set_all_lineups.return_value = {"team": "COMPLETE"}
    app_client.app.state.manager_automation = automation
    response = app_client.post(
        "/api/v1/admin/lineups/review",
        headers=admin_headers,
        json={"admin_message": "Review availability."},
    )
    assert response.status_code == 200
    automation.set_all_lineups.assert_awaited_once_with(
        league["id"], 1, admin_message="Review availability."
    )
    automation.reset_mock()
    assert app_client.post("/api/v1/admin/lineups/review", headers=admin_headers).status_code == 200
    automation.set_all_lineups.assert_awaited_once_with(league["id"], 1, admin_message=None)
