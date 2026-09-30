"""pushed sha: the PR head the loop last pushed or saw on the remote

Revision ID: sc0008ps
Revises: sc0007ap
Create Date: 2026-09-30 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0008ps"
down_revision: str | None = "sc0007ap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``shipcrew_tasks.pushed_sha`` (the ``--force-with-lease`` expectation)."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(sa.Column("pushed_sha", sa.String(64), nullable=True))


def downgrade() -> None:
    """Drop ``shipcrew_tasks.pushed_sha``."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.drop_column("pushed_sha")
