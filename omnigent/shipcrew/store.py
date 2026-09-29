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

    def to_api(self) -> dict[str, Any]:
        """Serialize to the shared API contract shape."""
        return {
            "id": self.id,
            "title": self.title,
            "repo_path": self.repo_path,
            "repo_url": self.repo_url,
            "status": self.status,
            "created_at": self.created_at,
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
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "ci": self.ci,
            "root_session_id": self.root_session_id,
            "cost_usd": self.cost_usd,
            "position": self.position,
            "blocked_reason": self.blocked_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
        "pr_number",
        "pr_url",
        "ci",
        "root_session_id",
        "cost_usd",
        "position",
        "blocked_reason",
        "session_seen_active",
    }
)
_UNSET: Any = object()


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
