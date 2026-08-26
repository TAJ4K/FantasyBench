from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import FantasyWeek, Matchup, Team


def round_robin_pairings(team_ids: Sequence[str]) -> list[list[tuple[str, str]]]:
    """Return a deterministic single round-robin using the circle method."""
    teams = list(team_ids)
    if len(teams) < 2 or len(teams) % 2:
        raise ValueError("A round-robin requires an even number of at least two teams.")

    rounds: list[list[tuple[str, str]]] = []
    rotating = teams[:]
    for round_index in range(len(teams) - 1):
        pairs: list[tuple[str, str]] = []
        for index in range(len(teams) // 2):
            left, right = rotating[index], rotating[-index - 1]
            # Alternate home designation so the fixed first team is not always home.
            if (round_index + index) % 2:
                left, right = right, left
            pairs.append((left, right))
        rounds.append(pairs)
        rotating = [rotating[0], rotating[-1], *rotating[1:-1]]
    return rounds


def generate_schedule(
    db: Session,
    league_id: str,
    *,
    regular_season_weeks: int,
) -> list[Matchup]:
    """Idempotently create a deterministic regular-season schedule."""
    existing = list(
        db.scalars(
            select(Matchup)
            .where(Matchup.league_id == league_id)
            .order_by(Matchup.week, Matchup.matchup_number)
        )
    )
    if existing:
        return existing

    teams = list(
        db.scalars(
            select(Team).where(Team.league_id == league_id).order_by(Team.draft_position, Team.id)
        )
    )
    pairings = round_robin_pairings([team.id for team in teams])
    matchups: list[Matchup] = []
    for week in range(1, regular_season_weeks + 1):
        db.add(FantasyWeek(league_id=league_id, week=week, status="SCHEDULED"))
        cycle = (week - 1) // len(pairings)
        week_pairs = pairings[(week - 1) % len(pairings)]
        for number, (home_id, away_id) in enumerate(week_pairs, start=1):
            if cycle % 2:
                home_id, away_id = away_id, home_id
            matchup = Matchup(
                league_id=league_id,
                week=week,
                matchup_number=number,
                home_team_id=home_id,
                away_team_id=away_id,
                status="SCHEDULED",
            )
            db.add(matchup)
            matchups.append(matchup)
    db.flush()
    return matchups
