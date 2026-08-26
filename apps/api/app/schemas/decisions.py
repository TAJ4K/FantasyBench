from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DraftDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["draft_player"]
    player_id: str
    public_reasoning: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class WaiverClaimDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add_player_id: str
    drop_player_id: str | None = None
    priority: int = Field(ge=1)


class WaiverDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[WaiverClaimDecision]
    public_reasoning: str = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_priorities(self) -> WaiverDecision:
        priorities = [claim.priority for claim in self.claims]
        if len(priorities) != len(set(priorities)):
            raise ValueError("waiver claim priorities must be unique")
        return self


class LineupDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineup: dict[str, str]
    public_reasoning: str = Field(max_length=1000)


class TradeAssetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["player"]
    id: str


class TradeProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["propose_trade", "pass"]
    to_team_id: str | None = None
    send: list[TradeAssetDecision] = Field(default_factory=list)
    receive: list[TradeAssetDecision] = Field(default_factory=list)
    message: str = Field(default="", max_length=1000)
    public_reasoning: str = Field(max_length=1000)

    @model_validator(mode="after")
    def proposal_is_complete(self) -> TradeProposalDecision:
        if self.action == "propose_trade" and (
            not self.to_team_id or not self.send or not self.receive or not self.message
        ):
            raise ValueError("a trade proposal requires a recipient, assets, and message")
        return self


class TradeResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject", "counter"]
    offer_id: str
    send: list[TradeAssetDecision] = Field(default_factory=list)
    receive: list[TradeAssetDecision] = Field(default_factory=list)
    message: str = Field(max_length=1000)
    public_reasoning: str = Field(max_length=1000)

    @model_validator(mode="after")
    def counter_is_complete(self) -> TradeResponseDecision:
        if self.action == "counter" and (not self.send or not self.receive):
            raise ValueError("a counteroffer requires assets from both sides")
        return self


class MemorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_building_philosophy: str = ""
    positions_of_concern: list[str] = Field(default_factory=list)
    valued_player_ids: list[str] = Field(default_factory=list)
    trade_target_player_ids: list[str] = Field(default_factory=list)
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    recent_decisions: list[str] = Field(default_factory=list, max_length=12)
    strategic_priorities: list[str] = Field(default_factory=list, max_length=8)
