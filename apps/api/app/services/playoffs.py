from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.entities import FantasyWeek, League, Matchup
from app.models.enums import LeagueStatus
from app.services.competition import playoff_seeds
from app.services.events import emit_event


def seed_playoffs(db: Session, *, league_id: str) -> list[Matchup]:
    league = db.scalar(select(League).where(League.id == league_id).with_for_update())
    if not league:
        raise NotFoundError("league", league_id)
    team_count = int(league.settings.get("playoff_team_count", 4))
    weeks = list(league.settings.get("playoff_weeks", [16, 17]))
    if team_count != 4 or len(weeks) != 2:
        raise ConflictError(
            "UNSUPPORTED_PLAYOFF_FORMAT",
            "The initial playoff engine supports a four-team, two-week bracket.",
        )
    existing = list(
        db.scalars(
            select(Matchup)
            .where(Matchup.league_id == league_id, Matchup.week.in_(weeks))
            .order_by(Matchup.week, Matchup.matchup_number)
        )
    )
    if existing:
        return existing
    seeds = playoff_seeds(db, league_id=league_id, team_count=team_count)
    by_seed = {row["seed"]: row["team_id"] for row in seeds}
    semifinals = [
        Matchup(
            league_id=league_id,
            week=weeks[0],
            matchup_number=1,
            home_team_id=by_seed[1],
            away_team_id=by_seed[4],
            playoff_round="SEMIFINAL",
        ),
        Matchup(
            league_id=league_id,
            week=weeks[0],
            matchup_number=2,
            home_team_id=by_seed[2],
            away_team_id=by_seed[3],
            playoff_round="SEMIFINAL",
        ),
    ]
    final = Matchup(
        league_id=league_id,
        week=weeks[1],
        matchup_number=1,
        home_team_id=None,
        away_team_id=None,
        playoff_round="CHAMPIONSHIP",
    )
    db.add_all([*semifinals, final])
    for week in weeks:
        if not db.scalar(
            select(FantasyWeek).where(FantasyWeek.league_id == league_id, FantasyWeek.week == week)
        ):
            db.add(FantasyWeek(league_id=league_id, week=week, status="SCHEDULED", is_playoff=True))
    league.status = LeagueStatus.PLAYOFFS.value
    league.current_week = weeks[0]
    emit_event(
        db,
        league_id,
        "PLAYOFFS_SEEDED",
        aggregate_type="LEAGUE",
        aggregate_id=league_id,
        data={"seeds": seeds, "weeks": weeks},
    )
    db.flush()
    return [*semifinals, final]


def advance_playoffs(db: Session, *, league_id: str) -> Matchup:
    league = db.scalar(select(League).where(League.id == league_id).with_for_update())
    if not league:
        raise NotFoundError("league", league_id)
    weeks = list(league.settings.get("playoff_weeks", [16, 17]))
    semis = list(
        db.scalars(
            select(Matchup)
            .where(
                Matchup.league_id == league_id,
                Matchup.week == weeks[0],
                Matchup.playoff_round == "SEMIFINAL",
            )
            .order_by(Matchup.matchup_number)
        )
    )
    final = db.scalar(
        select(Matchup).where(
            Matchup.league_id == league_id,
            Matchup.week == weeks[1],
            Matchup.playoff_round == "CHAMPIONSHIP",
        )
    )
    if len(semis) != 2 or final is None:
        raise ConflictError("PLAYOFFS_NOT_SEEDED", "Seed the playoffs before advancing them.")
    if final.status == "COMPLETE":
        if not final.winner_team_id:
            raise ConflictError(
                "CHAMPIONSHIP_TIED", "Resolve a tied championship before crowning a champion."
            )
        settings = dict(league.settings)
        settings["champion_team_id"] = final.winner_team_id
        league.settings = settings
        league.status = LeagueStatus.COMPLETE.value
        emit_event(
            db,
            league_id,
            "CHAMPION_CROWNED",
            aggregate_type="TEAM",
            aggregate_id=final.winner_team_id,
            team_id=final.winner_team_id,
        )
        db.flush()
        return final
    if any(matchup.status != "COMPLETE" or not matchup.winner_team_id for matchup in semis):
        raise ConflictError("SEMIFINALS_INCOMPLETE", "Both semifinals must have winners.")
    final.home_team_id = semis[0].winner_team_id
    final.away_team_id = semis[1].winner_team_id
    league.current_week = weeks[1]
    emit_event(
        db,
        league_id,
        "CHAMPIONSHIP_SET",
        aggregate_type="MATCHUP",
        aggregate_id=final.id,
        data={"home_team_id": final.home_team_id, "away_team_id": final.away_team_id},
    )
    db.flush()
    return final
