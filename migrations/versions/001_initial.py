"""Initial schema: repos, issues, triage_results, issue_embeddings, webhook_deliveries.

Revision ID: 001
Revises: —
Create Date: 2024-01-01 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enable pgvector extension ─────────────────────────────────────────────
    # Silently skip if pgvector is not installed (falls back to JSONB)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── repos ─────────────────────────────────────────────────────────────────
    op.create_table(
        "repos",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )
    op.create_index("ix_repos_github_installation_id", "repos", ["github_installation_id"])

    # ── issues ────────────────────────────────────────────────────────────────
    op.create_table(
        "issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_issue_number", sa.Integer(), nullable=False),
        sa.Column("github_issue_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("author", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at_github", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triage_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("last_delivery_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_id", "github_issue_id", name="uq_issue_repo_github_id"),
    )
    op.create_index("ix_issues_github_issue_number", "issues", ["github_issue_number"])
    op.create_index("ix_issues_last_delivery_id", "issues", ["last_delivery_id"])
    op.create_index("ix_issues_repo_status", "issues", ["repo_id", "triage_status"])

    # ── triage_results ────────────────────────────────────────────────────────
    op.create_table(
        "triage_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.Text(), nullable=False, server_default="P2"),
        sa.Column("priority_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "suggested_labels", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("questions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("repro_steps", postgresql.JSONB(), nullable=True),
        sa.Column("similar_issues", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id"),
    )

    # ── issue_embeddings ──────────────────────────────────────────────────────
    # Try to use pgvector type; fall back to JSONB if extension not available
    try:
        op.create_table(
            "issue_embeddings",
            sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "embedding",
                sa.Text(),  # Placeholder — overridden below
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("issue_id"),
        )
        # Alter column to use vector type if pgvector is available
        op.execute("ALTER TABLE issue_embeddings ALTER COLUMN embedding TYPE vector(1536) USING NULL")
    except Exception:
        # pgvector not available, use JSONB
        op.execute(
            "ALTER TABLE issue_embeddings ALTER COLUMN embedding TYPE jsonb USING NULL"
        )

    # Create HNSW index for fast vector search (if pgvector available)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
                CREATE INDEX IF NOT EXISTS ix_issue_embeddings_vector
                ON issue_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            END IF;
        END$$;
    """)

    # ── webhook_deliveries ────────────────────────────────────────────────────
    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "processed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="received"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "ix_webhook_deliveries_received_at", "webhook_deliveries", ["received_at"]
    )

    # ── updated_at trigger ────────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)
    for table in ("repos", "issues", "triage_results"):
        op.execute(f"""
            CREATE TRIGGER trigger_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();
        """)


def downgrade() -> None:
    for table in ("repos", "issues", "triage_results"):
        op.execute(f"DROP TRIGGER IF EXISTS trigger_{table}_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column()")
    op.drop_table("webhook_deliveries")
    op.drop_table("issue_embeddings")
    op.drop_table("triage_results")
    op.drop_table("issues")
    op.drop_table("repos")
