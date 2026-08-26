from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.base import Base

TEST_ADMIN_KEY = "test-admin-key"


@pytest.fixture
def engine() -> Generator[Engine]:
    database = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(database)
    try:
        yield database
    finally:
        Base.metadata.drop_all(database)
        database.dispose()


@pytest.fixture
def db(engine: Engine) -> Generator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()


@pytest.fixture
def app_client(engine: Engine) -> Generator[TestClient]:
    # Imported here so database/model tests do not require application startup.
    from app.main import app

    def test_db() -> Generator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    def test_settings() -> Settings:
        return Settings(
            app_env="test",
            database_url="sqlite+pysqlite://",
            admin_api_key=TEST_ADMIN_KEY,
            llm_provider="fake",
            draft_reveal_min_delay_seconds=0,
            draft_reveal_max_delay_seconds=0,
            auto_resume_draft=False,
        )

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_settings] = test_settings
    # These tests exercise synchronous request handlers only. Avoid starting the
    # production scheduler, whose session factory intentionally targets DATABASE_URL.
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-API-Key": TEST_ADMIN_KEY}
