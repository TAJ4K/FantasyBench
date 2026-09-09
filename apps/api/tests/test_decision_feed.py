from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.entities import LeagueEvent, Player
from app.services.initialization import initialize_league


def test_decisions_cursor_filters_and_enrichment(db: Session, app_client: TestClient) -> None:
    league = initialize_league(db, nfl_season=2026, settings={})
    other = initialize_league(db, nfl_season=2025, settings={})
    team = league.teams[0]
    player = Player(full_name="Feed Player", position="RB", active=True)
    db.add(player)
    db.flush()
    instant = datetime(2026, 9, 9, tzinfo=UTC)
    for index in range(5):
        db.add(LeagueEvent(
            id=f"event-{index}", league_id=league.id, team_id=team.id,
            event_type="TRADE_PROPOSED", occurred_at=instant,
            data={"player_id": player.id}, public_commentary="Public rationale",
        ))
    db.add_all([
        LeagueEvent(id="hidden", league_id=league.id, event_type="TRADE_PROPOSED",
                    visibility="PRIVATE", occurred_at=instant),
        LeagueEvent(id="other", league_id=other.id, event_type="TRADE_PROPOSED",
                    occurred_at=instant),
        LeagueEvent(id="lineup", league_id=league.id, event_type="LINEUP_SET",
                    occurred_at=instant),
    ])
    db.commit()
    params = {"league_id": league.id, "kind": "TRADE", "limit": 2}
    first = app_client.get("/api/v1/league/decisions", params=params)
    assert first.status_code == 200
    page = first.json()
    assert [item["id"] for item in page["items"]] == ["event-4", "event-3"]
    assert page["items"][0]["team"]["name"] == team.name
    assert page["items"][0]["player"]["full_name"] == "Feed Player"
    assert page["items"][0]["kind"] == "TRADE"
    # A newly inserted event must not shift subsequent pages.
    db.add(LeagueEvent(id="new", league_id=league.id, event_type="TRADE_PROPOSED",
                       occurred_at=instant + timedelta(seconds=1)))
    db.commit()
    seen = [item["id"] for item in page["items"]]
    while page["next_cursor"]:
        page = app_client.get("/api/v1/league/decisions", params={
            **params, **page["next_cursor"],
        }).json()
        seen.extend(item["id"] for item in page["items"])
    assert seen == [f"event-{i}" for i in range(4, -1, -1)]
    assert app_client.get("/api/v1/league/decisions", params={
        **params, "before_id": "event-3",
    }).status_code == 422
    empty = app_client.get("/api/v1/league/decisions", params={
        **params, "kind": "WAIVER",
    }).json()
    assert empty == {"items": [], "next_cursor": None}
