from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.contracts import LLMRequest
from app.agents.fake import DeterministicFakeProvider
from app.models.entities import (
    Draft,
    DraftPick,
    LeagueEvent,
    Matchup,
    Player,
    RosterAssignment,
    Team,
    Transaction,
    WaiverPeriod,
)
from app.schemas.decisions import DraftDecision
from app.services.competition import complete_matchup
from app.services.draft import DraftService, team_for_pick
from app.services.initialization import initialize_league
from app.services.rosters import RosterService
from app.services.scoring import persist_player_score
from app.services.trades import accept_trade, propose_trade
from app.services.waivers import process_waivers, submit_claims

STARTER_TEMPLATE = ("QB", "RB", "RB", "WR", "WR", "TE", "RB", "DST", "K")
BENCH_TEMPLATE = ("QB", "RB", "WR", "TE", "WR", "RB")
DRAFT_TEMPLATE = STARTER_TEMPLATE + BENCH_TEMPLATE


def _lineup(roster: list[RosterAssignment]) -> dict[str, str]:
    by_position: dict[str, list[str]] = {}
    for assignment in roster:
        by_position.setdefault(assignment.player.position, []).append(assignment.player_id)
    return {
        "QB": by_position["QB"][0],
        "RB1": by_position["RB"][0],
        "RB2": by_position["RB"][1],
        "WR1": by_position["WR"][0],
        "WR2": by_position["WR"][1],
        "TE": by_position["TE"][0],
        "FLEX": by_position["RB"][2],
        "DST": by_position["DST"][0],
        "K": by_position["K"][0],
    }


