from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.base import utcnow
from app.models.entities import (
    League,
    Player,
    RosterAssignment,
    Team,
    WaiverClaim,
    WaiverPeriod,
)
from app.services.guards import ensure_league_unlocked
from app.services.transactions import _active_roster_limit, create_transaction


def ensure_waiver_period(db: Session, *, league: League, week: int) -> WaiverPeriod:
    existing = db.scalar(
        select(WaiverPeriod).where(
            WaiverPeriod.league_id == league.id,
            WaiverPeriod.season == league.nfl_season,
            WaiverPeriod.week == week,
        )
    )
    if existing is not None:
        return existing
    hours = float(league.settings.get("waiver_period_hours", 24))
    period = WaiverPeriod(
        league_id=league.id,
        season=league.nfl_season,
        week=week,
        status="OPEN",
        deadline_at=utcnow() + timedelta(hours=hours),
    )
    period.processing_at = period.deadline_at + timedelta(
        minutes=float(league.settings.get("waiver_processing_grace_minutes", 30))
    )
    db.add(period)
    db.flush()
    return period


def submit_claims(
    db: Session,
    *,
    waiver_period_id: str,
    team_id: str,
    claims: Iterable[Any],
    public_reasoning: str | None = None,
    collection_started_at: datetime | None = None,
) -> list[WaiverClaim]:
    period = db.scalar(select(WaiverPeriod).where(WaiverPeriod.id == waiver_period_id))
    if period is None:
        raise NotFoundError("WaiverPeriod", waiver_period_id)
    ensure_league_unlocked(db, period.league_id)
    if period.status != "OPEN":
        raise ConflictError("WAIVERS_CLOSED", "This waiver period is not open.")
    now = utcnow()
    deadline = period.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=now.tzinfo)
    if deadline <= now:
        processing_at = period.processing_at or deadline
        if processing_at.tzinfo is None:
            processing_at = processing_at.replace(tzinfo=now.tzinfo)
        started = collection_started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo)
        manager_grace = started is not None and started < deadline and now < processing_at
        if not manager_grace:
            raise ConflictError("WAIVER_DEADLINE_PASSED", "The waiver deadline has passed.")
    team = db.scalar(select(Team).where(Team.id == team_id, Team.league_id == period.league_id))
    if team is None:
        raise NotFoundError("Team", team_id)

    normalized: list[tuple[str, str | None, int, int]] = []
    for item in claims:
        payload = item if isinstance(item, dict) else item.model_dump()
        add_id = str(payload.get("add_player_id"))
        drop_id = payload.get("drop_player_id")
        bid = int(payload.get("faab", payload.get("bid", 0)) or 0)
        priority = int(payload.get("priority", 0) or 0)
        if bid < 0 or bid > team.faab_budget:
            raise ConflictError("INVALID_FAAB_BID", "A FAAB bid must fit within the team's budget.")
        if priority < 1 or add_id == drop_id:
            raise ConflictError(
                "INVALID_WAIVER_CLAIM", "Claim priority or player pairing is invalid."
            )
        normalized.append((add_id, drop_id, bid, priority))
    priorities = [row[3] for row in normalized]
    if len(priorities) != len(set(priorities)):
        raise ConflictError("DUPLICATE_CLAIM_PRIORITY", "Claim priorities must be unique.")

    existing = list(
        db.scalars(
            select(WaiverClaim).where(
                WaiverClaim.waiver_period_id == waiver_period_id,
                WaiverClaim.team_id == team_id,
                WaiverClaim.status == "PENDING",
            )
        )
    )
    for claim in existing:
        db.delete(claim)
    db.flush()

    created = [
        WaiverClaim(
            waiver_period_id=period.id,
            league_id=period.league_id,
            team_id=team_id,
            add_player_id=add_id,
            drop_player_id=drop_id,
            bid=bid,
            priority=priority,
            public_reasoning=public_reasoning,
        )
        for add_id, drop_id, bid, priority in normalized
    ]
    db.add_all(created)
    db.flush()
    return sorted(created, key=lambda claim: claim.priority)


