"""mission project: the omnigent project that groups a mission's sessions

Revision ID: sc0005pj
Revises: sc0004sh
Create Date: 2026-09-29 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0005pj"
down_revision: str | None = "sc0004sh"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``project_id`` (an omnigent ``projects.id``, no FK) to missions."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.add_column(sa.Column("project_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Drop the mission's project link."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.drop_column("project_id")
