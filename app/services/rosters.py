from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import ConflictError, DomainError, NotFoundError
from app.models.entities import League, LineupDecision, NflGame, Player, RosterAssignment, Team
from app.models.enums import RosterSlotType
from app.services.guards import ensure_league_unlocked


def _slot_position(slot: str) -> str:
    slot = slot.upper()
    while slot and slot[-1].isdigit():
        slot = slot[:-1]
    return slot


class RosterService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def validate_lineup(
        self,
        team_id: str,
        lineup: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> None:
        team = self._team(team_id)
        ensure_league_unlocked(self.db, team.league_id)
        config = team.league.roster_config
        required = Counter(
            {str(key).upper(): int(value) for key, value in config["starters"].items()}
        )
        supplied = Counter(_slot_position(slot) for slot in lineup)
        if supplied != required:
            raise DomainError(
                "INVALID_LINEUP_SLOTS",
                "Lineup slots do not match the league starter configuration.",
                details={"required": dict(required), "supplied": dict(supplied)},
            )
        player_ids = list(lineup.values())
        if len(player_ids) != len(set(player_ids)):
            raise ConflictError(
                "DUPLICATE_LINEUP_PLAYER", "A player may occupy only one lineup slot."
            )

        assignments = list(
            self.db.scalars(
                select(RosterAssignment)
                .options(joinedload(RosterAssignment.player))
                .where(RosterAssignment.team_id == team_id)
            )
        )
        by_player = {assignment.player_id: assignment for assignment in assignments}
        missing = [player_id for player_id in player_ids if player_id not in by_player]
        if missing:
            raise DomainError(
                "PLAYER_NOT_ON_ROSTER",
                "Every starter must belong to the team.",
                details={"player_ids": missing},
            )

        flex_eligible = {str(position).upper() for position in config.get("flex_eligible", [])}
        for slot, player_id in lineup.items():
            assignment = by_player[player_id]
            if assignment.slot_type == RosterSlotType.IR.value:
                raise DomainError(
                    "PLAYER_ON_IR",
                    f"Player {player_id} must be activated before entering the lineup.",
                )
            position = assignment.player.position.upper()
            base_slot = _slot_position(slot)
            eligible = position in flex_eligible if base_slot == "FLEX" else position == base_slot
            if not eligible:
                raise DomainError(
                    "INELIGIBLE_LINEUP_SLOT",
                    f"{position} player {player_id} is not eligible for {slot}.",
                )

        moment = now or datetime.now(UTC)
        requested_slots = {player_id: slot.upper() for slot, player_id in lineup.items()}
        for assignment in assignments:
            if not self.is_player_locked(team.league, assignment.player, now=moment):
                continue
            old_slot = (
                assignment.position_slot.upper()
                if assignment.slot_type == RosterSlotType.STARTER.value and assignment.position_slot
                else assignment.slot_type
            )
            new_slot = requested_slots.get(assignment.player_id, RosterSlotType.BENCH.value)
            if old_slot != new_slot:
                raise ConflictError(
                    "PLAYER_LOCKED",
                    f"Player {assignment.player_id} cannot be moved after kickoff.",
                )

    def set_lineup(
        self,
        team_id: str,
        lineup: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> list[RosterAssignment]:
        self.validate_lineup(team_id, lineup, now=now)
        requested = {player_id: slot.upper() for slot, player_id in lineup.items()}
        assignments = list(
            self.db.scalars(select(RosterAssignment).where(RosterAssignment.team_id == team_id))
        )
        for assignment in assignments:
            if assignment.player_id in requested:
                assignment.slot_type = RosterSlotType.STARTER.value
                assignment.position_slot = requested[assignment.player_id]
            elif assignment.slot_type != RosterSlotType.IR.value:
                assignment.slot_type = RosterSlotType.BENCH.value
                assignment.position_slot = None
        self.db.flush()
        return assignments

    def record_current_lineup(
        self,
        team_id: str,
        *,
        week: int,
        source: str,
        public_reasoning: str,
    ) -> LineupDecision:
        team = self._team(team_id)
        lineup = {
            assignment.position_slot: assignment.player_id
            for assignment in self.db.scalars(
                select(RosterAssignment).where(
                    RosterAssignment.team_id == team_id,
                    RosterAssignment.slot_type == RosterSlotType.STARTER.value,
                    RosterAssignment.position_slot.is_not(None),
                )
            )
            if assignment.position_slot
        }
        decision = LineupDecision(
            league_id=team.league_id,
            team_id=team_id,
            week=week,
            lineup=lineup,
            public_reasoning=public_reasoning,
            source=source,
        )
        self.db.add(decision)
        self.db.flush()
        return decision

    def add_player(
        self,
        team_id: str,
        player_id: str,
        *,
        acquired_via: str,
        slot_type: str = RosterSlotType.BENCH.value,
        position_slot: str | None = None,
    ) -> RosterAssignment:
        team = self._team(team_id)
        ensure_league_unlocked(self.db, team.league_id)
        player = self.db.get(Player, player_id)
        if player is None:
            raise NotFoundError("Player", player_id)
        owned = self.db.scalar(
            select(RosterAssignment).where(
                RosterAssignment.league_id == team.league_id,
                RosterAssignment.player_id == player_id,
            )
        )
        if owned is not None:
            raise ConflictError(
                "PLAYER_ALREADY_OWNED", "Player is already rostered in this league."
            )

        assignments = list(
            self.db.scalars(select(RosterAssignment).where(RosterAssignment.team_id == team_id))
        )
        config = team.league.roster_config
        capacity = sum(int(value) for value in config["starters"].values()) + int(config["bench"])
        non_ir = sum(assignment.slot_type != RosterSlotType.IR.value for assignment in assignments)
        ir_count = sum(
            assignment.slot_type == RosterSlotType.IR.value for assignment in assignments
        )
        normalized_type = slot_type.upper()
        if normalized_type == RosterSlotType.IR.value:
            if ir_count >= int(config["ir"]):
                raise ConflictError("IR_FULL", "The team's IR slots are full.")
            if not self._ir_eligible(player):
                raise DomainError("PLAYER_NOT_IR_ELIGIBLE", "Player is not eligible for IR.")
        elif non_ir >= capacity:
            raise ConflictError("ROSTER_FULL", "The team's active roster is full.")
        elif normalized_type == RosterSlotType.STARTER.value:
            if not position_slot:
                raise DomainError("POSITION_SLOT_REQUIRED", "A starter requires a position slot.")
            self.validate_lineup_addition(team, assignments, player, position_slot)
        elif normalized_type != RosterSlotType.BENCH.value:
            raise DomainError("INVALID_SLOT_TYPE", f"Unknown roster slot type {slot_type!r}.")

        assignment = RosterAssignment(
            league_id=team.league_id,
            team_id=team.id,
            player_id=player.id,
            slot_type=normalized_type,
            position_slot=position_slot.upper() if position_slot else None,
            acquired_via=acquired_via,
        )
        self.db.add(assignment)
        self.db.flush()
        return assignment

    def drop_player(
        self,
        team_id: str,
        player_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        assignment = self.db.scalar(
            select(RosterAssignment)
            .options(
                joinedload(RosterAssignment.player),
                joinedload(RosterAssignment.team).joinedload(Team.league),
            )
            .where(RosterAssignment.team_id == team_id, RosterAssignment.player_id == player_id)
        )
        if assignment is None:
            raise NotFoundError("Roster assignment", player_id)
        ensure_league_unlocked(self.db, assignment.league_id)
        if self.is_player_locked(assignment.team.league, assignment.player, now=now):
            raise ConflictError("PLAYER_LOCKED", "Player cannot be dropped after kickoff.")
        if assignment.slot_type == RosterSlotType.STARTER.value:
            raise ConflictError(
                "STARTER_REPLACEMENT_REQUIRED",
                "Move the player to the bench or use an eligible add/drop replacement first.",
            )
        self.db.delete(assignment)
        self.db.flush()

    def move_to_ir(self, team_id: str, player_id: str, *, now: datetime | None = None) -> None:
        assignment = self._assignment(team_id, player_id)
        ensure_league_unlocked(self.db, assignment.league_id)
        if self.is_player_locked(assignment.team.league, assignment.player, now=now):
            raise ConflictError("PLAYER_LOCKED", "Player cannot be moved after kickoff.")
        if assignment.slot_type == RosterSlotType.STARTER.value:
            raise ConflictError(
                "STARTER_REPLACEMENT_REQUIRED",
                "Set a complete legal lineup before moving this starter to IR.",
            )
        if not self._ir_eligible(assignment.player):
            raise DomainError("PLAYER_NOT_IR_ELIGIBLE", "Player is not eligible for IR.")
        ir_count = self.db.scalar(
            select(func.count(RosterAssignment.id)).where(
                RosterAssignment.team_id == team_id,
                RosterAssignment.slot_type == RosterSlotType.IR.value,
            )
        )
        if int(ir_count or 0) >= int(assignment.team.league.roster_config["ir"]):
            raise ConflictError("IR_FULL", "The team's IR slots are full.")
        assignment.slot_type = RosterSlotType.IR.value
        assignment.position_slot = None
        self.db.flush()

    def is_player_locked(
        self,
        league: League,
        player: Player,
        *,
        now: datetime | None = None,
    ) -> bool:
        if not player.nfl_team:
            return False
        moment = now or datetime.now(UTC)
        game = self.db.scalar(
            select(NflGame).where(
                NflGame.season == league.nfl_season,
                NflGame.week == league.current_week,
                or_(NflGame.home_team == player.nfl_team, NflGame.away_team == player.nfl_team),
            )
        )
        if game is None:
            return False
        kickoff = game.kickoff_at
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment >= kickoff

    @staticmethod
    def _ir_eligible(player: Player) -> bool:
        return (player.injury_status or player.status).upper() in {"IR", "PUP", "OUT", "NFI"}

    @staticmethod
    def is_eligible_for_slot(team: Team, player: Player, slot: str) -> bool:
        base = _slot_position(slot)
        if base == "FLEX":
            eligible = {str(value).upper() for value in team.league.roster_config["flex_eligible"]}
            return player.position.upper() in eligible
        return player.position.upper() == base

    @staticmethod
    def validate_lineup_addition(
        team: Team,
        assignments: list[RosterAssignment],
        player: Player,
        slot: str,
    ) -> None:
        base = _slot_position(slot)
        limits = {
            str(key).upper(): int(value)
            for key, value in team.league.roster_config["starters"].items()
        }
        if base not in limits:
            raise DomainError("INVALID_LINEUP_SLOT", f"Unknown starter slot {slot!r}.")
        occupied = sum(
            assignment.slot_type == RosterSlotType.STARTER.value
            and assignment.position_slot is not None
            and _slot_position(assignment.position_slot) == base
            for assignment in assignments
        )
        if occupied >= limits[base]:
            raise ConflictError("LINEUP_SLOT_FULL", f"No open {base} starter slot remains.")
        if not RosterService.is_eligible_for_slot(team, player, slot):
            raise DomainError("INELIGIBLE_LINEUP_SLOT", f"Player is not eligible for {slot}.")

    def _team(self, team_id: str) -> Team:
        team = self.db.scalar(
            select(Team).options(joinedload(Team.league)).where(Team.id == team_id)
        )
        if team is None:
            raise NotFoundError("Team", team_id)
        return team

    def _assignment(self, team_id: str, player_id: str) -> RosterAssignment:
        assignment = self.db.scalar(
            select(RosterAssignment)
            .options(
                joinedload(RosterAssignment.player),
                joinedload(RosterAssignment.team).joinedload(Team.league),
            )
            .where(RosterAssignment.team_id == team_id, RosterAssignment.player_id == player_id)
        )
        if assignment is None:
            raise NotFoundError("Roster assignment", player_id)
        return assignment
