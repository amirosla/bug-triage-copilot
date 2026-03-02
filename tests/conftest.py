"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# ── Force mock LLM provider in tests ─────────────────────────────────────────
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://triage:triage@localhost:5432/bug_triage_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
# Use "changeme" so webhook signature verification is skipped in tests
# (the _verify_signature function skips when secret == "changeme")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "changeme")

from core.models.db import Base  # noqa: E402 — after env setup


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── In-memory SQLite for unit tests (no Postgres needed) ─────────────────────
@pytest.fixture(scope="session")
def sqlite_engine():
    """SQLite in-memory engine for fast unit tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def db_session(sqlite_engine) -> Generator[Session, None, None]:
    """Clean DB session for each test (uses SQLite)."""
    SessionLocal = sessionmaker(bind=sqlite_engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


# ── Webhook fixtures ──────────────────────────────────────────────────────────
@pytest.fixture
def bug_payload() -> dict:
    return json.loads((FIXTURES_DIR / "issue_opened_bug.json").read_text())


@pytest.fixture
def question_payload() -> dict:
    return json.loads((FIXTURES_DIR / "issue_opened_question.json").read_text())


@pytest.fixture
def minimal_payload() -> dict:
    return json.loads((FIXTURES_DIR / "issue_opened_minimal.json").read_text())


# ── FastAPI test client (mocking DB) ─────────────────────────────────────────
@pytest.fixture
def api_client():
    """Test client with mocked database and Redis."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    # StaticPool ensures all connections use the same in-memory database
    # (required for SQLite :memory: to persist between requests)
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(_engine)
    _TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False)

    with patch("apps.api.routes.webhooks._get_queue") as mock_queue:
        mock_queue.return_value = MagicMock()
        mock_queue.return_value.enqueue = MagicMock(return_value=MagicMock(id="test-job-id"))

        from apps.api.main import app
        from core.db.session import get_db

        def override_get_db():
            db = _TestSession()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app) as client:
            yield client
        app.dependency_overrides.clear()

    Base.metadata.drop_all(_engine)
