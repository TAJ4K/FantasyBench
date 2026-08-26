from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, NotFoundError
from app.models.base import utcnow
from app.models.entities import (
    Draft,
    DraftPick,
    FantasyWeek,
    League,
    Player,
    RosterAssignment,
    Team,
    Transaction,
)
from app.models.enums import DraftPickState, DraftStatus, LeagueStatus, TransactionType
from app.services.events import emit_event
from app.services.guards import ensure_league_unlocked
from app.services.rosters import RosterService
from app.services.waivers import ensure_waiver_period


def pick_coordinates(pick_number: int, team_count: int) -> tuple[int, int]:
    if pick_number < 1 or team_count < 2:
        raise ValueError("pick_number must be positive and team_count must be at least two")
    return (pick_number - 1) // team_count + 1, (pick_number - 1) % team_count + 1


def team_for_pick(order: list[str], pick_number: int) -> str:
    if not order:
        raise ValueError("Draft order cannot be empty.")
    round_number, round_pick = pick_coordinates(pick_number, len(order))
    index = round_pick - 1 if round_number % 2 else len(order) - round_pick
    return order[index]


def randomized_order(team_ids: list[str], seed: int) -> list[str]:
    order = list(team_ids)
    Random(seed).shuffle(order)
    return order


@dataclass(frozen=True)
class DraftTurn:
    draft: Draft
    team: Team
    pick: DraftPick


