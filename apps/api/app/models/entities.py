from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class League(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leagues"
    __table_args__ = (UniqueConstraint("nfl_season", "name", name="uq_leagues_season_name"),)

    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="PRE_DRAFT", index=True)
    nfl_season: Mapped[int] = mapped_column(Integer, index=True)
    current_week: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    roster_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scoring_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    teams: Mapped[list[Team]] = relationship(back_populates="league", cascade="all, delete-orphan")
    draft: Mapped[Draft | None] = relationship(back_populates="league", uselist=False)


class Team(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("league_id", "key", name="uq_teams_league_key"),
        UniqueConstraint("league_id", "draft_position", name="uq_teams_league_draft_position"),
    )

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(160))
    model_display_name: Mapped[str] = mapped_column(String(160))
    model_identifier: Mapped[str] = mapped_column(String(200))
    reasoning_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft_position: Mapped[int] = mapped_column(Integer)
    waiver_priority: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    ties: Mapped[int] = mapped_column(Integer, default=0)
    points_for: Mapped[float] = mapped_column(Float, default=0.0)
    points_against: Mapped[float] = mapped_column(Float, default=0.0)
    streak: Mapped[str] = mapped_column(String(16), default="-")
    manager_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    league: Mapped[League] = relationship(back_populates="teams")
    roster: Mapped[list[RosterAssignment]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class Player(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "players"
    __table_args__ = (
        UniqueConstraint("gsis_id", name="uq_players_gsis_id"),
        Index("ix_players_name_position", "full_name", "position"),
    )

    full_name: Mapped[str] = mapped_column(String(180), index=True)
    first_name: Mapped[str | None] = mapped_column(String(90))
    last_name: Mapped[str | None] = mapped_column(String(90))
    position: Mapped[str] = mapped_column(String(10), index=True)
    nfl_team: Mapped[str | None] = mapped_column(String(5), index=True)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    injury_status: Mapped[str | None] = mapped_column(String(40), index=True)
    bye_week: Mapped[int | None] = mapped_column(Integer)
    gsis_id: Mapped[str | None] = mapped_column(String(40))
    sleeper_id: Mapped[str | None] = mapped_column(String(40), index=True)
    espn_id: Mapped[str | None] = mapped_column(String(40))
    sportradar_id: Mapped[str | None] = mapped_column(String(80))
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NflGame(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "nfl_games"
    __table_args__ = (UniqueConstraint("season", "provider_game_id"),)

    season: Mapped[int] = mapped_column(Integer, index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    provider_game_id: Mapped[str] = mapped_column(String(100))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    home_team: Mapped[str] = mapped_column(String(5))
    away_team: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PlayerWeekStat(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "player_week_stats"
    __table_args__ = (UniqueConstraint("player_id", "season", "week"),)

    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    raw_stats: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlayerNews(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "player_news"
    __table_args__ = (UniqueConstraint("provider", "provider_news_id"),)

    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    provider_news_id: Mapped[str] = mapped_column(String(120))
    headline: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RosterAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "roster_assignments"
    __table_args__ = (
        UniqueConstraint("league_id", "player_id", name="uq_roster_league_player"),
        Index("ix_roster_team_slot", "team_id", "slot_type", "position_slot"),
    )

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    slot_type: Mapped[str] = mapped_column(String(20), default="BENCH")
    position_slot: Mapped[str | None] = mapped_column(String(20))
    acquired_via: Mapped[str] = mapped_column(String(30))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    team: Mapped[Team] = relationship(back_populates="roster")
    player: Mapped[Player] = relationship()


class LineupDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "lineup_decisions"

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    lineup: Mapped[dict[str, str]] = mapped_column(JSON)
    public_reasoning: Mapped[str | None] = mapped_column(Text)
    llm_run_id: Mapped[str | None] = mapped_column(ForeignKey("llm_runs.id"))
    source: Mapped[str] = mapped_column(String(30), default="MANAGER")


class Draft(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "drafts"

    league_id: Mapped[str] = mapped_column(
        ForeignKey("leagues.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="NOT_STARTED", index=True)
    draft_type: Mapped[str] = mapped_column(String(20), default="SNAKE")
    rounds: Mapped[int] = mapped_column(Integer, default=15)
    current_pick_number: Mapped[int] = mapped_column(Integer, default=1)
    order: Mapped[list[str]] = mapped_column(JSON, default=list)
    random_seed: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure: Mapped[str | None] = mapped_column(Text)
    runner_id: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    league: Mapped[League] = relationship(back_populates="draft")
    picks: Mapped[list[DraftPick]] = relationship(
        back_populates="draft", cascade="all, delete-orphan", order_by="DraftPick.pick_number"
    )


class DraftPick(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "draft_picks"
    __table_args__ = (
        UniqueConstraint("draft_id", "pick_number", name="uq_draft_picks_pick_number"),
        UniqueConstraint("draft_id", "player_id", name="uq_draft_picks_player_id"),
    )

    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"), index=True)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"), index=True)
    pick_number: Mapped[int] = mapped_column(Integer)
    round: Mapped[int] = mapped_column(Integer)
    round_pick: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="WAITING_FOR_MANAGER", index=True)
    model: Mapped[str | None] = mapped_column(String(200))
    public_reasoning: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_run_id: Mapped[str | None] = mapped_column(ForeignKey("llm_runs.id"))
    decision_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reveal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    draft: Mapped[Draft] = relationship(back_populates="picks")
    team: Mapped[Team] = relationship()
    player: Mapped[Player | None] = relationship()


class WaiverPeriod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "waiver_periods"
    __table_args__ = (UniqueConstraint("league_id", "season", "week"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)


class WaiverClaim(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "waiver_claims"
    __table_args__ = (UniqueConstraint("waiver_period_id", "team_id", "priority"),)

    waiver_period_id: Mapped[str] = mapped_column(ForeignKey("waiver_periods.id"), index=True)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    add_player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    drop_player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"))
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    public_reasoning: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Transaction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("league_id", "idempotency_key"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"), index=True)
    transaction_type: Mapped[str] = mapped_column(String(40), index=True)
    week: Mapped[int | None] = mapped_column(Integer, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class TradeThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trade_threads"

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    initiator_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    recipient_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="PROPOSED", index=True)
    negotiation_rounds: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class TradeOffer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trade_offers"

    thread_id: Mapped[str] = mapped_column(
        ForeignKey("trade_threads.id", ondelete="CASCADE"), index=True
    )
    proposer_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    recipient_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="PROPOSED")
    message: Mapped[str | None] = mapped_column(Text)
    public_reasoning: Mapped[str | None] = mapped_column(Text)
    parent_offer_id: Mapped[str | None] = mapped_column(ForeignKey("trade_offers.id"))


class TradeAsset(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "trade_assets"

    offer_id: Mapped[str] = mapped_column(
        ForeignKey("trade_offers.id", ondelete="CASCADE"), index=True
    )
    from_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    to_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(30), default="PLAYER")
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"), index=True)
    future_asset: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class TradeMessage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "trade_messages"

    thread_id: Mapped[str] = mapped_column(
        ForeignKey("trade_threads.id", ondelete="CASCADE"), index=True
    )
    sender_team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    structured_action: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FantasyWeek(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "fantasy_weeks"
    __table_args__ = (UniqueConstraint("league_id", "week"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_playoff: Mapped[bool] = mapped_column(Boolean, default=False)


class Matchup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "matchups"
    __table_args__ = (UniqueConstraint("league_id", "week", "matchup_number"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    matchup_number: Mapped[int] = mapped_column(Integer)
    home_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    away_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    home_score: Mapped[float] = mapped_column(Float, default=0.0)
    away_score: Mapped[float] = mapped_column(Float, default=0.0)
    home_projected: Mapped[float | None] = mapped_column(Float)
    away_projected: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED", index=True)
    winner_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    playoff_round: Mapped[str | None] = mapped_column(String(30))


class PlayerFantasyScore(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "player_fantasy_scores"
    __table_args__ = (UniqueConstraint("league_id", "player_id", "season", "week"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer, index=True)
    raw_stats: Mapped[dict[str, float]] = mapped_column(JSON)
    breakdown: Mapped[dict[str, float]] = mapped_column(JSON)
    total: Mapped[float] = mapped_column(Float)
    scoring_config_hash: Mapped[str] = mapped_column(String(64))


class LeagueEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "league_events"

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    aggregate_type: Mapped[str | None] = mapped_column(String(40))
    aggregate_id: Mapped[str | None] = mapped_column(String(50), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    visibility: Mapped[str] = mapped_column(String(20), default="PUBLIC")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    public_commentary: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class LLMRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "llm_runs"
    __table_args__ = (
        Index("ix_llm_runs_team_type_started", "team_id", "decision_type", "started_at"),
    )

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    decision_type: Mapped[str] = mapped_column(String(30), index=True)
    prompt_version: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[str | None] = mapped_column(String(150), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parsed_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)


class ManagerMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manager_memory"
    __table_args__ = (UniqueConstraint("league_id", "team_id"),)

    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_llm_run_id: Mapped[str | None] = mapped_column(ForeignKey("llm_runs.id"))


class JobRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job_name", "idempotency_key"),)

    job_name: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
