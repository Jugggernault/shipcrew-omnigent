"""SQLAlchemy tables for shipcrew missions and tasks.

Kept on their own declarative base and their own Alembic lineage (see
``omnigent/shipcrew/migrations``) so the fork never touches omnigent's schema
or migration chain. No DB foreign keys, matching omnigent's schema rule.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MISSION_STATUSES = ("planning", "active", "done")
TASK_STATUSES = ("backlog", "ready", "running", "review", "intervention", "merged", "blocked")
CI_STATES = ("none", "pending", "green", "red")
ASSIGNEE_KINDS = ("agent", "human")
PLAN_STATUSES = ("idle", "running", "imported", "failed")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class ShipcrewBase(DeclarativeBase):
    """Declarative base for the ``shipcrew_*`` tables."""


class SqlMission(ShipcrewBase):
    """A mission: one repository and the task DAG that ships it."""

    __tablename__ = "shipcrew_missions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    repo_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="planning")
    # Identity that created the mission; background starts act as this user.
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # Planner import (see omnigent/shipcrew/planner.py).
    plan_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="idle")
    plan_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_imported_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Wall-clock start of the planner run: a plan.json older than this is stale.
    plan_started_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint(_in_check("status", MISSION_STATUSES), name="ck_shipcrew_missions_status"),
        CheckConstraint(
            _in_check("plan_status", PLAN_STATUSES), name="ck_shipcrew_missions_plan_status"
        ),
        Index("ix_shipcrew_missions_created_at", "created_at"),
    )


class SqlTask(ShipcrewBase):
    """A task card: one issue, one branch, one root omnigent session."""

    __tablename__ = "shipcrew_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    acceptance: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="backlog")
    assignee_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(128), nullable=False, server_default="developer")
    depends_on: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    owned_paths: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # The plan.json ``key`` this task was imported from; re-imports match on it.
    plan_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    ci: Mapped[str] = mapped_column(String(8), nullable=False, server_default="none")
    root_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    position: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set once the root session was observed working, so an idle session only
    # maps to "review" after the agent actually ran.
    session_seen_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_check("status", TASK_STATUSES), name="ck_shipcrew_tasks_status"),
        CheckConstraint(_in_check("ci", CI_STATES), name="ck_shipcrew_tasks_ci"),
        Index("ix_shipcrew_tasks_mission", "mission_id", "position"),
        Index("ix_shipcrew_tasks_status", "status"),
        Index("ix_shipcrew_tasks_plan_key", "mission_id", "plan_key"),
    )
