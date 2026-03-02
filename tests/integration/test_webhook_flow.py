"""Integration tests for the full webhook → job → triage result flow.

These tests use SQLite in-memory and mock GitHub API calls.
They exercise the complete triage pipeline end-to-end.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.models.db import Base, Issue, Repo, TriageResult, TriageStatus, WebhookDelivery

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(scope="module")
def integration_engine():
    """Shared in-memory SQLite engine for integration tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def integration_db(integration_engine) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=integration_engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def seeded_repo(integration_db) -> Repo:
    """Pre-seed a repo for testing."""
    repo = Repo(
        full_name="acme/myapp",
        github_installation_id=99001,
        config={
            "allowed_labels": ["bug", "enhancement", "question"],
            "auto_apply_labels": False,
            "label_confidence_threshold": 0.75,
            "similarity_threshold": 0.80,
        },
    )
    integration_db.add(repo)
    integration_db.flush()
    return repo


class TestTriageJobFlow:
    """Tests for the triage_issue job function."""

    def test_triage_job_marks_issue_done(self, integration_db, seeded_repo):
        """Full triage pipeline should mark issue as done."""
        # Create an issue
        issue = Issue(
            repo_id=seeded_repo.id,
            github_issue_number=100,
            github_issue_id=9001001,
            title="App crashes when uploading files",
            body="Steps: 1. Upload > 10MB file\n2. See MemoryError crash",
            author="testuser",
            state="open",
            triage_status=TriageStatus.pending,
            last_delivery_id="delivery-test-001",
        )
        integration_db.add(issue)

        delivery = WebhookDelivery(
            delivery_id="delivery-test-001",
            event="issues",
            action="opened",
            payload={},
            status="enqueued",
        )
        integration_db.add(delivery)
        integration_db.flush()

        # Patch get_session to return our test session
        from contextlib import contextmanager

        @contextmanager
        def mock_get_session():
            try:
                yield integration_db
                integration_db.flush()  # mirror real get_session commit
            except Exception:
                raise

        with (
            patch("apps.worker.jobs.triage.get_session", mock_get_session),
            patch("apps.worker.jobs.triage.get_llm_client") as mock_llm,
        ):
            from core.models.schemas import TriageOutput

            mock_client = MagicMock()
            mock_client.generate_triage.return_value = (
                TriageOutput(
                    summary_bullets=["File upload crashes", "MemoryError in upload.py"],
                    priority="P1",
                    priority_reason="Production crash affects all users uploading large files.",
                    suggested_labels=[
                        {"label": "bug", "confidence": 0.95, "reason": "Clear crash"},
                    ],
                    questions=[],
                    repro_steps=["Upload > 10MB file"],
                    needs_more_info=False,
                ),
                {"model": "test-model", "tokens_in": 100, "tokens_out": 50},
            )
            mock_client.embed.return_value = [0.1] * 1536
            mock_llm.return_value = mock_client

            from apps.worker.jobs.triage import triage_issue

            triage_issue(str(issue.id), "delivery-test-001")

        # Python object is already updated (same identity map object)
        assert issue.triage_status == TriageStatus.done

    def test_triage_creates_result_record(self, integration_db, seeded_repo):
        """Triage job should create a TriageResult row."""
        issue = Issue(
            repo_id=seeded_repo.id,
            github_issue_number=101,
            github_issue_id=9001002,
            title="Documentation is unclear",
            body="The docs for configuration are outdated.",
            author="docsreader",
            state="open",
            triage_status=TriageStatus.pending,
            last_delivery_id="delivery-test-002",
        )
        integration_db.add(issue)
        delivery = WebhookDelivery(
            delivery_id="delivery-test-002",
            event="issues",
            action="opened",
            payload={},
            status="enqueued",
        )
        integration_db.add(delivery)
        integration_db.flush()

        from contextlib import contextmanager

        @contextmanager
        def mock_get_session():
            try:
                yield integration_db
                integration_db.flush()  # mirror real get_session commit
            except Exception:
                raise

        with (
            patch("apps.worker.jobs.triage.get_session", mock_get_session),
            patch("apps.worker.jobs.triage.get_llm_client") as mock_llm,
        ):
            from core.models.schemas import TriageOutput

            mock_client = MagicMock()
            mock_client.generate_triage.return_value = (
                TriageOutput(
                    summary_bullets=["Documentation issue", "Config docs are outdated"],
                    priority="P3",
                    priority_reason="Low severity documentation improvement.",
                    suggested_labels=[{"label": "documentation", "confidence": 0.90, "reason": "Docs issue"}],
                    questions=[],
                    repro_steps=None,
                    needs_more_info=False,
                ),
                {"model": "test-model", "tokens_in": 80, "tokens_out": 40},
            )
            mock_client.embed.return_value = [0.05] * 1536
            mock_llm.return_value = mock_client

            from apps.worker.jobs.triage import triage_issue

            triage_issue(str(issue.id), "delivery-test-002")

        assert issue.triage_result is not None
        assert issue.triage_result.priority == "P3"
        assert issue.triage_result.llm_model == "test-model"
        assert issue.triage_result.tokens_in == 80

    def test_triage_marks_failed_on_llm_error(self, integration_db, seeded_repo):
        """When LLM raises, issue should be marked as failed."""
        issue = Issue(
            repo_id=seeded_repo.id,
            github_issue_number=102,
            github_issue_id=9001003,
            title="Test failure case",
            body="Some body",
            author="tester",
            state="open",
            triage_status=TriageStatus.pending,
            last_delivery_id="delivery-fail-001",
        )
        integration_db.add(issue)
        delivery = WebhookDelivery(
            delivery_id="delivery-fail-001",
            event="issues",
            action="opened",
            payload={},
            status="enqueued",
        )
        integration_db.add(delivery)
        integration_db.flush()

        from contextlib import contextmanager

        @contextmanager
        def mock_get_session():
            try:
                yield integration_db
                integration_db.flush()  # mirror real get_session commit
            except Exception:
                raise

        with (
            patch("apps.worker.jobs.triage.get_session", mock_get_session),
            patch("apps.worker.jobs.triage.get_llm_client") as mock_llm,
        ):
            mock_client = MagicMock()
            mock_client.generate_triage.side_effect = RuntimeError("LLM API timeout")
            mock_llm.return_value = mock_client

            from apps.worker.jobs.triage import triage_issue

            with pytest.raises(RuntimeError):
                triage_issue(str(issue.id), "delivery-fail-001")

        # Python object is already updated by the exception handler
        assert issue.triage_status == TriageStatus.failed


