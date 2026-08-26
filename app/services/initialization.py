from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.defaults import (
    DEFAULT_LEAGUE_SETTINGS,
    DEFAULT_MANAGERS,
    DEFAULT_ROSTER_CONFIG,
    DEFAULT_SCORING_CONFIG,
    ManagerDefinition,
)
from app.core.errors import ConflictError
from app.models.entities import Draft, League, Team
from app.models.enums import DraftStatus, LeagueStatus
from app.services.schedule import generate_schedule


def initialize_league(
    db: Session,
    *,
    name: str = "Fantasy Bench",
    nfl_season: int,
    managers: tuple[ManagerDefinition, ...] = DEFAULT_MANAGERS,
    settings: dict[str, Any] | None = None,
) -> League:
    """Create the singleton season league, or return its already initialized row.

    The caller owns commit/rollback. Initialization deliberately never starts the draft.
    """
    configured = deepcopy(DEFAULT_LEAGUE_SETTINGS)
    if settings:
        configured.update(settings)
    expected_teams = int(configured["teams"])
    if len(managers) != expected_teams:
        raise ValueError(f"Expected {expected_teams} manager definitions, got {len(managers)}.")
    if len({manager.key for manager in managers}) != len(managers):
        raise ValueError("Manager keys must be unique.")

    league = db.scalar(select(League).where(League.nfl_season == nfl_season, League.name == name))
    if league is not None:
        if league.locked:
            raise ConflictError(
                "LEAGUE_LOCKED",
                "The league is administratively locked; unlock it before repairing initialization.",
            )
        # A partially initialized row is repaired, but a configured league is never regenerated.
        teams = list(
            db.scalars(
                select(Team).where(Team.league_id == league.id).order_by(Team.draft_position)
            )
        )
        existing_keys = {team.key for team in teams}
        for position, manager in enumerate(managers, start=1):
            if manager.key not in existing_keys:
                db.add(_make_team(league.id, manager, position, configured))
        db.flush()
        teams = list(
            db.scalars(
                select(Team).where(Team.league_id == league.id).order_by(Team.draft_position)
            )
        )
        if len(teams) != expected_teams:
            raise ValueError("Existing league has an incompatible number of teams.")
        draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
        if draft is None:
            draft = _make_draft(league, teams, configured)
            db.add(draft)
            db.flush()
        generate_schedule(
            db,
            league.id,
            regular_season_weeks=int(configured["regular_season_weeks"]),
        )
        return league

    league = League(
        name=name,
        status=LeagueStatus.PRE_DRAFT.value,
        nfl_season=nfl_season,
        current_week=0,
        locked=False,
        settings=configured,
        roster_config=deepcopy(DEFAULT_ROSTER_CONFIG),
        scoring_config=deepcopy(DEFAULT_SCORING_CONFIG),
    )
    db.add(league)
    try:
        db.flush()
    except IntegrityError:
        # The season/name uniqueness constraint is the race guard for two
        # commissioners initializing simultaneously. Re-read the winner.
        db.rollback()
        return initialize_league(
            db,
            name=name,
            nfl_season=nfl_season,
            managers=managers,
            settings=settings,
        )
    teams = [
        _make_team(league.id, manager, position, configured)
        for position, manager in enumerate(managers, start=1)
    ]
    db.add_all(teams)
    db.flush()
    db.add(_make_draft(league, teams, configured))
    db.flush()
    generate_schedule(
        db,
        league.id,
        regular_season_weeks=int(configured["regular_season_weeks"]),
    )
    return league


def _make_team(
    league_id: str,
    manager: ManagerDefinition,
    position: int,
    settings: dict[str, Any],
) -> Team:
    return Team(
        league_id=league_id,
        key=manager.key,
        name=manager.display_name,
        model_display_name=manager.display_name,
        model_identifier=manager.model,
        reasoning_config={"effort": manager.reasoning_effort} if manager.reasoning_effort else {},
        draft_position=position,
        faab_budget=int(settings["faab_starting_budget"]),
        waiver_priority=position,
    )


def _make_draft(league: League, teams: list[Team], settings: dict[str, Any]) -> Draft:
    return Draft(
        league_id=league.id,
        status=DraftStatus.NOT_STARTED.value,
        draft_type=str(settings["draft_type"]),
        rounds=int(settings["draft_rounds"]),
        current_pick_number=1,
        order=[team.id for team in sorted(teams, key=lambda team: team.draft_position)],
    )
