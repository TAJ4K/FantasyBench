from sqlalchemy.orm import Session

from app.agents.tools import LeagueToolbox
from app.models.entities import Player, RosterAssignment
from app.services.initialization import initialize_league


def test_candidate_limit_applies_after_fantasy_filter_and_ranking(db: Session) -> None:
    league = initialize_league(db, nfl_season=2026)
    team = league.teams[0]
    noise = [Player(full_name=f"AAA Lineman {i}", position="OL", active=True) for i in range(110)]
    target = Player(
        full_name="ZZZ Useful RB",
        position="RB",
        nfl_team="SEA",
        active=True,
        metadata_json={"rank": 120},
    )
    owned = Player(
        full_name="Owned", position="WR", nfl_team="SEA", active=True, metadata_json={"rank": 1}
    )
    unsigned = Player(
        full_name="AAA Unsigned", position="RB", active=True, metadata_json={"rank": 2}
    )
    db.add_all([*noise, target, owned, unsigned])
    db.flush()
    db.add(
        RosterAssignment(
            league_id=league.id,
            team_id=team.id,
            player_id=owned.id,
            slot_type="BENCH",
            acquired_via="DRAFT",
        )
    )
    db.flush()
    candidates = LeagueToolbox(db, league.id, team.id).get_available_players(limit=1)
    assert [p["player_id"] for p in candidates] == [target.id]
