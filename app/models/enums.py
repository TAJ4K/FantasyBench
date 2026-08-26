from __future__ import annotations

from enum import StrEnum


class LeagueStatus(StrEnum):
    SETUP = "SETUP"
    PRE_DRAFT = "PRE_DRAFT"
    DRAFTING = "DRAFTING"
    REGULAR_SEASON = "REGULAR_SEASON"
    PLAYOFFS = "PLAYOFFS"
    COMPLETE = "COMPLETE"
    LOCKED = "LOCKED"


class DraftStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class DraftPickState(StrEnum):
    WAITING_FOR_MANAGER = "WAITING_FOR_MANAGER"
    THINKING = "THINKING"
    PICK_LOCKED = "PICK_LOCKED"
    REVEAL_PENDING = "REVEAL_PENDING"
    REVEALED = "REVEALED"
    FAILED = "FAILED"
    UNDONE = "UNDONE"


class RosterSlotType(StrEnum):
    STARTER = "STARTER"
    BENCH = "BENCH"
    IR = "IR"


class WaiverClaimStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    CANCELLED = "CANCELLED"
    INVALID = "INVALID"


class WaiverPeriodStatus(StrEnum):
    OPEN = "OPEN"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"


class TradeStatus(StrEnum):
    PROPOSED = "PROPOSED"
    COUNTERED = "COUNTERED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    VETOED = "VETOED"
    PROCESSED = "PROCESSED"


class TransactionType(StrEnum):
    DRAFT = "DRAFT"
    WAIVER_ADD = "WAIVER_ADD"
    WAIVER_DROP = "WAIVER_DROP"
    FREE_AGENT_ADD = "FREE_AGENT_ADD"
    DROP = "DROP"
    TRADE = "TRADE"
    COMMISSIONER_ADD = "COMMISSIONER_ADD"
    COMMISSIONER_DROP = "COMMISSIONER_DROP"
    IR_MOVE = "IR_MOVE"


class MatchupStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    COMPLETE = "COMPLETE"


class DecisionType(StrEnum):
    DRAFT = "DRAFT"
    WAIVER = "WAIVER"
    LINEUP = "LINEUP"
    TRADE = "TRADE"
    MEMORY = "MEMORY"
    COMMENTARY = "COMMENTARY"
