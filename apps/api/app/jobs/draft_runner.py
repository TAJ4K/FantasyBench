from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.contracts import LLMProvider, LLMRequest
from app.agents.memory import ManagerMemoryService
from app.agents.prompts import build_prompt
from app.agents.service import LLMInvocationService
from app.core.config import Settings
from app.core.errors import ConflictError, DomainError
from app.models.entities import Draft, DraftPick, LLMRun, Player, RosterAssignment, Team
from app.models.enums import DraftPickState, DraftStatus
from app.schemas.decisions import DraftDecision
from app.services.draft import DraftService

logger = logging.getLogger(__name__)


class DraftRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.settings = settings
        self.runner_id = str(uuid.uuid4())
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, league_id: str) -> None:
        task = self._tasks.get(league_id)
        if task and not task.done():
            return
        self._tasks[league_id] = asyncio.create_task(
            self.run(league_id), name=f"draft-runner:{league_id}"
        )

    async def stop(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def resume_active(self) -> None:
        with self.session_factory() as db:
            league_ids = list(
                db.scalars(select(Draft.league_id).where(Draft.status == DraftStatus.ACTIVE.value))
            )
        for league_id in league_ids:
            self.start(league_id)

    async def run(self, league_id: str) -> None:
        logger.info("draft_runner_started", extra={"league_id": league_id})
        try:
            if not self._claim_lease(league_id):
                return
            while True:
                delay = self._prepare_iteration(league_id)
                if delay is None:
                    break
                if delay > 0:
                    await asyncio.sleep(min(delay, 60.0))
                    continue
                success = await self._decide_and_pick(league_id)
                if not success:
                    break
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("draft_runner_crashed", extra={"league_id": league_id})
            with self.session_factory() as db:
                try:
                    DraftService(db).mark_failed(
                        league_id,
                        "Draft runner crashed; administrative retry required.",
                        expected_runner_id=self.runner_id,
                    )
                    db.commit()
                except Exception:
                    db.rollback()
        finally:
            self._release_lease(league_id)
            logger.info("draft_runner_stopped", extra={"league_id": league_id})

    def _claim_lease(self, league_id: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            draft = db.scalar(select(Draft).where(Draft.league_id == league_id).with_for_update())
            if draft is None or draft.status != DraftStatus.ACTIVE.value:
                return False
            lease_expiry = draft.lease_expires_at
            if lease_expiry is not None and lease_expiry.tzinfo is None:
                lease_expiry = lease_expiry.replace(tzinfo=UTC)
            lease_active = lease_expiry is not None and lease_expiry > now
            if draft.runner_id not in (None, self.runner_id) and lease_active:
                return False
            draft.runner_id = self.runner_id
            draft.lease_expires_at = now + timedelta(
                seconds=self.settings.draft_runner_lease_seconds
            )
            db.commit()
            return True

    def _heartbeat(self, league_id: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            draft = db.scalar(select(Draft).where(Draft.league_id == league_id).with_for_update())
            if (
                draft is None
                or draft.status != DraftStatus.ACTIVE.value
                or draft.runner_id != self.runner_id
            ):
                return False
            draft.lease_expires_at = now + timedelta(
                seconds=self.settings.draft_runner_lease_seconds
            )
            db.commit()
            return True

    def _release_lease(self, league_id: str) -> None:
        with self.session_factory() as db:
            draft = db.scalar(select(Draft).where(Draft.league_id == league_id).with_for_update())
            if draft and draft.runner_id == self.runner_id:
                draft.runner_id = None
                draft.lease_expires_at = None
                db.commit()

    def _prepare_iteration(self, league_id: str) -> float | None:
        if not self._heartbeat(league_id):
            return None
        with self.session_factory() as db:
            service = DraftService(db)
            service.reveal_due(league_id)
            draft = db.scalar(select(Draft).where(Draft.league_id == league_id))
            if draft is None:
                return None
            pending_at = db.scalar(
                select(func.min(DraftPick.reveal_at)).where(
                    DraftPick.league_id == league_id,
                    DraftPick.state == DraftPickState.REVEAL_PENDING.value,
                )
            )
            db.commit()
            if pending_at:
                if pending_at.tzinfo is None:
                    pending_at = pending_at.replace(tzinfo=UTC)
                return max(0.01, (pending_at - datetime.now(UTC)).total_seconds())
            if draft.status != DraftStatus.ACTIVE.value:
                return None
            return 0.0

    async def _invoke_with_heartbeat(
        self,
        invocation: LLMInvocationService,
        request: LLMRequest,
        league_id: str,
    ) -> Any:
        task = asyncio.create_task(invocation.invoke(request))
        try:
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=self.settings.draft_runner_heartbeat_seconds
                )
                if task in done:
                    break
                if not self._heartbeat(league_id):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise ConflictError(
                        "DRAFT_LEASE_LOST",
                        "Draft lease was lost while the manager decision was in flight.",
                    )
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _decide_and_pick(self, league_id: str) -> bool:
        validation_error: str | None = None
        for attempt in range(1, self.settings.openrouter_max_retries + 1):
            if not self._heartbeat(league_id):
                return False
            with self.session_factory() as db:
                service = DraftService(db)
                turn = service.current(league_id)
                if turn is None or turn.draft.status != DraftStatus.ACTIVE.value:
                    return False
                if turn.pick.state in {
                    DraftPickState.WAITING_FOR_MANAGER.value,
                    DraftPickState.FAILED.value,
                }:
                    service.mark_thinking(league_id)
                context = build_draft_context(db, turn.team, turn.draft, validation_error)
                prompt = build_prompt("draft", context)
                request = LLMRequest(
                    league_id=league_id,
                    team_id=turn.team.id,
                    model=turn.team.model_identifier,
                    decision_type="DRAFT",
                    prompt_version=prompt.version,
                    system_prompt=prompt.system,
                    user_prompt=prompt.user,
                    response_model=DraftDecision,
                    reasoning_effort=(turn.team.reasoning_config or {}).get("effort"),
                    temperature=self.settings.openrouter_temperature,
                    max_tokens=self.settings.openrouter_max_tokens,
                    metadata={"context": context, "attempt": attempt},
                )
                db.commit()
                invocation = LLMInvocationService(
                    db,
                    self.provider,
                    daily_budget_usd=self.settings.openrouter_daily_budget_usd,
                    season_budget_usd=self.settings.openrouter_season_budget_usd,
                    max_single_request_usd=self.settings.openrouter_max_single_request_usd,
                )
                try:
                    result = await self._invoke_with_heartbeat(invocation, request, league_id)
                    decision = DraftDecision.model_validate(result.parsed)
                    llm_run_id = db.scalar(
                        select(LLMRun.id)
                        .where(LLMRun.team_id == turn.team.id, LLMRun.decision_type == "DRAFT")
                        .order_by(LLMRun.started_at.desc())
                        .limit(1)
                    )
                except DomainError as exc:
                    if exc.code == "DRAFT_LEASE_LOST":
                        return False
                    validation_error = f"Provider attempt {attempt} failed: {exc}"
                    logger.warning(
                        "draft_decision_failed",
                        extra={"league_id": league_id, "team_id": turn.team.id, "attempt": attempt},
                    )
                    continue
                except Exception as exc:
                    validation_error = f"Provider attempt {attempt} failed: {exc}"
                    logger.warning(
                        "draft_decision_failed",
                        extra={"league_id": league_id, "team_id": turn.team.id, "attempt": attempt},
                    )
                    continue
            with self.session_factory() as pick_db:
                try:
                    delay = (
                        0.0
                        if self.settings.llm_provider == "fake"
                        else random.uniform(
                            self.settings.draft_reveal_min_delay_seconds,
                            self.settings.draft_reveal_max_delay_seconds,
                        )
                    )
                    DraftService(pick_db).make_pick(
                        league_id,
                        decision.player_id,
                        public_reasoning=decision.public_reasoning,
                        confidence=decision.confidence,
                        context_snapshot=context,
                        llm_run_id=llm_run_id,
                        model=request.model,
                        reveal_delay_seconds=delay,
                        expected_pick_id=turn.pick.id,
                        expected_pick_number=turn.pick.pick_number,
                        expected_team_id=turn.team.id,
                        expected_runner_id=self.runner_id,
                    )
                    pick_db.commit()
                    try:
                        with self.session_factory() as memory_db:
                            ManagerMemoryService(memory_db).record_decision(
                                league_id,
                                request.team_id,
                                f"Drafted {decision.player_id}: {decision.public_reasoning}",
                                valued_player_ids=[decision.player_id],
                                last_llm_run_id=llm_run_id,
                            )
                    except Exception:
                        logger.exception(
                            "manager_memory_update_failed",
                            extra={"league_id": league_id, "team_id": request.team_id},
                        )
                    return True
                except DomainError as exc:
                    pick_db.rollback()
                    if exc.code in {"DRAFT_TURN_CHANGED", "DRAFT_LEASE_LOST"}:
                        return False
                    validation_error = f"The prior action was rejected: {exc.code}: {exc.message}"
        with self.session_factory() as db:
            DraftService(db).mark_failed(
                league_id,
                validation_error or "Manager failed to produce a valid selection after retries.",
                expected_runner_id=self.runner_id,
            )
            db.commit()
        return False


def build_draft_context(
    db: Session,
    team: Team,
    draft: Draft,
    validation_error: str | None = None,
) -> dict[str, Any]:
    roster_rows = db.execute(
        select(RosterAssignment, Player)
        .join(Player, Player.id == RosterAssignment.player_id)
        .where(RosterAssignment.team_id == team.id)
    ).all()
    owned = select(RosterAssignment.player_id).where(RosterAssignment.league_id == draft.league_id)
    available = list(
        db.scalars(
            select(Player).where(
                Player.active.is_(True),
                Player.id.not_in(owned),
                Player.position.in_(("QB", "RB", "WR", "TE", "DST", "K")),
            )
        )
    )
    counts: dict[str, int] = {}
    for _, player in roster_rows:
        counts[player.position] = counts.get(player.position, 0) + 1
    candidates = _legal_candidates(available, counts, draft.rounds - len(roster_rows))
    candidates.sort(
        key=lambda player: (int((player.metadata_json or {}).get("rank", 10**9)), player.id)
    )
    context: dict[str, Any] = {
        "league_id": draft.league_id,
        "team_id": team.id,
        "pick_number": draft.current_pick_number,
        "rounds": draft.rounds,
        "roster_limits": {
            "total": draft.rounds,
            "minimums": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1},
        },
        "roster": [
            {"player_id": player.id, "name": player.full_name, "position": player.position}
            for _, player in roster_rows
        ],
        "available_players": [
            {
                "player_id": player.id,
                "name": player.full_name,
                "position": player.position,
                "nfl_team": player.nfl_team,
                "injury_status": player.injury_status,
                "bye_week": player.bye_week,
                "rank": (player.metadata_json or {}).get("rank", 10**9),
                "projection": (player.metadata_json or {}).get("projection"),
            }
            for player in candidates[:80]
        ],
    }
    if validation_error:
        context["correction"] = validation_error
    return context


def _legal_candidates(
    players: list[Player], counts: dict[str, int], remaining: int
) -> list[Player]:
    minimums = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}
    maximums = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "DST": 1, "K": 1}
    missing = {
        position for position, required in minimums.items() if counts.get(position, 0) < required
    }
    total_missing = sum(
        max(0, required - counts.get(position, 0)) for position, required in minimums.items()
    )
    must_fill = remaining <= total_missing
    return [
        player
        for player in players
        if counts.get(player.position, 0) < maximums.get(player.position, 0)
        and (not must_fill or player.position in missing)
    ]
