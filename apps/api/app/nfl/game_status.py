from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from app.nfl.contracts import NFLGameRecord


def normalize_team(team: str) -> str:
    return {"LA": "LAR", "JAC": "JAX", "WSH": "WAS"}.get(team.upper(), team.upper())


class EspnGameStatusProvider:
    """Enrich scheduled NFL games with explicit scoreboard states, never time estimates."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def update_games(self, games: list[NFLGameRecord]) -> list[NFLGameRecord]:
        if not games:
            return []
        first = min(game.kickoff_at for game in games) - timedelta(days=1)
        last = max(game.kickoff_at for game in games) + timedelta(days=1)
        response = await self._client.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": f"{first:%Y%m%d}-{last:%Y%m%d}", "limit": 1000},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("NFL scoreboard is missing its events")
        indexed = {
            (normalize_team(game.home_team), normalize_team(game.away_team)): game
            for game in games
        }
        updated = {}
        checked_at = datetime.now(UTC).isoformat()
        for event in payload["events"]:
            for competition in event.get("competitions", []):
                teams = {
                    entry.get("homeAway"): normalize_team(entry.get("team", {}).get(
                        "abbreviation", "",
                    ))
                    for entry in competition.get("competitors", [])
                }
                game = indexed.get((teams.get("home", ""), teams.get("away", "")))
                if game is None:
                    continue
                kickoff = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                if abs((kickoff - game.kickoff_at).total_seconds()) > 24 * 3600:
                    continue
                status = (competition.get("status") or event.get("status") or {}).get("type", {})
                state, name = status.get("state"), status.get("name")
                interruptions = {
                    "STATUS_DELAYED": "DELAYED", "STATUS_POSTPONED": "POSTPONED",
                    "STATUS_SUSPENDED": "SUSPENDED", "STATUS_CANCELED": "CANCELLED",
                    "STATUS_CANCELLED": "CANCELLED",
                }
                if name in interruptions:
                    current = interruptions[name]
                elif state == "post" and status.get("completed") is True:
                    current = "FINAL"
                elif state == "in" and status.get("completed") is False:
                    current = "LIVE"
                elif state == "pre":
                    current = "SCHEDULED"
                else:
                    continue
                updated[game.provider_id] = replace(
                    game, status=current, payload={
                        **game.payload,
                        "game_status": {
                            "source": "espn", "checked_at": checked_at,
                            "detail": str(status.get("shortDetail") or status.get("description")
                                          or current),
                        },
                    },
                )
        return [updated.get(game.provider_id, game) for game in games]
