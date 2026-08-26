from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.entities import League, Player, RosterAssignment, Team, Transaction
from app.services.guards import ensure_league_unlocked


def create_transaction(
    db: Session,
    *,
    league_id: str,
    transaction_type: str,
    idempotency_key: str,
    team_id: str | None = None,
    player_id: str | None = None,
    week: int | None = None,
    details: dict[str, Any] | None = None,
) -> Transaction:
    """Append an immutable audit record, returning the prior record on a retry."""
    existing = db.scalar(
        select(Transaction).where(
            Transaction.league_id == league_id,
            Transaction.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        expected = (transaction_type, team_id, player_id, week, details or {})
        actual = (
            existing.transaction_type,
            existing.team_id,
            existing.player_id,
            existing.week,
            existing.details,
        )
        if actual != expected:
            raise ConflictError(
                "IDEMPOTENCY_KEY_REUSED",
                "The idempotency key was already used for a different transaction.",
            )
        return existing

    transaction = Transaction(
        league_id=league_id,
        team_id=team_id,
        player_id=player_id,
        transaction_type=transaction_type,
        week=week,
        idempotency_key=idempotency_key,
        details=details or {},
    )
    db.add(transaction)
    db.flush()
    return transaction


def _roster_limit(league: League) -> int:
    config = league.roster_config or {}
    starters = sum(int(value) for value in config.get("starters", {}).values())
    return starters + int(config.get("bench", 0)) + int(config.get("ir", 0))


def _active_roster_limit(league: League) -> int:
    config = league.roster_config or {}
    return sum(int(value) for value in config.get("starters", {}).values()) + int(
        config.get("bench", 0)
    )


def add_free_agent(
    db: Session,
    *,
    league_id: str,
    team_id: str,
    add_player_id: str,
    idempotency_key: str,
    drop_player_id: str | None = None,
    week: int | None = None,
) -> tuple[RosterAssignment, list[Transaction]]:
    """Atomically add a free agent and optionally drop a rostered player.

    The caller owns the surrounding transaction and should commit it. Any raised
    exception leaves all changes pending for the caller to roll back.
    """
    prior = db.scalar(
        select(Transaction).where(
            Transaction.league_id == league_id,
            Transaction.idempotency_key == f"{idempotency_key}:add",
        )
    )
    if prior is not None:
        assignment = db.scalar(
            select(RosterAssignment).where(
                RosterAssignment.league_id == league_id,
                RosterAssignment.player_id == add_player_id,
                RosterAssignment.team_id == team_id,
            )
        )
        if assignment is None:
            raise ConflictError("INCONSISTENT_RETRY", "Prior add transaction has no roster entry.")
        existing_records = [prior]
        if drop_player_id:
            dropped = db.scalar(
                select(Transaction).where(
                    Transaction.league_id == league_id,
                    Transaction.idempotency_key == f"{idempotency_key}:drop",
                )
            )
            if dropped is not None:
                existing_records.insert(0, dropped)
        return assignment, existing_records

    league = ensure_league_unlocked(db, league_id)
    team = db.scalar(
        select(Team).where(Team.id == team_id, Team.league_id == league_id).with_for_update()
    )
    if team is None:
        raise NotFoundError("Team", team_id)
    add_player = db.get(Player, add_player_id)
    if add_player is None:
        raise NotFoundError("Player", add_player_id)
    if add_player_id == drop_player_id:
        raise ConflictError("INVALID_ADD_DROP", "The same player cannot be added and dropped.")
    owned = db.scalar(
        select(RosterAssignment)
        .where(
            RosterAssignment.league_id == league_id,
            RosterAssignment.player_id == add_player_id,
        )
        .with_for_update()
    )
    if owned is not None:
        raise ConflictError("PLAYER_NOT_FREE_AGENT", "The requested player is already rostered.")

    assignments = list(
        db.scalars(
            select(RosterAssignment).where(RosterAssignment.team_id == team_id).with_for_update()
        )
    )
    dropped_assignment = None
    if drop_player_id is not None:
        dropped_assignment = next((x for x in assignments if x.player_id == drop_player_id), None)
        if dropped_assignment is None:
            raise ConflictError("DROP_NOT_OWNED", "The drop player is not on this team's roster.")
    active_count = sum(assignment.slot_type != "IR" for assignment in assignments)
    frees_active_slot = dropped_assignment is not None and dropped_assignment.slot_type != "IR"
    if active_count - int(frees_active_slot) >= _active_roster_limit(league):
        raise ConflictError("ROSTER_FULL", "A player must be dropped before adding a free agent.")

    from app.services.rosters import RosterService

    roster_service = RosterService(db)
    if roster_service.is_player_locked(league, add_player):
        raise ConflictError("PLAYER_LOCKED", "Player cannot be added after kickoff.")
    if dropped_assignment is not None and roster_service.is_player_locked(
        league, dropped_assignment.player
    ):
        raise ConflictError("PLAYER_LOCKED", "Player cannot be dropped after kickoff.")
    replacement_slot = (
        dropped_assignment.position_slot
        if dropped_assignment is not None and dropped_assignment.slot_type == "STARTER"
        else None
    )
    if replacement_slot and not roster_service.is_eligible_for_slot(
        team, add_player, replacement_slot
    ):
        raise ConflictError(
            "STARTER_REPLACEMENT_INELIGIBLE",
            f"The added player is not eligible for vacated starter slot {replacement_slot}.",
        )

    records: list[Transaction] = []
    if dropped_assignment is not None:
        db.delete(dropped_assignment)
        db.flush()
        records.append(
            create_transaction(
                db,
                league_id=league_id,
                team_id=team_id,
                player_id=drop_player_id,
                transaction_type="DROP",
                week=week,
                idempotency_key=f"{idempotency_key}:drop",
                details={"reason": "free_agent_add", "added_player_id": add_player_id},
            )
        )

    assignment = RosterAssignment(
        league_id=league_id,
        team_id=team_id,
        player_id=add_player_id,
        slot_type="STARTER" if replacement_slot else "BENCH",
        position_slot=replacement_slot,
        acquired_via="FREE_AGENT_ADD",
    )
    db.add(assignment)
    db.flush()
    if replacement_slot and week is not None:
        roster_service.record_current_lineup(
            team_id,
            week=week,
            source="FREE_AGENT_ADD",
            public_reasoning=(
                f"Replaced {drop_player_id} with {add_player_id} in {replacement_slot}."
            ),
        )
    records.append(
        create_transaction(
            db,
            league_id=league_id,
            team_id=team_id,
            player_id=add_player_id,
            transaction_type="FREE_AGENT_ADD",
            week=week,
            idempotency_key=f"{idempotency_key}:add",
            details={"dropped_player_id": drop_player_id},
        )
    )
    return assignment, records
