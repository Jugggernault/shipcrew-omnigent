"""Persistence for shipcrew missions and tasks."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from omnigent.db.utils import make_named_managed_session_maker
from omnigent.shipcrew.migrate import ensure_schema
from omnigent.shipcrew.models import SqlMission, SqlTask

ACTIVE_STATUSES = frozenset({"running", "intervention"})


@dataclass(frozen=True)
class Assignee:
    """Who owns a task: an agent (by role/bundle) or a human (by login)."""

    kind: str
    id: str


@dataclass(frozen=True)
class Mission:
    id: str
    title: str
    repo_path: str
    repo_url: str | None
    status: str
    created_at: int
    owner_user_id: str | None = None
    plan_status: str = "idle"
    plan_session_id: str | None = None
    plan_error: str | None = None
    plan_imported_count: int = 0
    plan_started_at: float | None = None
    auto_run: bool = False

    def to_api(self) -> dict[str, Any]:
        """Serialize to the shared API contract shape."""
        return {
            "id": self.id,
            "title": self.title,
            "repo_path": self.repo_path,
            "repo_url": self.repo_url,
            "status": self.status,
            "created_at": self.created_at,
            "plan": {
                "status": self.plan_status,
                "session_id": self.plan_session_id,
                "error": self.plan_error,
                "imported_count": self.plan_imported_count,
            },
            "auto_run": self.auto_run,
        }


@dataclass(frozen=True)
class Task:
    id: str
    mission_id: str
    title: str
    body: str = ""
    acceptance: list[str] = field(default_factory=list)
    status: str = "backlog"
    assignee: Assignee | None = None
    role: str = "developer"
    depends_on: list[str] = field(default_factory=list)
    owned_paths: list[str] = field(default_factory=list)
    issue_number: int | None = None
    issue_url: str | None = None
    plan_key: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    ci: str = "none"
    root_session_id: str | None = None
    cost_usd: float = 0.0
    position: float = 0.0
    blocked_reason: str | None = None
    session_seen_active: bool = False
    created_at: int = 0
    updated_at: int = 0
    branch: str | None = None
    ci_attempts: int = 0
    review: dict[str, Any] | None = None
    review_sha: str | None = None
    review_rounds: int = 0
    reviewer_session_id: str | None = None
    integrator_session_id: str | None = None
    needs_human_approval: bool = False
    approval_reasons: list[str] = field(default_factory=list)
    human_approved: bool = False

    @property
    def human_assigned(self) -> bool:
        return self.assignee is not None and self.assignee.kind == "human"

    def to_api(self) -> dict[str, Any]:
        """Serialize to the shared API contract shape."""
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "title": self.title,
            "body": self.body,
            "acceptance": list(self.acceptance),
            "status": self.status,
            "assignee": (
                {"kind": self.assignee.kind, "id": self.assignee.id} if self.assignee else None
            ),
            "role": self.role,
            "depends_on": list(self.depends_on),
            "owned_paths": list(self.owned_paths),
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "ci": self.ci,
            "root_session_id": self.root_session_id,
            "cost_usd": self.cost_usd,
            "position": self.position,
            "blocked_reason": self.blocked_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "branch": self.branch,
            "ci_attempts": self.ci_attempts,
            "review": _review_api(self.review),
            "needs_human_approval": self.needs_human_approval,
            "approval_reasons": list(self.approval_reasons),
        }


def _review_api(review: dict[str, Any] | None) -> dict[str, Any] | None:
    """The contract shape of a stored review: ``{verdict, summary, findings}``."""
    if review is None:
        return None
    return {
        "verdict": review.get("verdict"),
        "summary": str(review.get("summary") or ""),
        "findings": [dict(f) for f in review.get("findings") or [] if isinstance(f, dict)],
    }


def _mission(row: SqlMission) -> Mission:
    return Mission(
        id=row.id,
        title=row.title,
        repo_path=row.repo_path,
        repo_url=row.repo_url,
        status=row.status,
        created_at=row.created_at,
        owner_user_id=row.owner_user_id,
        plan_status=row.plan_status or "idle",
        plan_session_id=row.plan_session_id,
        plan_error=row.plan_error,
        plan_imported_count=int(row.plan_imported_count or 0),
        plan_started_at=row.plan_started_at,
        auto_run=bool(row.auto_run),
    )


def _task(row: SqlTask) -> Task:
    assignee = (
        Assignee(kind=row.assignee_kind, id=row.assignee_id or "")
        if row.assignee_kind is not None
        else None
    )
    return Task(
        id=row.id,
        mission_id=row.mission_id,
        title=row.title,
        body=row.body or "",
        acceptance=[str(a) for a in row.acceptance or []],
        status=row.status,
        assignee=assignee,
        role=row.role,
        depends_on=[str(d) for d in row.depends_on or []],
        owned_paths=[str(p) for p in row.owned_paths or []],
        issue_number=row.issue_number,
        issue_url=row.issue_url,
        plan_key=row.plan_key,
        pr_number=row.pr_number,
        pr_url=row.pr_url,
        ci=row.ci,
        root_session_id=row.root_session_id,
        cost_usd=float(row.cost_usd or 0.0),
        position=float(row.position or 0.0),
        blocked_reason=row.blocked_reason,
        session_seen_active=bool(row.session_seen_active),
        created_at=row.created_at,
        updated_at=row.updated_at,
        branch=row.branch,
        ci_attempts=int(row.ci_attempts or 0),
        review=dict(row.review) if isinstance(row.review, dict) else None,
        review_sha=row.review_sha,
        review_rounds=int(row.review_rounds or 0),
        reviewer_session_id=row.reviewer_session_id,
        integrator_session_id=row.integrator_session_id,
        needs_human_approval=bool(row.needs_human_approval),
        approval_reasons=[str(r) for r in row.approval_reasons or []],
        human_approved=bool(row.human_approved),
    )


_TASK_FIELDS = frozenset(
    {
        "title",
        "body",
        "acceptance",
        "status",
        "role",
        "depends_on",
        "owned_paths",
        "issue_number",
        "issue_url",
        "plan_key",
        "pr_number",
        "pr_url",
        "ci",
        "root_session_id",
        "cost_usd",
        "position",
        "blocked_reason",
        "session_seen_active",
        "branch",
        "ci_attempts",
        "review",
        "review_sha",
        "review_rounds",
        "reviewer_session_id",
        "integrator_session_id",
        "needs_human_approval",
        "approval_reasons",
        "human_approved",
    }
)
_MISSION_FIELDS = frozenset(
    {
        "status",
        "plan_status",
        "plan_session_id",
        "plan_error",
        "plan_imported_count",
        "plan_started_at",
        "auto_run",
    }
)
_PLAN_TASK_FIELDS = ("title", "body", "acceptance", "role", "owned_paths")
_PLAN_EDITABLE_STATUSES = frozenset({"backlog", "ready"})
_UNSET: Any = object()


@dataclass(frozen=True)
class PlanTaskSpec:
    """One ``plan.json`` task, already validated (see ``omnigent.shipcrew.planner``).

    :param depends_on: Plan keys, resolved to task ids on import.
    """

    key: str
    title: str
    body: str
    acceptance: list[str]
    role: str
    depends_on: list[str]
    owned_paths: list[str]


class ShipcrewStore:
    """SQLAlchemy store for the ``shipcrew_*`` tables.

    :param engine: Engine bound to the database holding the tables. The shipcrew
        Alembic lineage is applied on construction.
    """

    def __init__(self, engine: Engine) -> None:
        ensure_schema(engine)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.shipcrew_store"
        )

    # ── Missions ────────────────────────────────────────────────

    def create_mission(
        self,
        title: str,
        repo_path: str,
        repo_url: str | None = None,
        *,
        owner_user_id: str | None = None,
    ) -> Mission:
        row = SqlMission(
            id=uuid.uuid4().hex,
            title=title,
            repo_path=repo_path,
            repo_url=repo_url,
            status="planning",
            owner_user_id=owner_user_id,
            created_at=int(time.time()),
            auto_run=False,
        )
        with self._session("create_mission") as session:
            session.add(row)
        return _mission(row)

    def list_missions(self) -> list[Mission]:
        with self._session("list_missions") as session:
            rows = session.scalars(
                select(SqlMission).order_by(SqlMission.created_at.desc(), SqlMission.id)
            ).all()
            return [_mission(r) for r in rows]

    def get_mission(self, mission_id: str) -> Mission | None:
        with self._session("get_mission") as session:
            row = session.get(SqlMission, mission_id)
            return _mission(row) if row is not None else None

    def set_mission_status(self, mission_id: str, status: str) -> None:
        with self._session("set_mission_status") as session:
            row = session.get(SqlMission, mission_id)
            if row is not None:
                row.status = status

    def update_mission(self, mission_id: str, **fields: Any) -> Mission | None:
        """Apply ``fields`` (column names) to a mission.

        :returns: The updated mission, or ``None`` when it does not exist.
        :raises ValueError: On an unknown field name.
        """
        unknown = set(fields) - _MISSION_FIELDS
        if unknown:
            raise ValueError(f"unknown mission fields: {sorted(unknown)}")
        with self._session("update_mission") as session:
            row = session.get(SqlMission, mission_id)
            if row is None:
                return None
            for name, value in fields.items():
                setattr(row, name, value)
            return _mission(row)

    def upsert_plan_tasks(self, mission_id: str, specs: list[PlanTaskSpec]) -> list[Task]:
        """Create or update one task per plan key, in one transaction.

        A key already imported into this mission updates that task's content
        while it is still ``backlog`` / ``ready`` (never its status, session or
        board position: a started task keeps the contract its agents and
        reviewer work to); a new key becomes a ``backlog`` task. ``depends_on``
        keys are mapped to task ids. Tasks whose key left the plan are kept as
        they are.

        :returns: The imported tasks, in plan order.
        """
        now = int(time.time())
        with self._session("upsert_plan_tasks") as session:
            existing = {
                row.plan_key: row
                for row in session.scalars(
                    select(SqlTask).where(
                        SqlTask.mission_id == mission_id, SqlTask.plan_key.is_not(None)
                    )
                ).all()
            }
            last = session.scalar(
                select(func.max(SqlTask.position)).where(SqlTask.mission_id == mission_id)
            )
            position = float(last) + 1.0 if last is not None else 0.0
            rows: dict[str, SqlTask] = {}
            for spec in specs:
                row = existing.get(spec.key)
                if row is None:
                    row = SqlTask(
                        id=uuid.uuid4().hex,
                        mission_id=mission_id,
                        plan_key=spec.key,
                        status="backlog",
                        depends_on=[],
                        ci="none",
                        cost_usd=0.0,
                        position=position,
                        session_seen_active=False,
                        created_at=now,
                    )
                    position += 1.0
                    session.add(row)
                rows[spec.key] = row
                if row.status not in _PLAN_EDITABLE_STATUSES:
                    continue
                for name in _PLAN_TASK_FIELDS:
                    value = getattr(spec, name)
                    setattr(row, name, list(value) if isinstance(value, list) else value)
                row.updated_at = now
            for spec in specs:
                row = rows[spec.key]
                if row.status in _PLAN_EDITABLE_STATUSES:
                    row.depends_on = [rows[k].id for k in spec.depends_on]
            return [_task(rows[spec.key]) for spec in specs]

    def ready_backlog(self, mission_id: str, task_ids: set[str] | None = None) -> list[Task]:
        """Move the mission's backlog tasks to ``ready``, in one transaction.

        Human-assigned tasks stay in the backlog. Moving a task to ready only
        queues it: the scheduler's gates (dependencies, capacity, owned paths,
        budget) decide what starts.

        :param task_ids: Only these tasks (e.g. the ones a plan import just
            wrote); ``None`` means every backlog task of the mission.
        :returns: The tasks that moved, in board order (empty when none did).
        """
        stmt = (
            select(SqlTask)
            .where(SqlTask.mission_id == mission_id, SqlTask.status == "backlog")
            .order_by(SqlTask.position, SqlTask.created_at, SqlTask.id)
        )
        now = int(time.time())
        with self._session("ready_backlog") as session:
            moved: list[Task] = []
            for row in session.scalars(stmt).all():
                if row.assignee_kind == "human":
                    continue
                if task_ids is not None and row.id not in task_ids:
                    continue
                row.status = "ready"
                row.blocked_reason = None
                row.updated_at = now
                moved.append(_task(row))
            return moved

    # ── Tasks ───────────────────────────────────────────────────

    def create_task(
        self,
        mission_id: str,
        title: str,
        *,
        body: str = "",
        acceptance: list[str] | None = None,
        role: str = "developer",
        depends_on: list[str] | None = None,
        owned_paths: list[str] | None = None,
    ) -> Task:
        now = int(time.time())
        with self._session("create_task") as session:
            last = session.scalar(
                select(func.max(SqlTask.position)).where(SqlTask.mission_id == mission_id)
            )
            row = SqlTask(
                id=uuid.uuid4().hex,
                mission_id=mission_id,
                title=title,
                body=body,
                acceptance=list(acceptance or []),
                status="backlog",
                role=role,
                depends_on=list(depends_on or []),
                owned_paths=list(owned_paths or []),
                ci="none",
                ci_attempts=0,
                review_rounds=0,
                needs_human_approval=False,
                human_approved=False,
                cost_usd=0.0,
                position=(float(last) + 1.0) if last is not None else 0.0,
                session_seen_active=False,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        return _task(row)

    def get_task(self, task_id: str) -> Task | None:
        with self._session("get_task") as session:
            row = session.get(SqlTask, task_id)
            return _task(row) if row is not None else None

    def list_tasks(self, mission_id: str | None = None) -> list[Task]:
        """Tasks of one mission (or of all missions), ordered by board position."""
        stmt = select(SqlTask).order_by(SqlTask.position, SqlTask.created_at, SqlTask.id)
        if mission_id is not None:
            stmt = stmt.where(SqlTask.mission_id == mission_id)
        with self._session("list_tasks") as session:
            return [_task(r) for r in session.scalars(stmt).all()]

    def list_tasks_by_status(self, statuses: frozenset[str] | set[str]) -> list[Task]:
        stmt = (
            select(SqlTask)
            .where(SqlTask.status.in_(sorted(statuses)))
            .order_by(SqlTask.position, SqlTask.created_at, SqlTask.id)
        )
        with self._session("list_tasks_by_status") as session:
            return [_task(r) for r in session.scalars(stmt).all()]

    def update_task(
        self,
        task_id: str,
        *,
        assignee: Assignee | None = _UNSET,
        **fields: Any,
    ) -> Task | None:
        """Apply ``fields`` (column names) and optionally ``assignee``.

        :returns: The updated task, or ``None`` when it does not exist.
        :raises ValueError: On an unknown field name.
        """
        unknown = set(fields) - _TASK_FIELDS
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        with self._session("update_task") as session:
            row = session.get(SqlTask, task_id)
            if row is None:
                return None
            for name, value in fields.items():
                setattr(row, name, list(value) if isinstance(value, list | tuple) else value)
            if assignee is not _UNSET:
                row.assignee_kind = assignee.kind if assignee is not None else None
                row.assignee_id = assignee.id if assignee is not None else None
            row.updated_at = int(time.time())
            return _task(row)
