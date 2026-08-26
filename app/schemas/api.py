from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class LeagueInitializeRequest(BaseModel):
    name: str = "Fantasy Bench"
    nfl_season: int = Field(ge=2000, le=2200)
    randomize_draft_order: bool = False
    random_seed: int | None = None
    seed_fixture_players: bool = False


class DraftOrderRequest(BaseModel):
    team_ids: list[str] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def unique_teams(self) -> DraftOrderRequest:
        if len(set(self.team_ids)) != 8:
            raise ValueError("draft order must contain eight unique teams")
        return self


class AdminDraftPickRequest(BaseModel):
    player_id: str
    public_reasoning: str = "Commissioner selection"
    confidence: float = Field(default=1.0, ge=0, le=1)


class LineupSetRequest(BaseModel):
    lineup: dict[str, str]
    public_reasoning: str = ""


class AddDropRequest(BaseModel):
    team_id: str
    add_player_id: str
    drop_player_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=160)


class DropRequest(BaseModel):
    team_id: str
    player_id: str
    idempotency_key: str = Field(min_length=8, max_length=160)


class WaiverPeriodRequest(BaseModel):
    week: int = Field(ge=1, le=30)
    deadline_at: datetime


class WaiverClaimItem(BaseModel):
    add_player_id: str
    drop_player_id: str | None = None
    bid: int = Field(ge=0)
    priority: int = Field(ge=1)


class WaiverClaimsRequest(BaseModel):
    team_id: str
    claims: list[WaiverClaimItem]
    public_reasoning: str = ""


class TradeAssetRequest(BaseModel):
    type: Literal["player"] = "player"
    id: str


class TradeProposalRequest(BaseModel):
    from_team_id: str
    to_team_id: str
    send: list[TradeAssetRequest] = Field(min_length=1)
    receive: list[TradeAssetRequest] = Field(min_length=1)
    message: str = Field(default="", max_length=1000)
    public_reasoning: str = Field(default="", max_length=1000)
    expires_at: datetime | None = None


class TradeCounterRequest(BaseModel):
    proposer_team_id: str
    send: list[TradeAssetRequest] = Field(min_length=1)
    receive: list[TradeAssetRequest] = Field(min_length=1)
    message: str = Field(default="", max_length=1000)
    public_reasoning: str = Field(default="", max_length=1000)


class TradeActionRequest(BaseModel):
    team_id: str
    message: str = Field(default="", max_length=1000)


class StatsLoadItem(BaseModel):
    player_id: str
    stats: dict[str, float]


class StatsLoadRequest(BaseModel):
    week: int = Field(ge=1, le=30)
    provider: str = "fixture"
    stats: list[StatsLoadItem]


class FaabAdjustmentRequest(BaseModel):
    amount: int
    reason: str = Field(min_length=1, max_length=500)


class TriggerDecisionRequest(BaseModel):
    team_id: str
    decision_type: Literal["DRAFT", "WAIVER", "LINEUP", "TRADE", "MEMORY", "COMMENTARY"]
    context: dict[str, Any] = Field(default_factory=dict)
