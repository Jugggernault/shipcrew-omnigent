"""create shipcrew_missions and shipcrew_tasks

Revision ID: sc0001
Revises:
Create Date: 2026-09-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TASK_STATUSES = "'backlog', 'ready', 'running', 'review', 'intervention', 'merged', 'blocked'"


def upgrade() -> None:
    """Create both shipcrew tables."""
    op.create_table(
        "shipcrew_missions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("repo_path", sa.String(2048), nullable=False),
        sa.Column("repo_url", sa.String(2048), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="planning"),
        sa.Column("owner_user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planning', 'active', 'done')", name="ck_shipcrew_missions_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shipcrew_missions_created_at", "shipcrew_missions", ["created_at"])

    op.create_table(
        "shipcrew_tasks",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("mission_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("acceptance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="backlog"),
        sa.Column("assignee_kind", sa.String(8), nullable=True),
        sa.Column("assignee_id", sa.String(256), nullable=True),
        sa.Column("role", sa.String(128), nullable=False, server_default="developer"),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("owned_paths", sa.JSON(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(2048), nullable=True),
        sa.Column("ci", sa.String(8), nullable=False, server_default="none"),
        sa.Column("root_session_id", sa.String(64), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("session_seen_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(f"status IN ({_TASK_STATUSES})", name="ck_shipcrew_tasks_status"),
        sa.CheckConstraint(
            "ci IN ('none', 'pending', 'green', 'red')", name="ck_shipcrew_tasks_ci"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shipcrew_tasks_mission", "shipcrew_tasks", ["mission_id", "position"])
    op.create_index("ix_shipcrew_tasks_status", "shipcrew_tasks", ["status"])


def downgrade() -> None:
    """Drop both shipcrew tables."""
    op.drop_index("ix_shipcrew_tasks_status", table_name="shipcrew_tasks")
    op.drop_index("ix_shipcrew_tasks_mission", table_name="shipcrew_tasks")
    op.drop_table("shipcrew_tasks")
    op.drop_index("ix_shipcrew_missions_created_at", table_name="shipcrew_missions")
    op.drop_table("shipcrew_missions")
