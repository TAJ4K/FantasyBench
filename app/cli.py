from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import create_llm_provider
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.fixtures.players import seed_fixture_players
from app.jobs.draft_runner import DraftRunner
from app.models.entities import Draft, League, WaiverPeriod
from app.services.draft import DraftService
from app.services.initialization import initialize_league
from app.services.waivers import process_waivers

cli = typer.Typer(help="Fantasy Bench commissioner CLI")


def _league(db: Session, league_id: str | None) -> League:
    query = select(League)
    if league_id:
        query = query.where(League.id == league_id)
    league = db.scalar(query.order_by(League.created_at.desc()).limit(1))
    if not league:
        raise typer.BadParameter("No initialized league was found")
    return league


@cli.command("init-league")
def init_league(
    season: int = typer.Option(..., help="NFL season year"),
    name: str = "Fantasy Bench",
    seed_fixtures: bool = False,
) -> None:
    settings = get_settings()
    if settings.app_env == "production" and seed_fixtures:
        raise typer.BadParameter("Fixture player seeding is forbidden in production")
    with SessionLocal() as db:
        league = initialize_league(
            db,
            name=name,
            nfl_season=season,
            settings={
                "waiver_period_hours": settings.waiver_period_hours,
                "waiver_processing_grace_minutes": settings.waiver_processing_grace_minutes,
            },
        )
        seeded = seed_fixture_players(db) if seed_fixtures else 0
        draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
        if draft is None:
            raise typer.BadParameter("Initialized league has no draft")
        db.commit()
        typer.echo(f"league={league.id} teams=8 players_seeded={seeded} draft={draft.status}")


@cli.command("run-draft")
def run_draft(league_id: str | None = None) -> None:
    async def run() -> None:
        settings = get_settings()
        provider = create_llm_provider(settings)
        with SessionLocal() as db:
            league = _league(db, league_id)
            draft = db.scalar(select(Draft).where(Draft.league_id == league.id))
            if draft is None or draft.status == "NOT_STARTED":
                raise typer.BadParameter(
                    "Draft is NOT_STARTED. Start it only through POST /api/v1/draft/start."
                )
            selected_id = league.id
        runner = DraftRunner(SessionLocal, provider, settings)
        await runner.run(selected_id)
        close = getattr(provider, "aclose", None)
        if close:
            await close()

    asyncio.run(run())


@cli.command("draft-pause")
def draft_pause(league_id: str | None = None) -> None:
    with SessionLocal() as db:
        league = _league(db, league_id)
        draft = DraftService(db).pause(league.id)
        db.commit()
        typer.echo(draft.status)


@cli.command("draft-resume")
def draft_resume(league_id: str | None = None) -> None:
    with SessionLocal() as db:
        league = _league(db, league_id)
        draft = DraftService(db).resume(league.id).draft
        db.commit()
        typer.echo(draft.status)


@cli.command("open-waivers")
def open_waivers(week: int, hours: int = 24, league_id: str | None = None) -> None:
    with SessionLocal() as db:
        league = _league(db, league_id)
        settings = get_settings()
        deadline = datetime.now(UTC) + timedelta(hours=hours)
        period = WaiverPeriod(
            league_id=league.id,
            season=league.nfl_season,
            week=week,
            status="OPEN",
            deadline_at=deadline,
            processing_at=deadline + timedelta(minutes=settings.waiver_processing_grace_minutes),
        )
        db.add(period)
        db.commit()
        typer.echo(period.id)


@cli.command("process-waivers")
def process_waiver_period(period_id: str) -> None:
    with SessionLocal() as db:
        claims = process_waivers(db, waiver_period_id=period_id, idempotency_key=f"cli:{period_id}")
        db.commit()
        typer.echo(f"processed={len(claims)} winners={sum(c.status == 'WON' for c in claims)}")


if __name__ == "__main__":
    cli()
