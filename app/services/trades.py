from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.base import utcnow
from app.models.entities import (
    League,
    RosterAssignment,
    Team,
    TradeAsset,
    TradeMessage,
    TradeOffer,
    TradeThread,
)
from app.services.guards import ensure_league_unlocked
from app.services.rosters import RosterService
from app.services.transactions import _active_roster_limit, create_transaction


def _new_offer(
    db: Session,
    *,
    thread: TradeThread,
    proposer_team_id: str,
    recipient_team_id: str,
    send_player_ids: Iterable[str],
    receive_player_ids: Iterable[str],
    message: str | None,
    public_reasoning: str | None,
    parent_offer_id: str | None = None,
) -> TradeOffer:
    send = list(dict.fromkeys(send_player_ids))
    receive = list(dict.fromkeys(receive_player_ids))
    if not send or not receive or set(send) & set(receive):
        raise ConflictError("INVALID_TRADE_ASSETS", "Both sides must send distinct player assets.")
    offer = TradeOffer(
        thread_id=thread.id,
        proposer_team_id=proposer_team_id,
        recipient_team_id=recipient_team_id,
        sequence=thread.negotiation_rounds,
        status="PROPOSED" if parent_offer_id is None else "COUNTERED",
        message=message,
        public_reasoning=public_reasoning,
        parent_offer_id=parent_offer_id,
    )
    db.add(offer)
    db.flush()
    db.add_all(
        [
            TradeAsset(
                offer_id=offer.id,
                from_team_id=proposer_team_id,
                to_team_id=recipient_team_id,
                asset_type="PLAYER",
                player_id=player_id,
            )
            for player_id in send
        ]
        + [
            TradeAsset(
                offer_id=offer.id,
                from_team_id=recipient_team_id,
                to_team_id=proposer_team_id,
                asset_type="PLAYER",
                player_id=player_id,
            )
            for player_id in receive
        ]
    )
    if message:
        db.add(
            TradeMessage(
                thread_id=thread.id,
                sender_team_id=proposer_team_id,
                body=message,
                structured_action={
                    "action": "counter" if parent_offer_id else "propose",
                    "offer_id": offer.id,
                },
            )
        )
    db.flush()
    return offer


