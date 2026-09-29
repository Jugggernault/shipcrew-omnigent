"""approved paths: writes outside owned paths a human accepted during the build

Revision ID: sc0007ap
Revises: sc0005pj
Create Date: 2026-09-30 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0007ap"
down_revision: str | None = "sc0005pj"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``shipcrew_tasks.approved_paths`` (repo-relative paths, JSON list)."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(sa.Column("approved_paths", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop ``shipcrew_tasks.approved_paths``."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.drop_column("approved_paths")
