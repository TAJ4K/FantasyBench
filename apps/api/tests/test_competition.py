from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.entities import LineupDecision, Player, PlayerFantasyScore, RosterAssignment
from app.services.competition import (
    complete_matchup,
    generate_round_robin_matchups,
    standings,
    team_week_total,
)
from app.services.initialization import initialize_league
from app.services.rosters import RosterService


def test_lineup_scores_complete_matchup_and_update_standings_once() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        matchups = generate_round_robin_matchups(db, league_id=league.id, weeks=1)
        matchup = matchups[0]
        assert matchup.home_team_id and matchup.away_team_id
        players = [Player(full_name="Home", position="RB"), Player(full_name="Away", position="RB")]
        db.add_all(players)
        db.flush()
        for team_id, player, total in zip(
            [matchup.home_team_id, matchup.away_team_id], players, [20.5, 10.0], strict=True
        ):
            RosterService(db).add_player(team_id, player.id, acquired_via="DRAFT")
            db.add(
                LineupDecision(
                    league_id=league.id,
                    team_id=team_id,
                    week=1,
                    lineup={"RB1": player.id},
                )
            )
            db.add(
                PlayerFantasyScore(
                    league_id=league.id,
                    player_id=player.id,
                    season=2026,
                    week=1,
                    raw_stats={},
                    breakdown={},
                    total=total,
                    scoring_config_hash="x",
                )
            )
        db.flush()
        complete_matchup(db, matchup_id=matchup.id, season=2026)
        complete_matchup(db, matchup_id=matchup.id, season=2026)
        table = standings(db, league_id=league.id)
        assert matchup.home_score == 20.5
        assert table[0]["team_id"] == matchup.home_team_id
        assert table[0]["wins"] == 1


def test_stale_lineup_decision_does_not_score_a_departed_player() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        league = initialize_league(db, nfl_season=2026, settings={"regular_season_weeks": 1})
        team = league.teams[0]
        player = Player(full_name="Departed Starter", position="RB")
        db.add(player)
        db.flush()
        RosterService(db).add_player(team.id, player.id, acquired_via="DRAFT")
        db.add(
            LineupDecision(
                league_id=league.id,
                team_id=team.id,
                week=1,
                lineup={"RB1": player.id},
            )
        )
        db.add(
            PlayerFantasyScore(
                league_id=league.id,
                player_id=player.id,
                season=2026,
                week=1,
                raw_stats={},
                breakdown={},
                total=30,
                scoring_config_hash="x",
            )
        )
        db.flush()
        assignment = db.scalar(
            select(RosterAssignment).where(RosterAssignment.player_id == player.id)
        )
        assert assignment is not None
        db.delete(assignment)
        db.flush()
        total, breakdown = team_week_total(
            db,
            league_id=league.id,
            team_id=team.id,
            season=2026,
            week=1,
        )
        assert total == 0
        assert breakdown == {}
