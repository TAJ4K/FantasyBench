from __future__ import annotations

import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.fake import DeterministicFakeProvider
from app.agents.prompt_context import compact_context
from app.agents.prompts import build_prompt
from app.core.config import Settings
from app.jobs.manager_automation import ManagerAutomation
from app.jobs.progress import ManagerJobProgress
from app.jobs.scheduler import LeagueScheduler
from app.models.entities import JobRun
from app.services.initialization import initialize_league
from app.services.waivers import ensure_waiver_period


def expand(value):
    if isinstance(value, dict) and set(value) == {"columns", "rows"}:
        result = []
        for row in value["rows"]:
            record = {}
            for path, item in zip(value["columns"], row, strict=True):
                target = record
                for key in path[:-1]:
                    target = target.setdefault(key, {})
                target[path[-1]] = expand(item)
            result.append(record)
        return result
    if isinstance(value, dict):
        return {key: expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item) for item in value]
    return value


def roster(count=15):
    return [
        {
            "player_id": f"player-{i}", "name": f"Player {i}", "position": "WR",
            "nfl_team": "SEA", "active": True, "injury_status": None, "bye_week": 5,
            "slot_type": "BENCH", "position_slot": None, "locked": False,
            "rank": i + 1, "projection": None,
            "performance": {
                "season": 2026, "through_week": 3, "games_with_stats": 3,
                "season_points": i * 3, "points_per_recorded_game": i,
                "last_3_games_average": i, "current_week_points": None,
                "position_rank_by_total": count - i,
                "recent_weekly_points": [
                    {"week": week, "fantasy_points": i, "provisional": True,
                     "source": "sleeper", "updated_at": "2026-09-30T12:00:00+00:00"}
                    for week in (3, 2, 1)
                ],
            },
        }
        for i in range(count)
    ]


def test_compact_prompt_preserves_all_evidence_and_reduces_large_trade_context():
    context = {"my_roster": roster(), "other_rosters": {str(i): roster() for i in range(7)}}
    original = json.dumps(context, sort_keys=True, separators=(",", ":"))
    prompt = build_prompt("trade", context)
    compact = prompt.user.split("Context JSON:\n", 1)[1]
    assert expand(json.loads(compact)) == context
    # Include the format instructions in the savings comparison.
    assert len(compact) + 600 < len(original) * 0.65
    assert context["my_roster"][0]["performance"]["season_points"] == 0
    assert context["my_roster"][0]["performance"]["current_week_points"] is None


@pytest.mark.parametrize("value", [
    [], [0, False, None, ""], [{"a": 1}, {"b": 2}, {}, {"a": None}],
    [{"a": {"b": i}, "c": [], "d": {}} for i in range(5)],
    [{"a": {"b": 1}}, {"a": {}}, {"a": None}, {"a": {"c": 2}}],
])
def test_compact_context_preserves_heterogeneous_and_empty_values(value):
    assert expand(compact_context(value)) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,method", [
    ("lineup", "_set_team_lineup"), ("free_agent", "_review_team_free_agents"),
    ("waiver", "_collect_team_waivers"), ("trade", "_consider_trade_proposal"),
])
async def test_scheduler_retry_only_repeats_failed_manager(engine, db, monkeypatch, kind, method):
    factory = sessionmaker(engine, expire_on_commit=False)
    league = initialize_league(db, nfl_season=2026)
    league.current_week = 1
    period = ensure_waiver_period(db, league=league, week=1)
    job = JobRun(job_name=kind, idempotency_key="retry", status="RUNNING", attempt_count=1)
    db.add(job)
    db.commit()
    failed_team = league.teams[0].id
    calls = Counter()

    async def perform(target_id, team_id, *args, **kwargs):
        calls[team_id] += 1
        if team_id == failed_team and calls[team_id] == 1:
            raise RuntimeError("temporary provider failure")
        return False

    automation = ManagerAutomation(factory, DeterministicFakeProvider(), Settings())
    monkeypatch.setattr(automation, method, perform)
    scheduler = LeagueScheduler(factory, SimpleNamespace(), automation, 5)
    target = period.id if kind == "waiver" else league.id
    week = 1 if kind in {"lineup", "free_agent"} else None
    await scheduler._execute_manager_job(kind, job.id, target, week, 1)
    db.refresh(job)
    assert job.status == "FAILED"
    assert len(calls) == 8
    job.status = "RUNNING"
    job.attempt_count = 2
    db.commit()
    await scheduler._execute_manager_job(kind, job.id, target, week, 2)
    db.refresh(job)
    assert job.status == "COMPLETE"
    assert calls[failed_team] == 2
    assert sum(calls.values()) == 9  # Eight initial reviews, only one retry.


@pytest.mark.asyncio
async def test_shutdown_retains_completed_actions_for_restart(engine, db, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    league = initialize_league(db, nfl_season=2026)
    job = JobRun(job_name="lineup", idempotency_key="restart", status="RUNNING", attempt_count=1)
    db.add(job)
    db.commit()
    automation = ManagerAutomation(factory, DeterministicFakeProvider(), Settings())
    calls = []

    async def interrupted(league_id, team_id, *args, **kwargs):
        calls.append(team_id)
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(automation, "_set_team_lineup", interrupted)
    scheduler = LeagueScheduler(factory, SimpleNamespace(), automation, 5)
    with pytest.raises(asyncio.CancelledError):
        await scheduler._execute_manager_job("lineup", job.id, league.id, 1, 1)
    db.refresh(job)
    assert job.status == "FAILED"
    assert job.details[calls[0]] == "COMPLETE"
    assert calls[1] not in job.details
    job.status = "RUNNING"
    db.commit()
    old_progress = ManagerJobProgress(factory, job.id, 1)
    job.attempt_count = 2
    db.commit()
    with pytest.raises(RuntimeError, match="lease lost"):
        old_progress.record(calls[1], "COMPLETE")
    with pytest.raises(RuntimeError, match="lease lost"):
        ManagerJobProgress(factory, job.id, 1)
    resumed = AsyncMock()
    monkeypatch.setattr(automation, "_set_team_lineup", resumed)
    await scheduler._execute_manager_job("lineup", job.id, league.id, 1, 2)
    assert resumed.await_count == 7
    assert calls[0] not in [call.args[1] for call in resumed.await_args_list]


@pytest.mark.asyncio
async def test_trade_retry_skips_expired_offer_and_completed_proposals(engine, db, monkeypatch):
    factory = sessionmaker(engine, expire_on_commit=False)
    league = initialize_league(db, nfl_season=2026)
    job = JobRun(
        job_name="trade", idempotency_key="expired", status="RUNNING", attempt_count=2,
        details={
            "offer:expired-offer": "FAILED: temporarily unavailable",
            **{f"team:{team.id}": "PASS" for team in league.teams},
        },
    )
    db.add(job)
    db.commit()
    provider = AsyncMock()
    automation = ManagerAutomation(factory, provider, Settings())
    scheduler = LeagueScheduler(factory, SimpleNamespace(), automation, 5)
    await scheduler._execute_manager_job("trade", job.id, league.id, None, 2)
    db.refresh(job)
    assert job.status == "COMPLETE"
    assert job.details["offer:expired-offer"] == "SKIPPED: offer no longer pending"
    provider.decide.assert_not_awaited()
