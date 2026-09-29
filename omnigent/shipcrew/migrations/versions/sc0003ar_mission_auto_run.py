"""mission auto_run: start the imported tasks right after a plan import

Revision ID: sc0003ar
Revises: sc0002p
Create Date: 2026-09-29 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0003ar"
down_revision: str | None = "sc0002p"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``shipcrew_missions.auto_run`` (off by default)."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.add_column(
            sa.Column("auto_run", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Drop ``shipcrew_missions.auto_run``."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.drop_column("auto_run")
