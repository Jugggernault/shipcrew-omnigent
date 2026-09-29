"""planner import + GitHub issue sync columns

Revision ID: sc0002p
Revises: sc0001
Create Date: 2026-09-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0002p"
down_revision: str | None = "sc0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the mission plan state and the task plan key / issue URL."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.add_column(
            sa.Column("plan_status", sa.String(16), nullable=False, server_default="idle")
        )
        batch.add_column(sa.Column("plan_session_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("plan_error", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("plan_imported_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("plan_started_at", sa.Float(), nullable=True))
        batch.create_check_constraint(
            "ck_shipcrew_missions_plan_status",
            "plan_status IN ('idle', 'running', 'imported', 'failed')",
        )
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(sa.Column("issue_url", sa.String(2048), nullable=True))
        batch.add_column(sa.Column("plan_key", sa.String(128), nullable=True))
    op.create_index("ix_shipcrew_tasks_plan_key", "shipcrew_tasks", ["mission_id", "plan_key"])


def downgrade() -> None:
    """Drop the plan and issue sync columns."""
    op.drop_index("ix_shipcrew_tasks_plan_key", table_name="shipcrew_tasks")
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.drop_column("plan_key")
        batch.drop_column("issue_url")
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.drop_constraint("ck_shipcrew_missions_plan_status", type_="check")
        batch.drop_column("plan_started_at")
        batch.drop_column("plan_imported_count")
        batch.drop_column("plan_error")
        batch.drop_column("plan_session_id")
        batch.drop_column("plan_status")
