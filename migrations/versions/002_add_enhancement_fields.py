"""Add enhancement fields to triage_results.

Revision ID: 002
Revises: 001
Create Date: 2026-03-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "triage_results",
        sa.Column("issue_type", sa.Text(), nullable=True, server_default="other"),
    )
    op.add_column(
        "triage_results",
        sa.Column("issue_type_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "triage_results",
        sa.Column("problem_statement", sa.Text(), nullable=True),
    )
    op.add_column(
        "triage_results",
        sa.Column("acceptance_criteria", sa.JSON(), nullable=True),
    )
    op.add_column(
        "triage_results",
        sa.Column("proposed_solution", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("triage_results", "proposed_solution")
    op.drop_column("triage_results", "acceptance_criteria")
    op.drop_column("triage_results", "problem_statement")
    op.drop_column("triage_results", "issue_type_confidence")
    op.drop_column("triage_results", "issue_type")
