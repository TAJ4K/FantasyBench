from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Player

NFL_TEAMS = (
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LV",
    "LAC",
    "LAR",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SF",
    "SEA",
    "TB",
    "TEN",
    "WAS",
)

POSITION_COUNTS = {"RB": 48, "WR": 56, "QB": 24, "TE": 24, "DST": 16, "K": 16}


def seed_fixture_players(db: Session) -> int:
    """Insert a deterministic offline player pool; never overwrites provider data."""
    if (db.scalar(select(func.count(Player.id))) or 0) > 0:
        return 0
    rows: list[Player] = []
    global_rank = 1
    # Tiers keep the fake draft balanced while remaining deterministic.
    remaining = dict(POSITION_COUNTS)
    tier_order = ("RB", "WR", "WR", "RB", "TE", "QB", "WR", "RB", "QB", "TE", "DST", "K")
    indices = {position: 0 for position in POSITION_COUNTS}
    while any(remaining.values()):
        progressed = False
        for position in tier_order:
            if remaining[position] <= 0:
                continue
            indices[position] += 1
            ordinal = indices[position]
            team = NFL_TEAMS[(global_rank - 1) % len(NFL_TEAMS)]
            rows.append(
                Player(
                    full_name=f"Fixture {position} {ordinal:02d}",
                    first_name="Fixture",
                    last_name=f"{position} {ordinal:02d}",
                    position=position,
                    nfl_team=team,
                    status="ACTIVE",
                    active=True,
                    bye_week=5 + ((global_rank - 1) % 10),
                    external_ids={"fixture": f"fixture-{position.lower()}-{ordinal:03d}"},
                    metadata_json={
                        "rank": global_rank,
                        "projection": round(max(1.0, 300.0 - global_rank) / 10, 2),
                        "fixture": True,
                    },
                )
            )
            global_rank += 1
            remaining[position] -= 1
            progressed = True
        if not progressed:
            break
    db.add_all(rows)
    db.flush()
    return len(rows)
