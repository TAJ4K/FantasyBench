from __future__ import annotations

from typing import Any

import httpx

from app.nfl.contracts import NFLGameRecord, NFLPlayerRecord, NFLStatRecord


class SleeperProvider:
    """Read-only Sleeper adapter for player metadata.

    Sleeper does not provide a suitable authoritative schedule/stat feed here, so those
    optional hooks return no records and can be replaced by another provider independently.
    """

    name = "sleeper"

    def __init__(
        self,
        *,
        base_url: str = "https://api.sleeper.app/v1",
        timeout_seconds: float = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_players(self, season: int) -> list[NFLPlayerRecord]:
        del season  # Sleeper's player catalog is current rather than season-scoped.
        response = await self._client.get("/players/nfl")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Sleeper player response must be an object")
        records: list[NFLPlayerRecord] = []
        for sleeper_id, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            position = raw.get("position")
            if position == "DEF":
                position = "DST"
            full_name = raw.get("full_name") or _full_name(raw)
            if not full_name and position == "DST" and raw.get("team"):
                full_name = f"{raw['team']} Defense"
            if not position or not full_name:
                continue
            external = {"sleeper": str(sleeper_id)}
            for key in ("gsis_id", "espn_id", "sportradar_id", "yahoo_id"):
                if raw.get(key) is not None:
                    external[key.removesuffix("_id")] = str(raw[key])
            injury = raw.get("injury_status")
            status = str(raw.get("status") or ("INJURED" if injury else "ACTIVE")).upper()
            active_value = raw.get("active")
            active = (
                bool(active_value)
                if active_value is not None
                else status not in {"INACTIVE", "RETIRED"}
            )
            records.append(
                NFLPlayerRecord(
                    provider_id=str(sleeper_id),
                    full_name=str(full_name),
                    first_name=_string(raw.get("first_name")),
                    last_name=_string(raw.get("last_name")),
                    position=str(position).upper(),
                    nfl_team=_string(raw.get("team")),
                    active=active,
                    status=status,
                    injury_status=_string(injury),
                    gsis_id=_string(raw.get("gsis_id")),
                    external_ids=external,
                    metadata={
                        key: raw[key]
                        for key in ("age", "years_exp", "depth_chart_position", "depth_chart_order")
                        if raw.get(key) is not None
                    },
                )
            )
        return records

    async def get_schedule(self, season: int) -> list[NFLGameRecord]:
        return []

    async def get_week_stats(self, season: int, week: int) -> list[NFLStatRecord]:
        return []

    async def get_injuries(self, season: int, week: int) -> list[NFLPlayerRecord]:
        return [p for p in await self.get_players(season) if p.injury_status]


def _full_name(raw: dict[str, Any]) -> str:
    return " ".join(str(raw.get(key) or "").strip() for key in ("first_name", "last_name")).strip()


def _string(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None
