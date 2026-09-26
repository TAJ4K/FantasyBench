from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.contracts import LLMRequest, LLMResult, ToolCallsDecision
from app.agents.errors import LLMProviderError
from app.agents.openrouter import OpenRouterProvider
from app.agents.performance import PerformanceSnapshot
from app.agents.research import ManagerResearch
from app.agents.service import LLMInvocationService
from app.agents.tools import LeagueToolbox
from app.api.read_api import _llm_run_is_public
from app.api.serialization import public_llm_run
from app.core.errors import NotFoundError
from app.models.entities import LeagueEvent, LLMRun, Player, PlayerWeekStat, RosterAssignment
from app.schemas.decisions import WaiverDecision
from app.services.initialization import initialize_league
from app.services.waivers import ensure_waiver_period, process_waivers


def seed(db: Session):
    league = initialize_league(db, nfl_season=2026)
    league.current_week = 4
    league.scoring_config = {"linear": {"receptions": 1, "receiving_yards": 0.1}}
    players = [
        Player(full_name=name, position="WR", nfl_team="SEA", active=True)
        for name in ("Slumping", "Breakout", "Unknown", "Zero")
    ]
    db.add_all(players)
    db.flush()
    for player, week, catches, yards, season in [
        (players[0], 1, 2, 20, 2026),
        (players[0], 3, 1, 10, 2026),
        (players[1], 2, 8, 100, 2026),
        (players[1], 3, 10, 100, 2026),
        (players[0], 4, 40, 900, 2026),  # Live score cannot distort completed-week ranks.
        (players[0], 5, 50, 1000, 2026),
        (players[0], 1, 50, 1000, 2025),
        (players[3], 1, 0, 0, 2026),
    ]:
        db.add(
            PlayerWeekStat(
                player_id=player.id,
                season=season,
                week=week,
                provider="nflverse",
                raw_stats={"receptions": catches, "receiving_yards": yards},
            )
        )
    db.add(
        RosterAssignment(
            league_id=league.id,
            team_id=league.teams[0].id,
            player_id=players[0].id,
            slot_type="BENCH",
            acquired_via="DRAFT",
        )
    )
    db.commit()
    return league, players


def test_performance_uses_league_rules_and_distinguishes_missing_live_and_zero(db: Session):
    league, players = seed(db)
    snapshot = PerformanceSnapshot(db, league)
    first = snapshot.summary(players[0].id)
    assert first["season_points"] == 6
    assert first["points_per_recorded_game"] == 3
    assert first["games_with_stats"] == 2  # Missing week 2 is not an invented zero.
    assert first["position_rank_by_total"] == 2
    assert first["current_week_points"] == 130
    assert [r["week"] for r in snapshot.logs[players[0].id]] == [4, 3, 1]
    assert snapshot.logs[players[0].id][0]["provisional"] is True
    assert snapshot.summary(players[2].id)["season_points"] is None
    assert snapshot.summary(players[3].id)["season_points"] == 0
    league.scoring_config = {"linear": {"receiving_yards": 0.1}}
    assert PerformanceSnapshot(db, league).summary(players[1].id)["season_points"] == 20


def test_rankings_find_free_agents_and_scope_ownership_to_league(db: Session):
    league, players = seed(db)
    toolbox = LeagueToolbox(db, league.id, league.teams[0].id)
    research = ManagerResearch(toolbox)
    result = research.execute(
        "player_rankings",
        {"position": "WR", "pool": "free_agents", "metric": "last_3_games_average", "limit": 2},
    )
    assert [p["name"] for p in result["players"]] == ["Breakout", "Zero"]
    assert (
        research.execute("player_profile", {"player_id": players[0].id})["owner_team_id"]
        == league.teams[0].id
    )
    assert toolbox.get_available_players(limit=1)[0]["name"] == "Breakout"
    with pytest.raises(ValueError, match="Unknown research tool"):
        research.execute("drop_player", {"player_id": players[0].id})
    with pytest.raises(NotFoundError):
        research.execute("team_roster", {"team_id": "outside-league"})
    qb = Player(
        full_name="Injured QB", position="QB", nfl_team="SEA", injury_status="Out", active=True
    )
    db.add(qb)
    db.flush()
    teammates = research.execute("nfl_team_context", {"nfl_team": "SEA"})["players"]
    assert next(p for p in teammates if p["name"] == "Injured QB")["injury_status"] == "Out"


