"""ship stage: deploy + report per mission, decisions per task and plan

Revision ID: sc0004sh
Revises: sc0003ar
Create Date: 2026-09-29 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sc0004sh"
down_revision: str | None = "sc0003ar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHIP_STATUSES = ("idle", "deploying", "verifying", "done", "failed")
_SHIP_CHECK = "ship_status IN ({})".format(", ".join(f"'{s}'" for s in _SHIP_STATUSES))

_MISSION_COLUMNS = (
    "plan_decisions",
    "auto_ship",
    "ship_status",
    "ship_url",
    "ship_report_md",
    "ship_error",
    "ship_note",
    "ship_started_at",
    "ship_finished_at",
    "ship_session_id",
    "ship_branch",
    "ship_verify_until",
    "ship_decisions",
    "ship_cost_usd",
)


def upgrade() -> None:
    """Add the ship columns to missions and the decisions / timing columns to tasks."""
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.add_column(sa.Column("plan_decisions", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("auto_ship", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("ship_status", sa.String(length=16), nullable=False, server_default="idle")
        )
        batch.add_column(sa.Column("ship_url", sa.String(length=2048), nullable=True))
        batch.add_column(sa.Column("ship_report_md", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ship_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ship_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ship_started_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("ship_finished_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("ship_session_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("ship_branch", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("ship_verify_until", sa.Float(), nullable=True))
        batch.add_column(sa.Column("ship_decisions", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("ship_cost_usd", sa.Float(), nullable=False, server_default="0")
        )
        batch.create_check_constraint("ck_shipcrew_missions_ship_status", _SHIP_CHECK)
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.add_column(sa.Column("decisions", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("interventions", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the ship stage columns."""
    with op.batch_alter_table("shipcrew_tasks") as batch:
        batch.drop_column("interventions")
        batch.drop_column("started_at")
        batch.drop_column("decisions")
    with op.batch_alter_table("shipcrew_missions") as batch:
        batch.drop_constraint("ck_shipcrew_missions_ship_status", type_="check")
        for name in reversed(_MISSION_COLUMNS):
            batch.drop_column(name)
