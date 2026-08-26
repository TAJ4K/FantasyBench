from __future__ import annotations

from functools import cmp_to_key
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.entities import (
    FantasyWeek,
    League,
    LineupDecision,
    Matchup,
    PlayerFantasyScore,
    RosterAssignment,
    Team,
)


def generate_round_robin_matchups(
    db: Session, *, league_id: str, weeks: int, replace: bool = False
) -> list[Matchup]:
    """Generate a deterministic circle-method schedule, repeating as needed."""
    teams = list(
        db.scalars(select(Team).where(Team.league_id == league_id).order_by(Team.draft_position))
    )
    if len(teams) < 2:
        raise ConflictError("NOT_ENOUGH_TEAMS", "At least two teams are required for matchups.")
    if len(teams) % 2:
        team_ids: list[str | None] = [team.id for team in teams] + [None]
    else:
        team_ids = [team.id for team in teams]
    rounds: list[list[tuple[str | None, str | None]]] = []
    rotating = team_ids[:]
    for _ in range(len(rotating) - 1):
        rounds.append([(rotating[i], rotating[-i - 1]) for i in range(len(rotating) // 2)])
        rotating = [rotating[0], rotating[-1], *rotating[1:-1]]

    existing = list(db.scalars(select(Matchup).where(Matchup.league_id == league_id)))
    if existing and not replace:
        return sorted(existing, key=lambda matchup: (matchup.week, matchup.matchup_number))
    if replace:
        for matchup in existing:
            if matchup.status != "SCHEDULED":
                raise ConflictError("SCHEDULE_STARTED", "A started schedule cannot be replaced.")
            db.delete(matchup)
        db.flush()

    generated: list[Matchup] = []
    for week in range(1, weeks + 1):
        for number, (first, second) in enumerate(rounds[(week - 1) % len(rounds)], 1):
            if first is None or second is None:
                continue
            # Alternate home designation across cycles and rounds.
            home, away = (first, second) if (week + number) % 2 else (second, first)
            generated.append(
                Matchup(
                    league_id=league_id,
                    week=week,
                    matchup_number=number,
                    home_team_id=home,
                    away_team_id=away,
                )
            )
    db.add_all(generated)
    db.flush()
    return generated


def _lineup_player_ids(db: Session, league_id: str, team_id: str, week: int) -> list[str]:
    decision = db.scalar(
        select(LineupDecision)
        .where(
            LineupDecision.league_id == league_id,
            LineupDecision.team_id == team_id,
            LineupDecision.week == week,
        )
        .order_by(LineupDecision.created_at.desc(), LineupDecision.id.desc())
        .limit(1)
    )
    if decision is not None:
        currently_owned = set(
            db.scalars(
                select(RosterAssignment.player_id).where(
                    RosterAssignment.league_id == league_id,
                    RosterAssignment.team_id == team_id,
                )
            )
        )
        return [
            player_id
            for player_id in dict.fromkeys(decision.lineup.values())
            if player_id in currently_owned
        ]
    # Administrative/imported leagues may represent the active lineup directly.
    return list(
        db.scalars(
            select(RosterAssignment.player_id).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.team_id == team_id,
                RosterAssignment.slot_type == "STARTER",
            )
        )
    )


def team_week_total(
    db: Session, *, league_id: str, team_id: str, season: int, week: int
) -> tuple[float, dict[str, float]]:
    player_ids = _lineup_player_ids(db, league_id, team_id, week)
    if not player_ids:
        return 0.0, {}
    scores = list(
        db.scalars(
            select(PlayerFantasyScore).where(
                PlayerFantasyScore.league_id == league_id,
                PlayerFantasyScore.season == season,
                PlayerFantasyScore.week == week,
                PlayerFantasyScore.player_id.in_(player_ids),
            )
        )
    )
    by_player = {score.player_id: float(score.total) for score in scores}
    breakdown = {player_id: by_player.get(player_id, 0.0) for player_id in player_ids}
    return round(sum(breakdown.values()), 4), breakdown


def calculate_matchup(
    db: Session, *, matchup_id: str, season: int, mark_live: bool = True
) -> dict[str, Any]:
    matchup = db.get(Matchup, matchup_id)
    if matchup is None:
        raise NotFoundError("Matchup", matchup_id)
    home_total, home_players = (
        team_week_total(
            db,
            league_id=matchup.league_id,
            team_id=matchup.home_team_id,
            season=season,
            week=matchup.week,
        )
        if matchup.home_team_id
        else (0.0, {})
    )
    away_total, away_players = (
        team_week_total(
            db,
            league_id=matchup.league_id,
            team_id=matchup.away_team_id,
            season=season,
            week=matchup.week,
        )
        if matchup.away_team_id
        else (0.0, {})
    )
    matchup.home_score = home_total
    matchup.away_score = away_total
    if mark_live and matchup.status == "SCHEDULED":
        matchup.status = "LIVE"
    db.flush()
    return {
        "matchup": matchup,
        "home_player_scores": home_players,
        "away_player_scores": away_players,
    }


def _next_streak(current: str, result: str) -> str:
    if current.startswith(result):
        try:
            return f"{result}{int(current[1:]) + 1}"
        except ValueError:
            pass
    return f"{result}1"


def complete_matchup(db: Session, *, matchup_id: str, season: int) -> Matchup:
    snapshot = db.get(Matchup, matchup_id)
    if snapshot is None:
        raise NotFoundError("Matchup", matchup_id)
    fantasy_week = db.scalar(
        select(FantasyWeek)
        .where(
            FantasyWeek.league_id == snapshot.league_id,
            FantasyWeek.week == snapshot.week,
        )
        .with_for_update()
    )
    matchup = db.scalar(select(Matchup).where(Matchup.id == matchup_id).with_for_update())
    if matchup is None:
        raise NotFoundError("Matchup", matchup_id)
    if matchup.status == "COMPLETE":
        return matchup
    calculate_matchup(db, matchup_id=matchup.id, season=season, mark_live=False)
    if matchup.home_team_id is None or matchup.away_team_id is None:
        raise ConflictError("INVALID_MATCHUP", "A matchup must have two teams before completion.")
    home = db.scalar(select(Team).where(Team.id == matchup.home_team_id).with_for_update())
    away = db.scalar(select(Team).where(Team.id == matchup.away_team_id).with_for_update())
    if home is None or away is None:
        raise ConflictError("INVALID_MATCHUP", "A matchup references a missing team.")
    home.points_for += matchup.home_score
    home.points_against += matchup.away_score
    away.points_for += matchup.away_score
    away.points_against += matchup.home_score
    if matchup.home_score > matchup.away_score:
        home.wins += 1
        away.losses += 1
        home.streak = _next_streak(home.streak, "W")
        away.streak = _next_streak(away.streak, "L")
        matchup.winner_team_id = home.id
    elif matchup.away_score > matchup.home_score:
        away.wins += 1
        home.losses += 1
        away.streak = _next_streak(away.streak, "W")
        home.streak = _next_streak(home.streak, "L")
        matchup.winner_team_id = away.id
    else:
        home.ties += 1
        away.ties += 1
        home.streak = _next_streak(home.streak, "T")
        away.streak = _next_streak(away.streak, "T")
        matchup.winner_team_id = None
    matchup.status = "COMPLETE"

    remaining = db.scalar(
        select(Matchup.id)
        .where(
            Matchup.league_id == matchup.league_id,
            Matchup.week == matchup.week,
            Matchup.id != matchup.id,
            Matchup.status != "COMPLETE",
        )
        .limit(1)
    )
    if remaining is None and fantasy_week is not None:
        fantasy_week.status = "COMPLETE"
    db.flush()
    return matchup


def standings(db: Session, *, league_id: str) -> list[dict[str, Any]]:
    teams = list(db.scalars(select(Team).where(Team.league_id == league_id)))
    league = db.get(League, league_id)
    tiebreakers = list(
        (league.settings if league else {}).get(
            "standings_tiebreakers", ["WIN_PERCENTAGE", "POINTS_FOR"]
        )
    )
    completed = list(
        db.scalars(
            select(Matchup).where(
                Matchup.league_id == league_id,
                Matchup.status == "COMPLETE",
            )
        )
    )

    def percentage(team: Team) -> float:
        games = team.wins + team.losses + team.ties
        return (team.wins + 0.5 * team.ties) / games if games else 0.0

    def compare(first: Team, second: Team) -> int:
        for rule in tiebreakers:
            normalized = str(rule).upper()
            if normalized == "WIN_PERCENTAGE":
                difference = percentage(second) - percentage(first)
            elif normalized == "POINTS_FOR":
                difference = second.points_for - first.points_for
            elif normalized == "POINTS_AGAINST":
                difference = first.points_against - second.points_against
            elif normalized == "HEAD_TO_HEAD":
                first_wins = sum(
                    matchup.winner_team_id == first.id
                    for matchup in completed
                    if {matchup.home_team_id, matchup.away_team_id} == {first.id, second.id}
                )
                second_wins = sum(
                    matchup.winner_team_id == second.id
                    for matchup in completed
                    if {matchup.home_team_id, matchup.away_team_id} == {first.id, second.id}
                )
                difference = float(second_wins - first_wins)
            else:
                continue
            if difference < 0:
                return -1
            if difference > 0:
                return 1
        first_key = (first.name, first.id)
        second_key = (second.name, second.id)
        return -1 if first_key < second_key else (1 if first_key > second_key else 0)

    teams.sort(key=cmp_to_key(compare))
    playoff_count = int((league.settings if league else {}).get("playoff_team_count", 4))
    return [
        {
            "rank": rank,
            "team_id": team.id,
            "team_name": team.name,
            "wins": team.wins,
            "losses": team.losses,
            "ties": team.ties,
            "win_percentage": round(percentage(team), 4),
            "points_for": round(team.points_for, 4),
            "points_against": round(team.points_against, 4),
            "streak": team.streak,
            "faab_remaining": team.faab_budget,
            "playoff_position": rank if rank <= playoff_count else None,
        }
        for rank, team in enumerate(teams, 1)
    ]


def playoff_seeds(db: Session, *, league_id: str, team_count: int) -> list[dict[str, Any]]:
    table = standings(db, league_id=league_id)
    if team_count < 1 or team_count > len(table):
        raise ConflictError("INVALID_PLAYOFF_SIZE", "Playoff team count is out of range.")
    return [{**row, "seed": seed} for seed, row in enumerate(table[:team_count], 1)]
