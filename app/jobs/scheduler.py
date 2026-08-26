from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.jobs.draft_runner import DraftRunner
from app.jobs.manager_automation import ManagerAutomation
from app.models.base import utcnow
from app.models.entities import (
    Draft,
    FantasyWeek,
    JobRun,
    League,
    Matchup,
    NflGame,
    PlayerWeekStat,
    TradeThread,
    WaiverPeriod,
)
from app.models.enums import DraftStatus
from app.nfl import NFLDataSyncService, NflverseProvider, SleeperProvider
from app.services.competition import calculate_matchup, complete_matchup
from app.services.events import emit_event
from app.services.playoffs import advance_playoffs, seed_playoffs
from app.services.scoring import persist_player_score
from app.services.trades import expire_trades
from app.services.waivers import ensure_waiver_period, process_waivers

logger = logging.getLogger(__name__)


class LeagueScheduler:
    """Small in-process scheduler for idempotent league deadlines and recovery.

    A single app worker is the supported deployment topology. Database locks and
    idempotency keys protect operations if an administrator retries them.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        draft_runner: DraftRunner,
        manager_automation: ManagerAutomation,
        poll_seconds: float,
    ) -> None:
        self.session_factory = session_factory
        self.draft_runner = draft_runner
        self.manager_automation = manager_automation
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._manager_tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run(), name="league-scheduler")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        manager_tasks = [task for task in self._manager_tasks if not task.done()]
        for task in manager_tasks:
            task.cancel()
        if manager_tasks:
            await asyncio.gather(*manager_tasks, return_exceptions=True)
        self._manager_tasks.clear()

    async def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler_tick_failed")
            await asyncio.sleep(self.poll_seconds)

    def tick(self) -> None:
        now = datetime.now(UTC)
        manager_jobs: list[tuple[str, str, str, int | None, int]] = []
        with self.session_factory() as db:
            try:
                recoverable = list(
                    db.scalars(
                        select(JobRun).where(
                            JobRun.status.in_(("RUNNING", "FAILED")),
                            JobRun.attempt_count
                            < self.manager_automation.settings.job_max_attempts,
                        )
                    )
                )
                for prior in recoverable:
                    metadata = prior.details or {}
                    kind = metadata.get("_kind")
                    target_id = metadata.get("_target_id")
                    if not kind or not target_id:
                        continue
                    job = self._claim_job(
                        db,
                        prior.job_name,
                        prior.idempotency_key,
                        kind=str(kind),
                        target_id=str(target_id),
                        week=metadata.get("_week"),
                        now=now,
                    )
                    if job:
                        manager_jobs.append(
                            (
                                str(kind),
                                job.id,
                                str(target_id),
                                metadata.get("_week"),
                                job.attempt_count,
                            )
                        )

                for league_id in db.scalars(
                    select(Draft.league_id)
                    .join(League, League.id == Draft.league_id)
                    .where(
                        Draft.status == DraftStatus.ACTIVE.value,
                        League.locked.is_(False),
                    )
                ):
                    self.draft_runner.start(league_id)

                all_leagues = list(db.scalars(select(League).where(League.locked.is_(False))))
                schedule_bucket = now.date().isoformat()
                for league in all_leagues:
                    job = self._claim_job(
                        db,
                        "nfl_schedule_sync",
                        f"{league.nfl_season}:{schedule_bucket}",
                        kind="nfl_schedule",
                        target_id=league.id,
                        week=None,
                        now=now,
                    )
                    if job:
                        manager_jobs.append(
                            ("nfl_schedule", job.id, league.id, None, job.attempt_count)
                        )
                    players_job = self._claim_job(
                        db,
                        "nfl_player_sync",
                        schedule_bucket,
                        kind="nfl_players",
                        target_id=league.id,
                        week=None,
                        now=now,
                    )
                    if players_job:
                        manager_jobs.append(
                            (
                                "nfl_players",
                                players_job.id,
                                league.id,
                                None,
                                players_job.attempt_count,
                            )
                        )

                due_periods = list(
                    db.scalars(
                        select(WaiverPeriod)
                        .join(League, League.id == WaiverPeriod.league_id)
                        .where(
                            WaiverPeriod.status == "OPEN",
                            func.coalesce(WaiverPeriod.processing_at, WaiverPeriod.deadline_at)
                            <= now,
                            League.locked.is_(False),
                        )
                    )
                )
                for period in due_periods:
                    collection_job = db.scalar(
                        select(JobRun)
                        .where(
                            JobRun.job_name == "waiver_collection",
                            JobRun.idempotency_key == period.id,
                        )
                        .with_for_update()
                    )
                    collection_timed_out = bool(
                        collection_job is not None and collection_job.status == "RUNNING"
                    )
                    if collection_timed_out and collection_job is not None:
                        collection_job.status = "COMPLETE"
                        collection_job.completed_at = now
                        collection_job.lease_expires_at = None
                        collection_job.next_attempt_at = None
                        collection_job.error = (
                            "Collection stopped at the configured processing time"
                        )
                        collection_job.details = {
                            **(collection_job.details or {}),
                            "timed_out": True,
                            "processing_at": now.isoformat(),
                        }
                    process_waivers(
                        db,
                        waiver_period_id=period.id,
                        idempotency_key=f"scheduled:{period.id}",
                        processed_at=now,
                    )
                    emit_event(
                        db,
                        period.league_id,
                        "WAIVER_PROCESSED",
                        aggregate_type="WAIVER_PERIOD",
                        aggregate_id=period.id,
                        data={"collection_timed_out": collection_timed_out},
                    )
                    free_agent_job = self._claim_job(
                        db,
                        "free_agent_review",
                        period.id,
                        kind="free_agent",
                        target_id=period.league_id,
                        week=period.week,
                        now=now,
                    )
                    if free_agent_job:
                        manager_jobs.append(
                            (
                                "free_agent",
                                free_agent_job.id,
                                period.league_id,
                                period.week,
                                free_agent_job.attempt_count,
                            )
                        )

                expiring_ids = list(
                    db.scalars(
                        select(TradeThread.id)
                        .join(League, League.id == TradeThread.league_id)
                        .where(
                            TradeThread.status.in_(("PROPOSED", "COUNTERED")),
                            TradeThread.expires_at.is_not(None),
                            TradeThread.expires_at <= now,
                            League.locked.is_(False),
                        )
                    )
                )
                expired = expire_trades(db, now=now)
                for thread in expired:
                    if thread.id in expiring_ids:
                        emit_event(
                            db,
                            thread.league_id,
                            "TRADE_EXPIRED",
                            aggregate_type="TRADE",
                            aggregate_id=thread.id,
                        )

                leagues = list(
                    db.scalars(
                        select(League).where(
                            League.current_week > 0,
                            League.locked.is_(False),
                            League.status.in_(("REGULAR_SEASON", "PLAYOFFS")),
                        )
                    )
                )
                for league in leagues:
                    ensure_waiver_period(db, league=league, week=league.current_week)
                    injury_bucket = int(now.timestamp() // (6 * 3600))
                    injury_job = self._claim_job(
                        db,
                        "nfl_injury_sync",
                        f"{league.id}:{league.current_week}:{injury_bucket}",
                        kind="nfl_injuries",
                        target_id=league.id,
                        week=league.current_week,
                        now=now,
                    )
                    if injury_job:
                        manager_jobs.append(
                            (
                                "nfl_injuries",
                                injury_job.id,
                                league.id,
                                league.current_week,
                                injury_job.attempt_count,
                            )
                        )
                    stats_bucket = int(now.timestamp() // 3600)
                    stats_job = self._claim_job(
                        db,
                        "nfl_stats_scoring",
                        f"{league.id}:{league.current_week}:{stats_bucket}",
                        kind="nfl_stats",
                        target_id=league.id,
                        week=league.current_week,
                        now=now,
                    )
                    if stats_job:
                        manager_jobs.append(
                            (
                                "nfl_stats",
                                stats_job.id,
                                league.id,
                                league.current_week,
                                stats_job.attempt_count,
                            )
                        )
                    kickoffs = list(
                        db.scalars(
                            select(NflGame.kickoff_at)
                            .where(
                                NflGame.season == league.nfl_season,
                                NflGame.week == league.current_week,
                                NflGame.kickoff_at > now,
                            )
                            .distinct()
                            .order_by(NflGame.kickoff_at)
                        )
                    )
                    for kickoff in kickoffs:
                        if kickoff.tzinfo is None:
                            kickoff = kickoff.replace(tzinfo=UTC)
                        hours = (kickoff - now).total_seconds() / 3600
                        for window in self.manager_automation.settings.lineup_review_windows:
                            if 0 < hours <= window:
                                key = (
                                    f"{league.id}:{league.current_week}:"
                                    f"{kickoff.isoformat()}:{window:g}"
                                )
                                job = self._claim_job(
                                    db,
                                    "lineup_review",
                                    key,
                                    kind="lineup",
                                    target_id=league.id,
                                    week=league.current_week,
                                    now=now,
                                )
                                if job:
                                    manager_jobs.append(
                                        (
                                            "lineup",
                                            job.id,
                                            league.id,
                                            league.current_week,
                                            job.attempt_count,
                                        )
                                    )
                    trade_interval = self.manager_automation.settings.trade_review_interval_hours
                    trade_bucket = int(now.timestamp() // (trade_interval * 3600))
                    trade_job = self._claim_job(
                        db,
                        "trade_review",
                        f"{league.id}:{trade_bucket}",
                        kind="trade",
                        target_id=league.id,
                        week=None,
                        now=now,
                    )
                    if trade_job:
                        manager_jobs.append(
                            ("trade", trade_job.id, league.id, None, trade_job.attempt_count)
                        )

                collection_window = (
                    self.manager_automation.settings.waiver_collection_hours_before_deadline
                )
                future_periods = list(
                    db.scalars(
                        select(WaiverPeriod)
                        .join(League, League.id == WaiverPeriod.league_id)
                        .where(
                            WaiverPeriod.status == "OPEN",
                            WaiverPeriod.deadline_at > now,
                            League.locked.is_(False),
                        )
                    )
                )
                for period in future_periods:
                    deadline = period.deadline_at
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC)
                    hours = (deadline - now).total_seconds() / 3600
                    if hours <= collection_window:
                        job = self._claim_job(
                            db,
                            "waiver_collection",
                            period.id,
                            kind="waiver",
                            target_id=period.id,
                            week=None,
                            now=now,
                        )
                        if job:
                            manager_jobs.append(
                                ("waiver", job.id, period.id, None, job.attempt_count)
                            )
                db.commit()
            except Exception:
                db.rollback()
                raise
        for kind, job_id, target_id, week, attempt in manager_jobs:
            task = asyncio.create_task(
                self._execute_manager_job(kind, job_id, target_id, week, attempt),
                name=f"manager-job:{job_id}",
            )
            self._manager_tasks.add(task)
            task.add_done_callback(self._manager_tasks.discard)

    def _claim_job(
        self,
        db: Session,
        name: str,
        key: str,
        *,
        kind: str,
        target_id: str,
        week: int | None,
        now: datetime,
    ) -> JobRun | None:
        job = db.scalar(
            select(JobRun)
            .where(JobRun.job_name == name, JobRun.idempotency_key == key)
            .with_for_update()
        )
        if job is not None:
            lease = job.lease_expires_at
            retry_at = job.next_attempt_at
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            if retry_at is not None and retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            if job.status == "COMPLETE":
                return None
            if job.status == "RUNNING" and lease is not None and lease > now:
                return None
            if job.status == "FAILED" and retry_at is not None and retry_at > now:
                return None
            if job.attempt_count >= self.manager_automation.settings.job_max_attempts:
                return None
        else:
            job = JobRun(job_name=name, idempotency_key=key, status="RUNNING")
            db.add(job)
        job.status = "RUNNING"
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.started_at = now
        job.completed_at = None
        job.duration_ms = None
        job.lease_expires_at = now + timedelta(
            seconds=self.manager_automation.settings.job_lease_seconds
        )
        job.next_attempt_at = None
        job.error = None
        job.details = {
            **(job.details or {}),
            "_kind": kind,
            "_target_id": target_id,
            "_week": week,
        }
        db.flush()
        return job

    async def _execute_manager_job(
        self,
        kind: str,
        job_id: str,
        target_id: str,
        week: int | None,
        attempt: int,
    ) -> None:
        try:
            if kind == "lineup":
                assert week is not None
                operation = self.manager_automation.set_all_lineups(target_id, week)
            elif kind == "waiver":
                operation = self.manager_automation.collect_waiver_claims(target_id)
            elif kind == "free_agent":
                assert week is not None
                operation = self.manager_automation.review_free_agents(target_id, week)
            elif kind == "trade":
                operation = self.manager_automation.review_trades(target_id)
            elif kind == "nfl_schedule":
                operation = self._sync_schedule(target_id)
            elif kind == "nfl_players":
                operation = self._sync_players(target_id)
            elif kind == "nfl_injuries":
                assert week is not None
                operation = self._sync_injuries(target_id, week)
            elif kind == "nfl_stats":
                assert week is not None
                operation = self._sync_stats_and_score(target_id, week)
            else:
                raise ValueError(f"Unknown scheduled job kind {kind!r}")
            details = await self._await_with_heartbeat(job_id, attempt, operation)
            failures = [value for value in details.values() if value.startswith("FAILED:")]
            status = "FAILED" if failures else "COMPLETE"
            error = f"{len(failures)} manager action(s) failed" if failures else None
        except asyncio.CancelledError:
            with self.session_factory() as db:
                job = db.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())
                if job and job.status == "RUNNING" and job.attempt_count == attempt:
                    job.status = "FAILED"
                    job.completed_at = utcnow()
                    job.lease_expires_at = None
                    job.next_attempt_at = job.completed_at
                    job.error = "Job cancelled during application shutdown"
                    db.commit()
            raise
        except Exception as exc:
            details = {}
            status = "FAILED"
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("manager_job_failed", extra={"job_id": job_id})
        with self.session_factory() as db:
            job = db.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())
            if job and job.status == "RUNNING" and job.attempt_count == attempt:
                job.status = status
                job.completed_at = utcnow()
                job.lease_expires_at = None
                started_at = job.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                job.duration_ms = int((job.completed_at - started_at).total_seconds() * 1000)
                job.details = {**(job.details or {}), **details}
                job.error = error
                if (
                    status == "FAILED"
                    and attempt < self.manager_automation.settings.job_max_attempts
                ):
                    job.next_attempt_at = job.completed_at + timedelta(
                        seconds=self.manager_automation.settings.job_retry_base_seconds
                        * (2 ** (attempt - 1))
                    )
                db.commit()

    async def _await_with_heartbeat(
        self,
        job_id: str,
        attempt: int,
        operation: Any,
    ) -> dict[str, str]:
        task = asyncio.create_task(operation)
        interval = min(30.0, self.manager_automation.settings.job_lease_seconds / 3)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    break
                now = utcnow()
                with self.session_factory() as db:
                    job = db.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())
                    if job is None or job.status != "RUNNING" or job.attempt_count != attempt:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise RuntimeError("job lease lost")
                    job.lease_expires_at = now + timedelta(
                        seconds=self.manager_automation.settings.job_lease_seconds
                    )
                    db.commit()
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _sync_schedule(self, league_id: str) -> dict[str, str]:
        with self.session_factory() as db:
            league = db.get(League, league_id)
            if league is None:
                raise ValueError("league does not exist")
            season = league.nfl_season
        provider = NflverseProvider()
        try:
            with self.session_factory() as db:
                result = await NFLDataSyncService(db, provider).sync_schedule(season)
            return {
                "inserted": str(result.inserted),
                "updated": str(result.updated),
                "skipped": str(result.skipped),
            }
        finally:
            await provider.aclose()

    async def _sync_injuries(self, league_id: str, week: int) -> dict[str, str]:
        with self.session_factory() as db:
            league = db.get(League, league_id)
            if league is None:
                raise ValueError("league does not exist")
            season = league.nfl_season
        provider = SleeperProvider()
        try:
            with self.session_factory() as db:
                result = await NFLDataSyncService(db, provider).sync_injuries(season, week)
            return {"updated": str(result.updated), "inserted": str(result.inserted)}
        finally:
            await provider.aclose()

    async def _sync_players(self, league_id: str) -> dict[str, str]:
        with self.session_factory() as db:
            league = db.get(League, league_id)
            if league is None:
                raise ValueError("league does not exist")
            season = league.nfl_season
        provider = SleeperProvider()
        try:
            with self.session_factory() as db:
                result = await NFLDataSyncService(db, provider).sync_players(season)
            return {"updated": str(result.updated), "inserted": str(result.inserted)}
        finally:
            await provider.aclose()

    async def _sync_stats_and_score(self, league_id: str, week: int) -> dict[str, str]:
        with self.session_factory() as db:
            league = db.get(League, league_id)
            if league is None:
                raise ValueError("league does not exist")
            season = league.nfl_season
        provider = NflverseProvider()
        try:
            with self.session_factory() as db:
                result = await NFLDataSyncService(db, provider).sync_week_stats(season, week)
        finally:
            await provider.aclose()

        completed = False
        with self.session_factory() as db:
            league = db.scalar(select(League).where(League.id == league_id).with_for_update())
            if league is None:
                raise ValueError("league does not exist")
            stats = list(
                db.scalars(
                    select(PlayerWeekStat).where(
                        PlayerWeekStat.season == season,
                        PlayerWeekStat.week == week,
                    )
                )
            )
            for stat in stats:
                persist_player_score(
                    db,
                    league_id=league_id,
                    player_id=stat.player_id,
                    season=season,
                    week=week,
                    raw_stats=stat.raw_stats,
                    scoring_config=league.scoring_config,
                )
            matchups = list(
                db.scalars(
                    select(Matchup).where(Matchup.league_id == league_id, Matchup.week == week)
                )
            )
            for matchup in matchups:
                calculate_matchup(db, matchup_id=matchup.id, season=season)
            games = list(
                db.scalars(select(NflGame).where(NflGame.season == season, NflGame.week == week))
            )
            all_final = bool(games) and all(game.status == "FINAL" for game in games)
            already_complete = bool(matchups) and all(
                matchup.status == "COMPLETE" for matchup in matchups
            )
            if all_final and matchups and not already_complete:
                for matchup in matchups:
                    complete_matchup(db, matchup_id=matchup.id, season=season)
                emit_event(db, league_id, "WEEK_COMPLETED", data={"week": week})
                completed = True
                self._advance_after_week(db, league, week)
            db.commit()
        return {
            "inserted": str(result.inserted),
            "updated": str(result.updated),
            "week_completed": str(completed).lower(),
        }

    @staticmethod
    def _advance_after_week(db: Session, league: League, week: int) -> None:
        regular_end = int(league.settings.get("regular_season_weeks", 14))
        playoff_weeks = list(league.settings.get("playoff_weeks", [16, 17]))
        if week < regular_end:
            next_week = week + 1
            league.current_week = next_week
            fantasy_week = db.scalar(
                select(FantasyWeek).where(
                    FantasyWeek.league_id == league.id,
                    FantasyWeek.week == next_week,
                )
            )
            if fantasy_week is not None:
                fantasy_week.status = "ACTIVE"
            ensure_waiver_period(db, league=league, week=next_week)
            emit_event(db, league.id, "WEEK_STARTED", data={"week": next_week})
        elif week == regular_end:
            seed_playoffs(db, league_id=league.id)
            if playoff_weeks:
                ensure_waiver_period(db, league=league, week=int(playoff_weeks[0]))
        elif playoff_weeks and week in playoff_weeks:
            advance_playoffs(db, league_id=league.id)
            position = playoff_weeks.index(week)
            if position + 1 < len(playoff_weeks):
                ensure_waiver_period(db, league=league, week=int(playoff_weeks[position + 1]))