@pytest.mark.asyncio
async def test_complete_network_free_league_lifecycle(db: Session, app_client: TestClient) -> None:
    league = initialize_league(db, name="E2E League", nfl_season=2026)
    db.flush()
    teams = list(
        db.scalars(select(Team).where(Team.league_id == league.id).order_by(Team.draft_position))
    )
    draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
    assert len(teams) == 8
    assert draft is not None and draft.status == "NOT_STARTED"
    assert db.scalar(select(func.count(DraftPick.id))) == 0

    # Rank players in the exact overall-pick sequence. Each fake manager therefore
    # receives a legal starter core plus six bench players after the snake draft.
    occurrence = Counter[str]()
    players: list[Player] = []
    for pick_number in range(1, 121):
        team_id = team_for_pick(draft.order, pick_number)
        position = DRAFT_TEMPLATE[occurrence[team_id]]
        occurrence[team_id] += 1
        players.append(
            Player(
                full_name=f"Draft Player {pick_number:03d}",
                position=position,
                nfl_team="SEA",
                active=True,
                metadata_json={"rank": pick_number},
            )
        )
    waiver_target = Player(
        full_name="Waiver Upgrade",
        position="RB",
        nfl_team="SEA",
        active=True,
        metadata_json={"rank": 121},
    )
    db.add_all([*players, waiver_target])
    db.flush()
    db.commit()

    service = DraftService(db)
    service.start(league.id)  # This is the explicit start boundary under test.
    provider = DeterministicFakeProvider()
    for _ in range(120):
        turn = service.current(league.id)
        assert turn is not None
        owned = select(RosterAssignment.player_id).where(RosterAssignment.league_id == league.id)
        available = list(
            db.scalars(select(Player).where(Player.id.not_in(owned)).order_by(Player.full_name))
        )
        decision = await provider.decide(
            LLMRequest(
                league_id=league.id,
                team_id=turn.team.id,
                model=turn.team.model_identifier,
                decision_type="DRAFT",
                prompt_version="draft_e2e_v1",
                system_prompt="Return one legal draft selection.",
                user_prompt="Select the best ranked available player.",
                response_model=DraftDecision,
                metadata={
                    "context": {
                        "available_players": [
                            {
                                "id": player.id,
                                "rank": player.metadata_json["rank"],
                                "position": player.position,
                            }
                            for player in available
                        ]
                    }
                },
            )
        )
        parsed = DraftDecision.model_validate(decision.parsed)
        pick = service.make_pick(
            league.id,
            parsed.player_id,
            public_reasoning=parsed.public_reasoning,
            confidence=parsed.confidence,
            reveal_delay_seconds=0,
        )
        service.reveal_pick(pick.id, force=True)
        db.commit()

    db.refresh(draft)
    picks = list(db.scalars(select(DraftPick).order_by(DraftPick.pick_number)))
    assignments = list(db.scalars(select(RosterAssignment)))
    assert draft.status == "COMPLETED"
    assert len(picks) == len(assignments) == 120
    assert len({pick.player_id for pick in picks}) == 120
    assert all(pick.state == "REVEALED" and pick.public_reasoning for pick in picks)
    assert [pick.team_id for pick in picks] == [
        team_for_pick(draft.order, number) for number in range(1, 121)
    ]
    assert (
        db.scalar(
            select(func.count(LeagueEvent.id)).where(
                LeagueEvent.event_type == "DRAFT_PICK_REVEALED"
            )
        )
        == 120
    )

    roster_service = RosterService(db)
    rosters: dict[str, list[RosterAssignment]] = {}
    for team in teams:
        roster = list(
            db.scalars(select(RosterAssignment).where(RosterAssignment.team_id == team.id))
        )
        assert len(roster) == 15
        roster_service.validate_lineup(team.id, _lineup(roster))
        rosters[team.id] = roster

    # Full rosters force the waiver engine to exercise its atomic drop/add path.
    waiver_team = teams[0]
    dropped = next(item for item in rosters[waiver_team.id] if item.player.position == "RB")
    period = db.scalar(
        select(WaiverPeriod).where(
            WaiverPeriod.league_id == league.id,
            WaiverPeriod.week == 1,
        )
    )
    assert period is not None
    period.deadline_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    submit_claims(
        db,
        waiver_period_id=period.id,
        team_id=waiver_team.id,
        claims=[
            {
                "add_player_id": waiver_target.id,
                "drop_player_id": dropped.player_id,
                "priority": 1,
            }
        ],
        public_reasoning="Upgrade running-back depth.",
    )
    claims = process_waivers(db, waiver_period_id=period.id, idempotency_key="e2e-waivers-1")
    assert claims[0].status == "WON"
    db.commit()

    # Swap same-position bench assets so both post-trade rosters remain legal.
    refreshed = {
        team.id: list(
            db.scalars(select(RosterAssignment).where(RosterAssignment.team_id == team.id))
        )
        for team in teams[:2]
    }
    first_asset = next(item for item in refreshed[teams[0].id] if item.player.position == "WR")
    second_asset = next(item for item in refreshed[teams[1].id] if item.player.position == "WR")
    thread, offer = propose_trade(
        db,
        league_id=league.id,
        proposer_team_id=teams[0].id,
        recipient_team_id=teams[1].id,
        send_player_ids=[first_asset.player_id],
        receive_player_ids=[second_asset.player_id],
        message="A position-neutral benchmark trade.",
    )
    accept_trade(db, offer_id=offer.id, accepting_team_id=teams[1].id)
    assert thread.status == "PROCESSED"
    db.commit()

    league.current_week = 1
    for team in teams:
        roster = list(
            db.scalars(select(RosterAssignment).where(RosterAssignment.team_id == team.id))
        )
        lineup = _lineup(roster)
        roster_service.set_lineup(team.id, lineup)
        # Persist the weekly decision separately from mutable roster slot state.
        from app.models.entities import LineupDecision

        db.add(
            LineupDecision(
                league_id=league.id,
                team_id=team.id,
                week=1,
                lineup=lineup,
                public_reasoning="Deterministic highest-ranked legal lineup.",
                source="FAKE",
            )
        )
        for index, player_id in enumerate(lineup.values(), 1):
            persist_player_score(
                db,
                league_id=league.id,
                player_id=player_id,
                season=league.nfl_season,
                week=1,
                raw_stats={"rushing_yards": 20 + index, "receptions": index % 4},
                scoring_config=league.scoring_config,
            )
    db.commit()

    week_one = list(
        db.scalars(
            select(Matchup)
            .where(Matchup.league_id == league.id, Matchup.week == 1)
            .order_by(Matchup.matchup_number)
        )
    )
    assert len(week_one) == 4
    for matchup in week_one:
        complete_matchup(db, matchup_id=matchup.id, season=league.nfl_season)
    db.commit()
    assert all(matchup.status == "COMPLETE" for matchup in week_one)
    assert sum(team.wins + team.losses + team.ties for team in teams) == 8

    # Exercise the website-facing read surface against the exact same SQLite state.
    reads = {
        "/api/v1/teams": 8,
        "/api/v1/draft/picks?limit=200": 120,
        "/api/v1/transactions?limit=200": 124,
        "/api/v1/matchups/1": 4,
        "/api/v1/weeks/1/scores": 72,
        "/api/v1/standings": 8,
    }
    for path, expected_count in reads.items():
        response = app_client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        payload = response.json()
        items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
        assert len(items) == expected_count, path

    transactions = list(db.scalars(select(Transaction)))
    assert len(transactions) == 124  # 120 draft + waiver add/drop + two trade transfers.
