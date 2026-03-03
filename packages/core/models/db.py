"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Use JSONB on PostgreSQL; fall back to standard JSON for SQLite (tests)
_JSON = JSON().with_variant(JSONB(), "postgresql")
# Use PostgreSQL UUID on PG, CHAR(32) on SQLite
_UUID = Uuid(as_uuid=True)

# Try to import pgvector; fall back to _JSON storage
try:
    from pgvector.sqlalchemy import Vector as PgVector

    _PGVECTOR = True
except ImportError:
    PgVector = None  # type: ignore[assignment,misc]
    _PGVECTOR = False


class Base(DeclarativeBase):
    pass


# ── Enums (stored as plain text for Alembic portability) ────────────────────


class TriageStatus:
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"

    ALL = [pending, processing, done, failed]


class DeliveryStatus:
    received = "received"
    ignored = "ignored"
    enqueued = "enqueued"
    done = "done"
    failed = "failed"


# ── Models ───────────────────────────────────────────────────────────────────


class Repo(Base):
    """GitHub repository registered with the app."""

    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, default=uuid.uuid4
    )
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    full_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    issues: Mapped[list[Issue]] = relationship("Issue", back_populates="repo")

    def effective_config(self) -> dict[str, Any]:
        """Return config merged with defaults."""
        return {
            "allowed_labels": self.config.get(
                "allowed_labels",
                [
                    "bug",
                    "enhancement",
                    "documentation",
                    "question",
                    "duplicate",
                    "wontfix",
                    "help wanted",
                    "good first issue",
                    "security",
                    "performance",
                ],
            ),
            "auto_apply_labels": self.config.get("auto_apply_labels", False),
            "label_confidence_threshold": self.config.get(
                "label_confidence_threshold", 0.75
            ),
            "similarity_threshold": self.config.get("similarity_threshold", 0.80),
        }


class Issue(Base):
    """Mirrored GitHub Issue."""

    __tablename__ = "issues"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    github_issue_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at_github: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triage_status: Mapped[str] = mapped_column(
        Text, nullable=False, default=TriageStatus.pending
    )
    last_delivery_id: Mapped[str | None] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repo: Mapped[Repo] = relationship("Repo", back_populates="issues")
    triage_result: Mapped[TriageResult | None] = relationship(
        "TriageResult", back_populates="issue", uselist=False, cascade="all, delete-orphan"
    )
    embedding: Mapped[IssueEmbedding | None] = relationship(
        "IssueEmbedding", back_populates="issue", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "github_issue_id", name="uq_issue_repo_github_id"),
        Index("ix_issues_repo_status", "repo_id", "triage_status"),
    )

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.repo.full_name}/issues/{self.github_issue_number}"


class TriageResult(Base):
    """LLM-generated triage analysis for an issue."""

    __tablename__ = "triage_results"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, default=uuid.uuid4
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    summary_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[str] = mapped_column(Text, nullable=False, default="P2")
    priority_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_labels: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON, nullable=False, default=list
    )
    questions: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    repro_steps: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    similar_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON, nullable=False, default=list
    )
    issue_type: Mapped[str | None] = mapped_column(Text, nullable=True, default="other")
    issue_type_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    proposed_solution: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    issue: Mapped[Issue] = relationship("Issue", back_populates="triage_result")


class IssueEmbedding(Base):
    """Vector embedding for semantic similarity search."""

    __tablename__ = "issue_embeddings"

    issue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True
    )
    # Store embeddings as JSON in the ORM (portable across SQLite/PostgreSQL).
    # The migration applies a real vector(1536) column type on PostgreSQL.
    # pgvector queries in embedding_service.py use raw SQL for similarity search.
    embedding: Mapped[list[float] | None] = mapped_column(_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    issue: Mapped[Issue] = relationship("Issue", back_populates="embedding")


class WebhookDelivery(Base):
    """Idempotency log for GitHub webhook deliveries."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=DeliveryStatus.received
    )
    error: Mapped[str | None] = mapped_column(Text)