def propose_trade(
    db: Session,
    *,
    league_id: str,
    proposer_team_id: str,
    recipient_team_id: str,
    send_player_ids: Iterable[str],
    receive_player_ids: Iterable[str],
    message: str | None = None,
    public_reasoning: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[TradeThread, TradeOffer]:
    ensure_league_unlocked(db, league_id)
    if proposer_team_id == recipient_team_id:
        raise ConflictError("INVALID_TRADE_TEAMS", "A team cannot trade with itself.")
    teams = list(
        db.scalars(
            select(Team).where(
                Team.league_id == league_id,
                Team.id.in_([proposer_team_id, recipient_team_id]),
            )
        )
    )
    if len(teams) != 2:
        raise ConflictError("INVALID_TRADE_TEAMS", "Both teams must belong to the league.")
    thread = TradeThread(
        league_id=league_id,
        initiator_team_id=proposer_team_id,
        recipient_team_id=recipient_team_id,
        status="PROPOSED",
        negotiation_rounds=1,
        expires_at=expires_at,
    )
    db.add(thread)
    db.flush()
    offer = _new_offer(
        db,
        thread=thread,
        proposer_team_id=proposer_team_id,
        recipient_team_id=recipient_team_id,
        send_player_ids=send_player_ids,
        receive_player_ids=receive_player_ids,
        message=message,
        public_reasoning=public_reasoning,
    )
    _validate_assets(db, offer)
    return thread, offer


def _locked_offer(db: Session, offer_id: str) -> tuple[TradeThread, TradeOffer]:
    snapshot = db.get(TradeOffer, offer_id)
    if snapshot is None:
        raise NotFoundError("TradeOffer", offer_id)
    thread_snapshot = db.get(TradeThread, snapshot.thread_id)
    if thread_snapshot is None:
        raise NotFoundError("TradeThread", snapshot.thread_id)
    ensure_league_unlocked(db, thread_snapshot.league_id)
    thread = db.scalar(
        select(TradeThread).where(TradeThread.id == snapshot.thread_id).with_for_update()
    )
    offer = db.scalar(select(TradeOffer).where(TradeOffer.id == offer_id).with_for_update())
    if thread is None or offer is None:
        raise NotFoundError("TradeOffer", offer_id)
    if thread.expires_at is not None:
        expires = thread.expires_at
        now = utcnow()
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if expires <= now and thread.status in {"PROPOSED", "COUNTERED"}:
            thread.status = "EXPIRED"
            offer.status = "EXPIRED"
            db.flush()
            raise ConflictError("TRADE_EXPIRED", "This trade has expired.")
    return thread, offer


def _validate_assets(db: Session, offer: TradeOffer) -> list[tuple[TradeAsset, RosterAssignment]]:
    assets = list(db.scalars(select(TradeAsset).where(TradeAsset.offer_id == offer.id)))
    if not assets or any(
        asset.asset_type != "PLAYER" or asset.player_id is None for asset in assets
    ):
        raise ConflictError(
            "UNSUPPORTED_TRADE_ASSET", "Only player assets are currently executable."
        )
    assignments = {
        row.player_id: row
        for row in db.scalars(
            select(RosterAssignment)
            .where(
                RosterAssignment.player_id.in_([asset.player_id for asset in assets]),
                RosterAssignment.team_id.in_([asset.from_team_id for asset in assets]),
            )
            .with_for_update()
        )
    }
    paired: list[tuple[TradeAsset, RosterAssignment]] = []
    for asset in assets:
        assert asset.player_id is not None
        assignment = assignments.get(asset.player_id)
        if assignment is None or assignment.team_id != asset.from_team_id:
            raise ConflictError(
                "TRADE_ASSET_NOT_OWNED", "A traded player is no longer owned by its sender."
            )
        paired.append((asset, assignment))
    return paired


def counter_trade(
    db: Session,
    *,
    offer_id: str,
    countering_team_id: str,
    send_player_ids: Iterable[str],
    receive_player_ids: Iterable[str],
    message: str | None = None,
    public_reasoning: str | None = None,
    max_rounds: int = 4,
) -> tuple[TradeThread, TradeOffer]:
    thread, prior = _locked_offer(db, offer_id)
    if prior.sequence != thread.negotiation_rounds:
        raise ConflictError("TRADE_OFFER_SUPERSEDED", "A newer offer exists in this negotiation.")
    if thread.status not in {"PROPOSED", "COUNTERED"} or prior.status not in {
        "PROPOSED",
        "COUNTERED",
    }:
        raise ConflictError("TRADE_NOT_PENDING", "Only the current pending offer can be countered.")
    if prior.recipient_team_id != countering_team_id:
        raise ConflictError("NOT_TRADE_RECIPIENT", "Only the recipient may counter this offer.")
    if thread.negotiation_rounds >= max_rounds:
        raise ConflictError("TRADE_ROUND_LIMIT", "The negotiation round limit has been reached.")
    prior.status = "COUNTERED"
    thread.status = "COUNTERED"
    thread.negotiation_rounds += 1
    offer = _new_offer(
        db,
        thread=thread,
        proposer_team_id=countering_team_id,
        recipient_team_id=prior.proposer_team_id,
        send_player_ids=send_player_ids,
        receive_player_ids=receive_player_ids,
        message=message,
        public_reasoning=public_reasoning,
        parent_offer_id=prior.id,
    )
    _validate_assets(db, offer)
    return thread, offer


def accept_trade(db: Session, *, offer_id: str, accepting_team_id: str) -> TradeThread:
    thread, offer = _locked_offer(db, offer_id)
    if offer.sequence != thread.negotiation_rounds:
        raise ConflictError("TRADE_OFFER_SUPERSEDED", "A newer offer exists in this negotiation.")
    if thread.status not in {"PROPOSED", "COUNTERED"} or offer.status not in {
        "PROPOSED",
        "COUNTERED",
    }:
        raise ConflictError("TRADE_NOT_PENDING", "Only a pending offer can be accepted.")
    if offer.recipient_team_id != accepting_team_id:
        raise ConflictError("NOT_TRADE_RECIPIENT", "Only the recipient may accept this offer.")
    paired = _validate_assets(db, offer)
    if any(assignment.slot_type == "STARTER" for _, assignment in paired):
        raise ConflictError(
            "TRADE_STARTER_REQUIRES_LINEUP_CHANGE",
            "Move traded starters to the bench with a complete legal lineup before accepting.",
        )
    league = db.get(League, thread.league_id)
    if league is not None:
        roster_service = RosterService(db)
        if any(
            roster_service.is_player_locked(league, assignment.player) for _, assignment in paired
        ):
            raise ConflictError(
                "PLAYER_LOCKED", "A player in this trade has already reached kickoff."
            )
    team_ids = {asset.from_team_id for asset, _ in paired} | {
        asset.to_team_id for asset, _ in paired
    }
    # Load each roster under a write lock before calculating post-trade sizes.
    team_rosters = {
        team_id: list(
            db.scalars(
                select(RosterAssignment)
                .where(RosterAssignment.team_id == team_id)
                .with_for_update()
            )
        )
        for team_id in team_ids
    }
    roster_sizes = {
        team_id: sum(assignment.slot_type != "IR" for assignment in rows)
        for team_id, rows in team_rosters.items()
    }
    incoming = {team_id: 0 for team_id in team_ids}
    outgoing = {team_id: 0 for team_id in team_ids}
    for asset, assignment in paired:
        if assignment.slot_type != "IR":
            outgoing[asset.from_team_id] += 1
        incoming[asset.to_team_id] += 1
    if league is not None:
        limit = _active_roster_limit(league)
        if any(roster_sizes[t] - outgoing[t] + incoming[t] > limit for t in team_ids):
            raise ConflictError("TRADE_ROSTER_FULL", "The trade would exceed a roster limit.")

    offer.status = "ACCEPTED"
    thread.status = "ACCEPTED"
    for asset, assignment in paired:
        assignment.team_id = asset.to_team_id
        assignment.slot_type = "BENCH"
        assignment.position_slot = None
        assignment.acquired_via = "TRADE"
        create_transaction(
            db,
            league_id=thread.league_id,
            team_id=asset.to_team_id,
            player_id=asset.player_id,
            transaction_type="TRADE",
            week=league.current_week if league else None,
            idempotency_key=f"trade:{offer.id}:{asset.id}",
            details={
                "thread_id": thread.id,
                "offer_id": offer.id,
                "from_team_id": asset.from_team_id,
                "to_team_id": asset.to_team_id,
            },
        )
    thread.status = "PROCESSED"
    db.flush()
    return thread


def reject_trade(db: Session, *, offer_id: str, rejecting_team_id: str) -> TradeThread:
    thread, offer = _locked_offer(db, offer_id)
    if offer.sequence != thread.negotiation_rounds:
        raise ConflictError("TRADE_OFFER_SUPERSEDED", "A newer offer exists in this negotiation.")
    if offer.recipient_team_id != rejecting_team_id:
        raise ConflictError("NOT_TRADE_RECIPIENT", "Only the recipient may reject this offer.")
    if thread.status not in {"PROPOSED", "COUNTERED"}:
        raise ConflictError("TRADE_NOT_PENDING", "This trade is no longer pending.")
    offer.status = "REJECTED"
    thread.status = "REJECTED"
    db.flush()
    return thread


def cancel_trade(db: Session, *, offer_id: str, cancelling_team_id: str) -> TradeThread:
    thread, offer = _locked_offer(db, offer_id)
    if offer.sequence != thread.negotiation_rounds:
        raise ConflictError("TRADE_OFFER_SUPERSEDED", "A newer offer exists in this negotiation.")
    if offer.proposer_team_id != cancelling_team_id:
        raise ConflictError("NOT_TRADE_PROPOSER", "Only the proposer may cancel this offer.")
    if thread.status not in {"PROPOSED", "COUNTERED"}:
        raise ConflictError("TRADE_NOT_PENDING", "This trade is no longer pending.")
    offer.status = "CANCELLED"
    thread.status = "CANCELLED"
    db.flush()
    return thread


def expire_trades(db: Session, *, now: datetime | None = None) -> list[TradeThread]:
    instant = now or utcnow()
    threads = list(
        db.scalars(
            select(TradeThread)
            .join(League, League.id == TradeThread.league_id)
            .where(
                TradeThread.status.in_(["PROPOSED", "COUNTERED"]),
                TradeThread.expires_at.is_not(None),
                TradeThread.expires_at <= instant,
                League.locked.is_(False),
            )
        )
    )
    for thread in threads:
        thread.status = "EXPIRED"
        for offer in db.scalars(select(TradeOffer).where(TradeOffer.thread_id == thread.id)):
            if offer.status in {"PROPOSED", "COUNTERED"}:
                offer.status = "EXPIRED"
    db.flush()
    return threads
