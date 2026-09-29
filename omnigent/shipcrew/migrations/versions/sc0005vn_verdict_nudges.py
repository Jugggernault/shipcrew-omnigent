"""verdict nudge: one automatic "end with PASS/FAIL" turn before blocking

Revision ID: sc0005vn
Revises: sc0004sh
Create Date: 2026-09-29 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0005vn"
down_revision: str | None = "sc0004sh"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``shipcrew_tasks.verdict_nudges`` (nudges sent since the last verdict)."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(
            sa.Column("verdict_nudges", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    """Drop ``shipcrew_tasks.verdict_nudges``."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.drop_column("verdict_nudges")
