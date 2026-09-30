"""mission preview: the continuous deploy of main (docker / argocd targets)

Revision ID: sc0008pv
Revises: sc0007ap
Create Date: 2026-09-30 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0008pv"
down_revision: str | None = "sc0007ap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``shipcrew_missions.preview`` (JSON object)."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.add_column(sa.Column("preview", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop ``shipcrew_missions.preview``."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.drop_column("preview")
