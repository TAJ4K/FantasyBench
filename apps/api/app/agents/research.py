from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agents.tools import LeagueToolbox
from app.models.entities import Player, RosterAssignment, Team


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RankingsArgs(Arguments):
    position: Literal["QB", "RB", "WR", "TE", "K", "DST"]
    pool: Literal["all", "free_agents", "rostered"]
    metric: Literal["season_points", "points_per_recorded_game", "last_3_games_average"]
    limit: int = Field(default=10, ge=1, le=20)


class PlayerArgs(Arguments):
    player_id: str


class RosterArgs(Arguments):
    team_id: str


class NFLTeamArgs(Arguments):
    nfl_team: str = Field(min_length=2, max_length=5)


TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "player_rankings": (
        RankingsArgs,
        "Compare league-scored players by position, ownership and "
        "performance. Includes fantasy owner IDs for trade targets. Missing stats "
        "are unknown, not zero. Current-week points are excluded from rankings.",
    ),
    "player_profile": (
        PlayerArgs,
        "Inspect a player's game log, usage stats, fantasy points, "
        "availability, owner and any stored news. Use exact player_id.",
    ),
    "team_roster": (
        RosterArgs,
        "Inspect a fantasy team's roster and performance before offering "
        "a trade. Only teams in this league are accessible.",
    ),
    "nfl_team_context": (
        NFLTeamArgs,
        "Inspect fantasy-position teammates, depth order and "
        "injuries, including a receiver's quarterbacks. NFL abbreviation required. "
        "Depth order is provider metadata, not a confirmed starting lineup.",
    ),
}


def definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": model.model_json_schema(),
            },
        }
        for name, (model, description) in TOOLS.items()
    ]


class ManagerResearch:
    def __init__(self, toolbox: LeagueToolbox) -> None:
        self.toolbox = toolbox
        self.db = toolbox.db
        self.owners = {
            row.player_id: row.team_id
            for row in self.db.scalars(
                select(RosterAssignment).where(RosterAssignment.league_id == toolbox.league_id)
            )
        }
        self.teams = {
            t.id: t.name
            for t in self.db.scalars(select(Team).where(Team.league_id == toolbox.league_id))
        }

    def player(self, player: Player) -> dict[str, Any]:
        result = self.toolbox.get_player(player.id)
        owner = self.owners.get(player.id)
        result.update(owner_team_id=owner, owner_team_name=self.teams.get(owner or ""))
        return result

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            raise ValueError("Unknown research tool; use only the supplied read-only tools.")
        args = TOOLS[name][0].model_validate(arguments)
        if isinstance(args, RankingsArgs):
            players = self.db.scalars(
                select(Player).where(Player.position == args.position, Player.active.is_(True))
            ).all()
            ranked = [
                self.player(p)
                for p in players
                if args.pool == "all" or (p.id in self.owners) == (args.pool == "rostered")
            ]
            ranked.sort(
                key=lambda p: (
                    p["performance"][args.metric] is None,
                    -(p["performance"][args.metric] or 0),
                    p["name"],
                    p["player_id"],
                )
            )
            return {
                "metric": args.metric,
                "pool": args.pool,
                "players": ranked[: args.limit],
                "matching_players": len(ranked),
            }
        if isinstance(args, PlayerArgs):
            result = self.toolbox.get_player(args.player_id)
            result.update(
                owner_team_id=self.owners.get(args.player_id),
                game_log=self.toolbox.performance.logs.get(args.player_id, [])[:8],
                news=self.toolbox.get_player_news(args.player_id, limit=3),
            )
            return result
        if isinstance(args, RosterArgs):
            return {"team_id": args.team_id, "roster": self.toolbox.get_roster(args.team_id)}
        assert isinstance(args, NFLTeamArgs)
        players = self.db.scalars(
            select(Player)
            .where(
                Player.nfl_team == args.nfl_team.upper(),
                Player.position.in_(("QB", "RB", "WR", "TE", "K", "DST")),
            )
            .order_by(Player.position, Player.full_name)
        ).all()
        return {
            "nfl_team": args.nfl_team.upper(),
            "players": [self.player(p) for p in players][:60],
            "note": "Availability is the latest stored feed; missing news is not proof of health.",
        }
