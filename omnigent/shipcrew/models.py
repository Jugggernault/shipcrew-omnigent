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
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MISSION_STATUSES = ("planning", "active", "done")
TASK_STATUSES = ("backlog", "ready", "running", "review", "intervention", "merged", "blocked")
CI_STATES = ("none", "pending", "green", "red")
ASSIGNEE_KINDS = ("agent", "human")
PLAN_STATUSES = ("idle", "running", "imported", "failed")
SHIP_STATUSES = ("idle", "deploying", "verifying", "done", "failed")


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
    # A plan import moves its backlog tasks to ready right away (sc0003ar).
    auto_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # ── Ship stage (sc0004sh, see omnigent/shipcrew/ship.py) ──
    # The planner's "Decisions:" list.
    plan_decisions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # Deploy once every agent task is merged.
    auto_ship: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    ship_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="idle")
    ship_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    ship_report_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    ship_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Protected deployment (HTTP 401/403) and similar remarks of the URL check.
    ship_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    ship_started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ship_finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ship_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Branch of the throwaway worktree of main the devops session deploys from.
    ship_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The URL check gives up at this wall-clock time (restart-safe deadline).
    ship_verify_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    ship_decisions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    ship_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(_in_check("status", MISSION_STATUSES), name="ck_shipcrew_missions_status"),
        CheckConstraint(
            _in_check("plan_status", PLAN_STATUSES), name="ck_shipcrew_missions_plan_status"
        ),
        CheckConstraint(
            _in_check("ship_status", SHIP_STATUSES), name="ck_shipcrew_missions_ship_status"
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
    # ── PR loop (sc0002pr) ──
    # Git branch of the task, fixed at first start ("shipcrew/<id8>-<slug>").
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # CI fix turns sent to the developer so far.
    ci_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Latest reviewer verdict: {verdict, summary, findings}; verdict null = in progress.
    review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Head SHA the review (or the in-flight reviewer session) is for.
    review_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # CHANGES verdicts received so far.
    review_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reviewer_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    integrator_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_human_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    approval_reasons: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # POST /approve was pressed for the current changes; reset by a developer push.
    human_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # ── Ship stage (sc0004sh) ──
    # The agent's "Decisions:" lists, merged across its turns.
    decisions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # Wall-clock time of the first start (the report's wall time begins here).
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Every time the card entered intervention: [{"at": float, "reason": str}].
    interventions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # ── sc0005vn ──
    # Automatic "end with PASS/FAIL" turns sent since the agent's last verdict.
    verdict_nudges: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(_in_check("status", TASK_STATUSES), name="ck_shipcrew_tasks_status"),
        CheckConstraint(_in_check("ci", CI_STATES), name="ck_shipcrew_tasks_ci"),
        Index("ix_shipcrew_tasks_mission", "mission_id", "position"),
        Index("ix_shipcrew_tasks_status", "status"),
        Index("ix_shipcrew_tasks_plan_key", "mission_id", "plan_key"),
    )
