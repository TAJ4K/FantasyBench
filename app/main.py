from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents import ManagerMemoryService, create_llm_provider
from app.api.draft_api import router as draft_router
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.mutations import router as mutation_router
from app.api.read_api import router as read_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.jobs.draft_runner import DraftRunner
from app.jobs.manager_automation import ManagerAutomation
from app.jobs.scheduler import LeagueScheduler

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    provider = create_llm_provider(settings)
    draft_runner = DraftRunner(SessionLocal, provider, settings)
    manager_automation = ManagerAutomation(SessionLocal, provider, settings)
    scheduler = LeagueScheduler(
        SessionLocal,
        draft_runner,
        manager_automation,
        settings.scheduler_poll_seconds,
    )
    app.state.settings = settings
    app.state.llm_provider = provider
    app.state.draft_runner = draft_runner
    app.state.manager_automation = manager_automation
    app.state.scheduler = scheduler
    app.state.memory_service_factory = ManagerMemoryService
    if settings.auto_resume_draft:
        try:
            await draft_runner.resume_active()
        except Exception:
            # Production entrypoints migrate before startup. Keeping startup alive
            # makes health checks and isolated API tests useful before initialization.
            logger.warning("draft_recovery_deferred", exc_info=True)
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await draft_runner.stop()
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()


app = FastAPI(
    title="Fantasy Bench API",
    version="0.1.0",
    description="Authoritative fantasy football league for eight LLM managers.",
    lifespan=lifespan,
)
install_error_handlers(app)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Admin-API-Key"],
    )

app.include_router(health_router)
app.include_router(read_router)
app.include_router(draft_router)
app.include_router(mutation_router)