class TestCommentMarkdownBuilder:
    """Tests for the Markdown comment builder."""

    def test_comment_contains_marker(self):
        from core.models.schemas import TriageOutput
        from apps.worker.jobs.triage import _build_comment_md

        output = TriageOutput(
            summary_bullets=["Crash on upload"],
            priority="P1",
            priority_reason="Production impact.",
            suggested_labels=[{"label": "bug", "confidence": 0.9, "reason": "Crash"}],
            questions=[],
            repro_steps=None,
            needs_more_info=False,
        )
        marker = "<!-- triage-copilot -->"
        md = _build_comment_md(
            triage=output,
            similar=[],
            model="test-model",
            issue_url="https://github.com/acme/myapp/issues/42",
            marker=marker,
        )
        assert marker in md

    def test_comment_contains_priority(self):
        from core.models.schemas import TriageOutput
        from apps.worker.jobs.triage import _build_comment_md

        output = TriageOutput(
            summary_bullets=["Critical crash"],
            priority="P0",
            priority_reason="Data loss risk.",
            suggested_labels=[],
            questions=[],
            repro_steps=None,
            needs_more_info=False,
        )
        md = _build_comment_md(
            triage=output,
            similar=[],
            model="test-model",
            issue_url="https://github.com/acme/myapp/issues/1",
            marker="<!-- marker -->",
        )
        assert "P0" in md
        assert "🔴" in md  # P0 emoji

    def test_comment_includes_similar_issues(self):
        from core.models.schemas import TriageOutput
        from apps.worker.jobs.triage import _build_comment_md

        output = TriageOutput(
            summary_bullets=["Bug report"],
            priority="P2",
            priority_reason="Normal severity.",
            suggested_labels=[],
            questions=[],
            repro_steps=None,
            needs_more_info=False,
        )
        similar = [
            {
                "issue_number": 10,
                "title": "Previous upload bug",
                "url": "https://github.com/acme/myapp/issues/10",
                "score": 0.92,
            }
        ]
        md = _build_comment_md(
            triage=output,
            similar=similar,
            model="test-model",
            issue_url="https://github.com/acme/myapp/issues/42",
            marker="<!-- marker -->",
        )
        assert "#10" in md
        assert "92% similar" in md

    def test_comment_shows_questions_when_needs_more_info(self):
        from core.models.schemas import TriageOutput
        from apps.worker.jobs.triage import _build_comment_md

        output = TriageOutput(
            summary_bullets=["Vague report"],
            priority="P2",
            priority_reason="Unknown severity.",
            suggested_labels=[],
            questions=["What version?", "Can you reproduce?"],
            repro_steps=None,
            needs_more_info=True,
        )
        md = _build_comment_md(
            triage=output,
            similar=[],
            model="test-model",
            issue_url="https://github.com/acme/myapp/issues/44",
            marker="<!-- marker -->",
        )
        assert "Questions for Author" in md
        assert "What version?" in md