def _invalidate(claim: WaiverClaim, reason: str, processed_at: datetime) -> None:
    claim.status = "INVALID"
    claim.failure_reason = reason
    claim.processed_at = processed_at


def process_waivers(
    db: Session,
    *,
    waiver_period_id: str,
    idempotency_key: str,
    processed_at: datetime | None = None,
) -> list[WaiverClaim]:
    """Resolve ordered conditional FAAB claims in deterministic waves."""
    now = processed_at or utcnow()
    snapshot = db.get(WaiverPeriod, waiver_period_id)
    if snapshot is None:
        raise NotFoundError("WaiverPeriod", waiver_period_id)
    ensure_league_unlocked(db, snapshot.league_id)
    period = db.scalar(
        select(WaiverPeriod).where(WaiverPeriod.id == waiver_period_id).with_for_update()
    )
    if period is None:
        raise NotFoundError("WaiverPeriod", waiver_period_id)
    if period.status == "PROCESSED":
        if period.idempotency_key != idempotency_key:
            raise ConflictError("WAIVERS_ALREADY_PROCESSED", "This period was already processed.")
        return list(
            db.scalars(
                select(WaiverClaim)
                .where(WaiverClaim.waiver_period_id == period.id)
                .order_by(WaiverClaim.team_id, WaiverClaim.priority)
            )
        )
    if period.status != "OPEN":
        raise ConflictError("WAIVERS_BUSY", "This waiver period cannot be processed now.")
    period.status = "PROCESSING"
    period.idempotency_key = idempotency_key
    db.flush()

    league = db.get(League, period.league_id)
    if league is None:
        raise NotFoundError("League", period.league_id)
    teams = {
        team.id: team
        for team in db.scalars(
            select(Team).where(Team.league_id == period.league_id).with_for_update()
        )
    }
    assignments = list(
        db.scalars(
            select(RosterAssignment)
            .where(RosterAssignment.league_id == period.league_id)
            .with_for_update()
        )
    )
    roster_by_player = {assignment.player_id: assignment for assignment in assignments}
    rosters: dict[str, dict[str, RosterAssignment]] = defaultdict(dict)
    for assignment in assignments:
        rosters[assignment.team_id][assignment.player_id] = assignment
    from app.services.rosters import RosterService

    roster_service = RosterService(db)
    claims = list(
        db.scalars(
            select(WaiverClaim)
            .where(
                WaiverClaim.waiver_period_id == period.id,
                WaiverClaim.status == "PENDING",
            )
            .order_by(WaiverClaim.priority, WaiverClaim.created_at, WaiverClaim.id)
            .with_for_update()
        )
    )
    pending: dict[str, list[WaiverClaim]] = defaultdict(list)
    for claim in claims:
        pending[claim.team_id].append(claim)

    winners_in_order: list[str] = []
    while any(pending.values()):
        heads: list[WaiverClaim] = []
        for team_id, queue in pending.items():
            while queue:
                claim = queue[0]
                team = teams.get(team_id)
                if team is None:
                    _invalidate(claim, "team not found", now)
                elif claim.bid > team.faab_budget:
                    _invalidate(claim, "insufficient FAAB", now)
                elif claim.add_player_id in roster_by_player:
                    claim.status = "LOST"
                    claim.failure_reason = "player unavailable"
                    claim.processed_at = now
                elif claim.drop_player_id and claim.drop_player_id not in rosters[team_id]:
                    _invalidate(claim, "drop player not owned", now)
                elif (
                    not claim.drop_player_id
                    and league is not None
                    and sum(item.slot_type != "IR" for item in rosters[team_id].values())
                    >= _active_roster_limit(league)
                ):
                    _invalidate(claim, "roster full", now)
                else:
                    add_player = db.get(Player, claim.add_player_id)
                    drop_assignment = (
                        rosters[team_id].get(claim.drop_player_id) if claim.drop_player_id else None
                    )
                    if add_player is None:
                        _invalidate(claim, "add player not found", now)
                    elif roster_service.is_player_locked(league, add_player, now=now):
                        _invalidate(claim, "add player locked", now)
                    elif drop_assignment is not None and roster_service.is_player_locked(
                        league, drop_assignment.player, now=now
                    ):
                        _invalidate(claim, "drop player locked", now)
                    elif (
                        drop_assignment is not None
                        and drop_assignment.slot_type == "STARTER"
                        and drop_assignment.position_slot is not None
                        and not roster_service.is_eligible_for_slot(
                            team, add_player, drop_assignment.position_slot
                        )
                    ):
                        _invalidate(claim, "replacement is ineligible for starter slot", now)
                    else:
                        heads.append(claim)
                        break
                queue.pop(0)
        if not heads:
            break

        by_player: dict[str, list[WaiverClaim]] = defaultdict(list)
        for claim in heads:
            by_player[claim.add_player_id].append(claim)
        for player_id in sorted(by_player):
            contenders = by_player[player_id]
            contenders.sort(
                key=lambda claim: (
                    -claim.bid,
                    teams[claim.team_id].waiver_priority,
                    claim.created_at,
                    claim.id,
                )
            )
            winner = contenders[0]
            team = teams[winner.team_id]
            dropped = None
            replacement_slot = None
            if winner.drop_player_id:
                dropped = rosters[winner.team_id].pop(winner.drop_player_id)
                if dropped.slot_type == "STARTER":
                    replacement_slot = dropped.position_slot
                roster_by_player.pop(winner.drop_player_id, None)
                db.delete(dropped)
                db.flush()
                create_transaction(
                    db,
                    league_id=period.league_id,
                    team_id=winner.team_id,
                    player_id=winner.drop_player_id,
                    transaction_type="WAIVER_DROP",
                    week=period.week,
                    idempotency_key=f"waiver:{period.id}:{winner.id}:drop",
                    details={"claim_id": winner.id, "bid": winner.bid},
                )
            added = RosterAssignment(
                league_id=period.league_id,
                team_id=winner.team_id,
                player_id=winner.add_player_id,
                slot_type="STARTER" if replacement_slot else "BENCH",
                position_slot=replacement_slot,
                acquired_via="WAIVER_ADD",
            )
            db.add(added)
            db.flush()
            if replacement_slot:
                roster_service.record_current_lineup(
                    winner.team_id,
                    week=period.week,
                    source="WAIVER_ADD",
                    public_reasoning=(
                        f"Replaced {winner.drop_player_id} with "
                        f"{winner.add_player_id} in {replacement_slot}."
                    ),
                )
            roster_by_player[winner.add_player_id] = added
            rosters[winner.team_id][winner.add_player_id] = added
            team.faab_budget -= winner.bid
            winner.status = "WON"
            winner.processed_at = now
            winners_in_order.append(winner.team_id)
            create_transaction(
                db,
                league_id=period.league_id,
                team_id=winner.team_id,
                player_id=winner.add_player_id,
                transaction_type="WAIVER_ADD",
                week=period.week,
                idempotency_key=f"waiver:{period.id}:{winner.id}:add",
                details={
                    "claim_id": winner.id,
                    "bid": winner.bid,
                    "dropped_player_id": winner.drop_player_id,
                },
            )
            pending[winner.team_id].pop(0)
            for loser in contenders[1:]:
                loser.status = "LOST"
                loser.failure_reason = "outbid"
                loser.processed_at = now
                pending[loser.team_id].pop(0)

    for queue in pending.values():
        for claim in queue:
            if claim.status == "PENDING":
                claim.status = "LOST"
                claim.failure_reason = "conditional claim no longer executable"
                claim.processed_at = now

    # Rolling priority: each successful team moves behind teams that did not win,
    # preserving prior priority order and first-win order.
    ordered = sorted(teams.values(), key=lambda team: team.waiver_priority)
    winner_ids = list(dict.fromkeys(winners_in_order))
    reordered = [team for team in ordered if team.id not in winner_ids]
    reordered.extend(teams[team_id] for team_id in winner_ids)
    for priority, team in enumerate(reordered, 1):
        team.waiver_priority = priority

    period.status = "PROCESSED"
    period.processed_at = now
    db.flush()
    return list(
        db.scalars(
            select(WaiverClaim)
            .where(WaiverClaim.waiver_period_id == period.id)
            .order_by(WaiverClaim.team_id, WaiverClaim.priority)
        )
    )