def request_for(league, team, **kwargs):
    return LLMRequest(
        league_id=league.id,
        team_id=team.id,
        model=team.model_identifier,
        decision_type="FREE_AGENT",
        prompt_version="test",
        system_prompt="system",
        user_prompt="Review roster",
        response_model=WaiverDecision,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_native_tool_roundtrip_audits_each_hop_and_preserves_signatures(db: Session):
    league, players = seed(db)
    team = league.teams[0]
    requests = []
    signature = [{"type": "reasoning.encrypted", "data": "private-signature", "id": "sig"}]

    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["tool_choice"] == "auto"
            assert {t["function"]["name"] for t in payload["tools"]} == {
                "player_rankings",
                "player_profile",
                "team_roster",
                "nfl_team_context",
            }
            message = {
                "role": "assistant",
                "content": None,
                "reasoning_details": signature,
                "tool_calls": [
                    {
                        "id": "lookup-1",
                        "type": "function",
                        "function": {
                            "name": "player_rankings",
                            "arguments": json.dumps(
                                {
                                    "position": "WR",
                                    "pool": "free_agents",
                                    "metric": "last_3_games_average",
                                    "limit": 2,
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            assert payload["messages"][-2]["reasoning_details"] == signature
            evidence = json.loads(payload["messages"][-1]["content"])
            assert evidence["players"][0]["name"] == "Breakout"
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "add_player_id": players[1].id,
                                "drop_player_id": players[0].id,
                                "priority": 1,
                            }
                        ],
                        "public_reasoning": "Breakout averaged 19 points over two recorded games.",
                    }
                ),
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.01},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenRouterProvider("test", client=client)
        result = await LLMInvocationService(db, provider, research_rounds=3).invoke(
            request_for(league, team)
        )
    assert result.parsed.claims[0].add_player_id == players[1].id
    assert len(result.research) == 1
    runs = list(db.scalars(select(LLMRun).order_by(LLMRun.started_at)))
    assert len(runs) == 2
    assert sum(float(r.cost_usd) for r in runs) == 0.02
    public = json.dumps(public_llm_run(runs[-1]))
    assert "Breakout" in public
    assert "private-signature" not in public
    assert db.scalar(select(RosterAssignment.player_id)) == players[0].id  # Research never mutates.


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [1, 8])
async def test_research_budget_forces_final_and_reports_invalid_tools(db: Session, batch_size: int):
    league, players = seed(db)
    captured = []

    class Provider:
        async def decide(self, request):
            captured.append(request)
            if not request.allow_tool_calls:
                return LLMResult(
                    parsed=WaiverDecision(claims=[], public_reasoning="Hold"), raw_response={}
                )
            calls = [
                {
                    "id": f"call-{i}",
                    "type": "function",
                    "function": {
                        "name": "drop_player",
                        "arguments": json.dumps({"player_id": players[0].id}),
                    },
                }
                for i in range(batch_size)
            ]
            return LLMResult(
                parsed=ToolCallsDecision(tool_calls=calls),
                raw_response={
                    "choices": [
                        {"message": {"role": "assistant", "tool_calls": calls, "content": None}}
                    ]
                },
            )

    result = await LLMInvocationService(db, Provider(), research_rounds=3).invoke(
        request_for(league, league.teams[0])
    )
    assert len(captured) == (4 if batch_size == 1 else 2)
    assert captured[-1].allow_tool_calls is False
    assert len(result.research) == (3 if batch_size == 1 else 6)
    assert all("error" in entry["result"] for entry in result.research)
    assert db.scalar(select(RosterAssignment.player_id)) == players[0].id


@pytest.mark.asyncio
async def test_unsupported_tool_route_falls_back(db: Session):
    league, _ = seed(db)
    calls = []

    class Provider:
        async def decide(self, request):
            calls.append(request)
            if request.tools:
                raise LLMProviderError("No matching endpoint", status_code=404)
            return LLMResult(
                parsed=WaiverDecision(claims=[], public_reasoning="Hold"), raw_response={}
            )

    await LLMInvocationService(db, Provider(), research_rounds=3).invoke(
        request_for(league, league.teams[0])
    )
    assert len(calls) == 2 and not calls[-1].tools
    assert calls[-1].metadata["research_unavailable"] is True


def test_waiver_research_is_sealed_until_processing(db: Session):
    league, _ = seed(db)
    period = ensure_waiver_period(db, league=league, week=league.current_week)
    event = LeagueEvent(
        league_id=league.id,
        team_id=league.teams[0].id,
        event_type="WAIVER_SUBMITTED",
        visibility="PRIVATE",
        data={"period_id": period.id, "research": [{"tool": "player_rankings"}]},
    )
    db.add(event)
    run = LLMRun(
        league_id=league.id,
        team_id=league.teams[0].id,
        model="test",
        decision_type="WAIVER",
        prompt_version="test",
        request_payload={"metadata": {"context": {"waiver_period_id": period.id}}},
    )
    db.add(run)
    db.flush()
    assert not _llm_run_is_public(db, run)
    assert event.visibility == "PRIVATE"
    process_waivers(
        db,
        waiver_period_id=period.id,
        idempotency_key="test-reveal",
        processed_at=datetime.now(UTC) + timedelta(days=3),
    )
    assert event.visibility == "PUBLIC"
    assert _llm_run_is_public(db, run)


@pytest.mark.asyncio
async def test_credit_failure_does_not_bypass_provider_limits(db: Session):
    league, _ = seed(db)
    calls = 0

    class Provider:
        async def decide(self, request):
            nonlocal calls
            calls += 1
            raise LLMProviderError("Insufficient credits", status_code=402)

    with pytest.raises(LLMProviderError, match="Insufficient credits"):
        await LLMInvocationService(db, Provider(), research_rounds=3).invoke(
            request_for(league, league.teams[0])
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_manager_research_results_execute_an_atomic_free_agent_upgrade(db: Session, engine):
    from sqlalchemy.orm import sessionmaker

    from app.core.config import Settings
    from app.jobs.manager_automation import ManagerAutomation
    from app.models.entities import Transaction

    league, players = seed(db)
    league.roster_config = {"starters": {}, "bench": 1, "ir": 0}
    db.commit()
    calls = []

    class Provider:
        async def decide(self, request):
            calls.append(request)
            if not request.messages:
                assert (
                    request.metadata["context"]["droppable_players"][0]["performance"][
                        "season_points"
                    ]
                    == 6
                )
                call = {
                    "id": "rank",
                    "type": "function",
                    "function": {
                        "name": "player_rankings",
                        "arguments": json.dumps(
                            {
                                "position": "WR",
                                "pool": "free_agents",
                                "metric": "last_3_games_average",
                                "limit": 1,
                            }
                        ),
                    },
                }
                return LLMResult(
                    parsed=ToolCallsDecision(tool_calls=[call]),
                    raw_response={
                        "choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]
                    },
                )
            candidate = json.loads(request.messages[-1]["content"])["players"][0]
            return LLMResult(
                parsed=WaiverDecision(
                    claims=[
                        {
                            "add_player_id": candidate["player_id"],
                            "drop_player_id": players[0].id,
                            "priority": 1,
                        }
                    ],
                    public_reasoning="Upgrade based on recorded production.",
                ),
                raw_response={},
            )

    automation = ManagerAutomation(
        sessionmaker(engine, expire_on_commit=False), Provider(), Settings()
    )
    assert await automation._review_team_free_agents(league.id, league.teams[0].id, 4)
    db.expire_all()
    assert len(calls) == 2
    assert db.scalar(select(RosterAssignment.player_id)) == players[1].id
    assert {t.transaction_type for t in db.scalars(select(Transaction))} == {
        "DROP",
        "FREE_AGENT_ADD",
    }
    event = db.scalar(select(LeagueEvent).where(LeagueEvent.event_type == "PLAYER_ADDED"))
    assert event.data["research"][0]["tool"] == "player_rankings"