class DraftService:
    """Synchronous, recovery-friendly draft state machine.

    Methods flush but do not commit. A worker should call ``make_pick`` once per
    transaction, making a process crash safe to retry.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def start(self, league_id: str) -> DraftTurn:
        ensure_league_unlocked(self.db, league_id)
        draft = self._locked_draft(league_id)
        if draft.status != DraftStatus.NOT_STARTED.value:
            raise ConflictError("DRAFT_ALREADY_STARTED", "Draft is not in NOT_STARTED state.")
        league = self._league(league_id)
        team_ids = set(self.db.scalars(select(Team.id).where(Team.league_id == league_id)))
        expected = int(league.settings.get("teams", 0))
        valid_order = len(draft.order) == expected and set(draft.order) == team_ids
        if len(team_ids) != expected or not valid_order:
            raise DomainError(
                "LEAGUE_NOT_CONFIGURED",
                "Draft order must contain every configured team.",
            )
        if not self.db.scalar(select(func.count(Player.id))):
            raise DomainError(
                "PLAYER_DATA_REQUIRED", "Player data must exist before starting the draft."
            )
        if draft.rounds <= 0:
            raise DomainError("DRAFT_NOT_CONFIGURED", "Draft must have at least one round.")

        draft.status = DraftStatus.ACTIVE.value
        draft.runner_id = None
        draft.lease_expires_at = None
        draft.started_at = utcnow()
        draft.paused_at = None
        league.status = LeagueStatus.DRAFTING.value
        turn = self._ensure_current_pick(draft)
        emit_event(
            self.db,
            league_id,
            "DRAFT_STARTED",
            aggregate_type="DRAFT",
            aggregate_id=draft.id,
            data={"rounds": draft.rounds, "order": draft.order},
        )
        self._emit_on_clock(turn)
        self.db.flush()
        return turn

    def pause(self, league_id: str) -> Draft:
        draft = self._locked_draft(league_id)
        if draft.status != DraftStatus.ACTIVE.value:
            raise ConflictError("DRAFT_NOT_ACTIVE", "Only an active draft can be paused.")
        draft.status = DraftStatus.PAUSED.value
        draft.paused_at = utcnow()
        draft.runner_id = None
        draft.lease_expires_at = None
        emit_event(
            self.db,
            league_id,
            "DRAFT_PAUSED",
            aggregate_type="DRAFT",
            aggregate_id=draft.id,
        )
        self.db.flush()
        return draft

    def resume(self, league_id: str) -> DraftTurn:
        ensure_league_unlocked(self.db, league_id)
        draft = self._locked_draft(league_id)
        if draft.status != DraftStatus.PAUSED.value:
            raise ConflictError("DRAFT_NOT_PAUSED", "Only a paused draft can be resumed.")
        draft.status = DraftStatus.ACTIVE.value
        draft.paused_at = None
        draft.runner_id = None
        draft.lease_expires_at = None
        turn = self._ensure_current_pick(draft)
        emit_event(
            self.db,
            league_id,
            "DRAFT_RESUMED",
            aggregate_type="DRAFT",
            aggregate_id=draft.id,
        )
        self._emit_on_clock(turn)
        self.db.flush()
        return turn

    def current(self, league_id: str) -> DraftTurn | None:
        draft = self._draft(league_id)
        if draft.status in {
            DraftStatus.NOT_STARTED.value,
            DraftStatus.COMPLETED.value,
        }:
            return None
        return self._ensure_current_pick(draft)

    def mark_thinking(self, league_id: str) -> DraftPick:
        draft = self._locked_draft(league_id)
        self._require_active(draft)
        turn = self._ensure_current_pick(draft)
        if turn.pick.state not in {
            DraftPickState.WAITING_FOR_MANAGER.value,
            DraftPickState.FAILED.value,
        }:
            raise ConflictError("INVALID_PICK_PHASE", "Current pick cannot enter THINKING.")
        turn.pick.state = DraftPickState.THINKING.value
        turn.pick.error = None
        emit_event(
            self.db,
            league_id,
            "LLM_THINKING",
            aggregate_type="DRAFT_PICK",
            aggregate_id=turn.pick.id,
            team_id=turn.team.id,
            data={"pick_number": turn.pick.pick_number},
        )
        self.db.flush()
        return turn.pick

    def mark_failed(
        self,
        league_id: str,
        error: str,
        *,
        expected_runner_id: str | None = None,
    ) -> DraftPick:
        draft = self._locked_draft(league_id)
        if expected_runner_id is not None and draft.runner_id != expected_runner_id:
            raise ConflictError(
                "DRAFT_LEASE_LOST",
                "This runner no longer owns the draft lease.",
            )
        self._require_active(draft)
        pick = self._ensure_current_pick(draft).pick
        if pick.player_id is not None:
            raise ConflictError("PICK_ALREADY_LOCKED", "A selected pick cannot be marked failed.")
        pick.state = DraftPickState.FAILED.value
        pick.error = error
        draft.status = DraftStatus.PAUSED.value
        draft.paused_at = utcnow()
        draft.failure = error
        draft.runner_id = None
        draft.lease_expires_at = None
        self.db.flush()
        return pick

    def make_pick(
        self,
        league_id: str,
        player_id: str,
        *,
        public_reasoning: str | None = None,
        confidence: float | None = None,
        context_snapshot: dict[str, object] | None = None,
        llm_run_id: str | None = None,
        model: str | None = None,
        reveal_delay_seconds: float = 0,
        source: str = "MANAGER",
        expected_pick_id: str | None = None,
        expected_pick_number: int | None = None,
        expected_team_id: str | None = None,
        expected_runner_id: str | None = None,
    ) -> DraftPick:
        ensure_league_unlocked(self.db, league_id)
        draft = self._locked_draft(league_id)
        self._require_active(draft)
        turn = self._ensure_current_pick(draft)
        pick = turn.pick
        if (
            (expected_pick_id is not None and pick.id != expected_pick_id)
            or (expected_pick_number is not None and pick.pick_number != expected_pick_number)
            or (expected_team_id is not None and turn.team.id != expected_team_id)
            or (expected_runner_id is not None and draft.runner_id != expected_runner_id)
        ):
            raise ConflictError(
                "DRAFT_TURN_CHANGED",
                "The draft advanced while this manager decision was in flight.",
            )
        if pick.player_id is not None or pick.state not in {
            DraftPickState.WAITING_FOR_MANAGER.value,
            DraftPickState.THINKING.value,
            DraftPickState.FAILED.value,
        }:
            raise ConflictError(
                "PICK_ALREADY_MADE", "The current draft pick has already been made."
            )
        player = self.db.get(Player, player_id)
        if player is None:
            raise NotFoundError("Player", player_id)
        if self.db.scalar(
            select(DraftPick.id).where(
                DraftPick.draft_id == draft.id,
                DraftPick.player_id == player_id,
                DraftPick.state != DraftPickState.UNDONE.value,
            )
        ):
            raise ConflictError("PLAYER_ALREADY_DRAFTED", "Player has already been drafted.")

        # Roster ownership has a league-wide unique constraint as the final race guard.
        RosterService(self.db).add_player(
            turn.team.id,
            player_id,
            acquired_via=TransactionType.DRAFT.value,
        )
        completed_at = utcnow()
        pick.player_id = player_id
        pick.model = model or turn.team.model_identifier
        pick.public_reasoning = public_reasoning
        pick.confidence = confidence
        pick.context_snapshot = dict(context_snapshot or {})
        pick.llm_run_id = llm_run_id
        pick.decision_completed_at = completed_at
        pick.reveal_at = completed_at + timedelta(seconds=max(0, reveal_delay_seconds))
        pick.state = DraftPickState.REVEAL_PENDING.value
        pick.error = None
        self.db.add(
            Transaction(
                league_id=league_id,
                team_id=turn.team.id,
                player_id=player_id,
                transaction_type=TransactionType.DRAFT.value,
                week=0,
                idempotency_key=f"draft:{pick.id}:{completed_at.isoformat()}",
                details={"pick_number": pick.pick_number, "round": pick.round, "source": source},
            )
        )
        emit_event(
            self.db,
            league_id,
            "DRAFT_PICK_LOCKED",
            aggregate_type="DRAFT_PICK",
            aggregate_id=pick.id,
            team_id=turn.team.id,
            data={"pick_number": pick.pick_number, "source": source},
        )

        total_picks = len(draft.order) * draft.rounds
        if pick.pick_number >= total_picks:
            draft.status = DraftStatus.COMPLETED.value
            draft.completed_at = completed_at
            league = self._league(league_id)
            league.status = LeagueStatus.REGULAR_SEASON.value
            league.current_week = 1
            fantasy_week = self.db.scalar(
                select(FantasyWeek).where(
                    FantasyWeek.league_id == league_id,
                    FantasyWeek.week == 1,
                )
            )
            if fantasy_week is not None:
                fantasy_week.status = "ACTIVE"
            ensure_waiver_period(self.db, league=league, week=1)
            emit_event(
                self.db,
                league_id,
                "DRAFT_COMPLETED",
                aggregate_type="DRAFT",
                aggregate_id=draft.id,
            )
            emit_event(self.db, league_id, "WEEK_STARTED", data={"week": 1})
        else:
            draft.current_pick_number = pick.pick_number + 1
            next_turn = self._ensure_current_pick(draft)
            self._emit_on_clock(next_turn)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "PLAYER_ALREADY_DRAFTED", "Player was selected concurrently by another operation."
            ) from exc
        return pick

    def admin_pick(self, league_id: str, player_id: str, **kwargs: object) -> DraftPick:
        kwargs["source"] = "ADMIN"
        return self.make_pick(league_id, player_id, **kwargs)  # type: ignore[arg-type]

    def reveal_pick(
        self,
        pick_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> DraftPick:
        pick = self.db.scalar(select(DraftPick).where(DraftPick.id == pick_id).with_for_update())
        if pick is None:
            raise NotFoundError("Draft pick", pick_id)
        if pick.state == DraftPickState.REVEALED.value:
            return pick
        if pick.state != DraftPickState.REVEAL_PENDING.value:
            raise ConflictError("INVALID_PICK_PHASE", "Pick is not pending reveal.")
        moment = now or datetime.now(UTC)
        reveal_at = pick.reveal_at
        if reveal_at and reveal_at.tzinfo is None:
            reveal_at = reveal_at.replace(tzinfo=UTC)
        if not force and reveal_at and moment < reveal_at:
            raise ConflictError("REVEAL_NOT_DUE", "The configured reveal delay has not elapsed.")
        pick.state = DraftPickState.REVEALED.value
        pick.revealed_at = moment
        emit_event(
            self.db,
            pick.league_id,
            "DRAFT_PICK_REVEALED",
            aggregate_type="DRAFT_PICK",
            aggregate_id=pick.id,
            team_id=pick.team_id,
            data={"pick_number": pick.pick_number, "player_id": pick.player_id},
            commentary=pick.public_reasoning,
        )
        self.db.flush()
        return pick

    def reveal_due(self, league_id: str, *, now: datetime | None = None) -> list[DraftPick]:
        moment = now or datetime.now(UTC)
        picks = list(
            self.db.scalars(
                select(DraftPick).where(
                    DraftPick.league_id == league_id,
                    DraftPick.state == DraftPickState.REVEAL_PENDING.value,
                    DraftPick.reveal_at <= moment,
                )
            )
        )
        for pick in picks:
            self.reveal_pick(pick.id, now=moment)
        return picks

    def undo_last_pick(self, league_id: str) -> DraftPick:
        ensure_league_unlocked(self.db, league_id)
        draft = self._locked_draft(league_id)
        pick = self.db.scalar(
            select(DraftPick)
            .where(DraftPick.draft_id == draft.id, DraftPick.player_id.is_not(None))
            .order_by(DraftPick.pick_number.desc())
            .with_for_update()
        )
        if pick is None or pick.player_id is None:
            raise ConflictError("NO_PICK_TO_UNDO", "There is no draft pick to undo.")
        player_id = pick.player_id
        assignment = self.db.scalar(
            select(RosterAssignment).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.team_id == pick.team_id,
                RosterAssignment.player_id == player_id,
                RosterAssignment.acquired_via == TransactionType.DRAFT.value,
            )
        )
        if assignment is None:
            raise ConflictError(
                "DRAFT_ROSTER_MISMATCH", "Draft pick has no matching roster assignment."
            )
        self.db.delete(assignment)
        self.db.add(
            Transaction(
                league_id=league_id,
                team_id=pick.team_id,
                player_id=player_id,
                transaction_type=TransactionType.COMMISSIONER_DROP.value,
                week=0,
                idempotency_key=f"draft-undo:{pick.id}:{utcnow().isoformat()}",
                details={"pick_number": pick.pick_number, "reason": "DRAFT_UNDO"},
            )
        )
        emit_event(
            self.db,
            league_id,
            "DRAFT_PICK_UNDONE",
            aggregate_type="DRAFT_PICK",
            aggregate_id=pick.id,
            team_id=pick.team_id,
            data={"pick_number": pick.pick_number, "player_id": player_id},
        )
        pick.player_id = None
        pick.state = DraftPickState.WAITING_FOR_MANAGER.value
        pick.model = None
        pick.public_reasoning = None
        pick.confidence = None
        pick.context_snapshot = {}
        pick.llm_run_id = None
        pick.decision_completed_at = None
        pick.reveal_at = None
        pick.revealed_at = None
        pick.error = None
        draft.current_pick_number = pick.pick_number
        draft.completed_at = None
        draft.failure = None
        draft.status = DraftStatus.ACTIVE.value
        self._league(league_id).status = LeagueStatus.DRAFTING.value
        self._emit_on_clock(DraftTurn(draft=draft, team=self._team(pick.team_id), pick=pick))
        self.db.flush()
        return pick

    def set_order(
        self,
        league_id: str,
        order: list[str],
        *,
        random_seed: int | None = None,
    ) -> Draft:
        ensure_league_unlocked(self.db, league_id)
        draft = self._locked_draft(league_id)
        if draft.status != DraftStatus.NOT_STARTED.value:
            raise ConflictError("DRAFT_ALREADY_STARTED", "Draft order is locked after draft start.")
        team_ids = set(self.db.scalars(select(Team.id).where(Team.league_id == league_id)))
        if len(order) != len(team_ids) or set(order) != team_ids:
            raise DomainError(
                "INVALID_DRAFT_ORDER", "Order must contain each league team exactly once."
            )
        draft.order = list(order)
        draft.random_seed = random_seed
        self.db.flush()
        return draft

    def _ensure_current_pick(self, draft: Draft) -> DraftTurn:
        total = len(draft.order) * draft.rounds
        if draft.current_pick_number > total:
            raise ConflictError("DRAFT_COMPLETE", "Draft has no current pick.")
        pick = self.db.scalar(
            select(DraftPick).where(
                DraftPick.draft_id == draft.id,
                DraftPick.pick_number == draft.current_pick_number,
            )
        )
        team_id = team_for_pick(draft.order, draft.current_pick_number)
        round_number, round_pick = pick_coordinates(draft.current_pick_number, len(draft.order))
        if pick is None:
            pick = DraftPick(
                draft_id=draft.id,
                league_id=draft.league_id,
                team_id=team_id,
                pick_number=draft.current_pick_number,
                round=round_number,
                round_pick=round_pick,
                state=DraftPickState.WAITING_FOR_MANAGER.value,
            )
            self.db.add(pick)
            self.db.flush()
        elif pick.team_id != team_id:
            raise ConflictError(
                "DRAFT_STATE_CORRUPT", "Persisted current pick does not match draft order."
            )
        return DraftTurn(draft=draft, team=self._team(team_id), pick=pick)

    def _emit_on_clock(self, turn: DraftTurn) -> None:
        emit_event(
            self.db,
            turn.draft.league_id,
            "ON_THE_CLOCK",
            aggregate_type="DRAFT_PICK",
            aggregate_id=turn.pick.id,
            team_id=turn.team.id,
            data={
                "pick_number": turn.pick.pick_number,
                "round": turn.pick.round,
                "round_pick": turn.pick.round_pick,
            },
        )

    @staticmethod
    def _require_active(draft: Draft) -> None:
        if draft.status != DraftStatus.ACTIVE.value:
            raise ConflictError("DRAFT_NOT_ACTIVE", "Draft must be active to make a pick.")

    def _draft(self, league_id: str) -> Draft:
        draft = self.db.scalar(select(Draft).where(Draft.league_id == league_id))
        if draft is None:
            raise NotFoundError("Draft for league", league_id)
        return draft

    def _locked_draft(self, league_id: str) -> Draft:
        draft = self.db.scalar(select(Draft).where(Draft.league_id == league_id).with_for_update())
        if draft is None:
            raise NotFoundError("Draft for league", league_id)
        return draft

    def _league(self, league_id: str) -> League:
        league = self.db.get(League, league_id)
        if league is None:
            raise NotFoundError("League", league_id)
        return league

    def _team(self, team_id: str) -> Team:
        team = self.db.get(Team, team_id)
        if team is None:
            raise NotFoundError("Team", team_id)
        return team
