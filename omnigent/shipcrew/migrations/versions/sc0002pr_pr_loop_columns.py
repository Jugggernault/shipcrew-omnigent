"""PR loop columns on shipcrew_tasks

Revision ID: sc0002pr
Revises: sc0001
Create Date: 2026-09-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0002pr"
down_revision: str | None = "sc0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "human_approved",
    "approval_reasons",
    "needs_human_approval",
    "integrator_session_id",
    "reviewer_session_id",
    "review_rounds",
    "review_sha",
    "review",
    "ci_attempts",
    "branch",
)


def upgrade() -> None:
    """Add the branch / CI / review / approval state of the PR loop."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(sa.Column("branch", sa.String(256), nullable=True))
        batch.add_column(
            sa.Column("ci_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("review", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("review_sha", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("review_rounds", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("reviewer_session_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("integrator_session_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "needs_human_approval", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("approval_reasons", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("human_approved", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Drop the PR loop columns."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        for name in _COLUMNS:
            batch.drop_column(name)
