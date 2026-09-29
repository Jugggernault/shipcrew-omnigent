"""Planner import: PRD -> planner session -> ``.shipcrew/plan.json`` -> board tasks.

``POST /missions/{id}/plan`` starts a session of the ``planner`` role bundle in
the mission repository itself (no worktree: the planner writes only
``.shipcrew/plan.json``). Each scheduler tick polls that session; once it has
answered and gone idle, the plan file is validated and imported as ``backlog``
tasks. Plan keys are stored on the tasks, so importing again updates the same
cards instead of duplicating them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.shipcrew.decisions import parse_decisions
from omnigent.shipcrew.sessions import RootSessionRequest, SessionServiceError, SessionSnapshot
from omnigent.shipcrew.store import Mission, PlanTaskSpec

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)

PLANNER_ROLE = "planner"
PLAN_FILE = Path(".shipcrew") / "plan.json"
PRD_FILE = Path(".shipcrew") / "prd.md"
MISSION_LABEL_KEY = "shipcrew.mission_id"
# A plan.json last written this long before the planner started is stale.
_STALE_SLACK_S = 1.0
_MAX_ERRORS_SHOWN = 5
# The task roles of agents/planner/ROLE.md. The PRD is untrusted input to the
# planner, so a plan may not route work to the orchestrator, planner or
# reviewer bundles.
PLAN_ROLES = frozenset(
    {"designer", "scaffolder", "developer", "integrator", "qa", "security", "devops"}
)
MAX_PLAN_TASKS = 200
_MAX_OWNED = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _check_owned_path(glob: str) -> str:
    """A repo-relative owned glob: no absolute path, ``..``, backslash or control char."""
    glob = glob.strip()
    if not glob or len(glob) > 512:
        raise ValueError("owned path must be 1 to 512 characters")
    if _CONTROL.search(glob) or "\\" in glob:
        raise ValueError(f"owned path {glob!r} has a control character or a backslash")
    if glob.startswith(("/", "~")):
        raise ValueError(f"owned path {glob!r} must be relative to the repository")
    if ".." in glob.split("/"):
        raise ValueError(f"owned path {glob!r} must not contain '..'")
    return glob


class PlanError(ValueError):
    """``plan.json`` is missing or invalid; the message goes to ``mission.plan.error``."""


class PlanTaskModel(BaseModel):
    """One entry of ``plan.json``'s ``tasks`` (schema: ``agents/planner/ROLE.md``)."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=50_000)
    acceptance: list[str] = Field(default_factory=list, max_length=50)
    role: str = "developer"
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_PLAN_TASKS)
    owned_paths: list[str] = Field(default_factory=list, max_length=_MAX_OWNED)

    @field_validator("key", "title")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("role")
    @classmethod
    def _bundle_name(cls, value: str) -> str:
        # The role names a directory under SHIPCREW_AGENTS_DIR: a known task role only.
        if value not in PLAN_ROLES:
            raise ValueError(f"must be one of {', '.join(sorted(PLAN_ROLES))}")
        return value

    @field_validator("acceptance")
    @classmethod
    def _acceptance(cls, value: list[str]) -> list[str]:
        if any(len(a) > 2000 for a in value):
            raise ValueError("an acceptance criterion is longer than 2000 characters")
        return value

    @field_validator("owned_paths")
    @classmethod
    def _owned(cls, value: list[str]) -> list[str]:
        return [_check_owned_path(p) for p in value]


class PlanModel(BaseModel):
    """The part of ``plan.json`` the board ingests; other keys are kept by the file."""

    model_config = ConfigDict(extra="ignore")

    tasks: list[PlanTaskModel] = Field(min_length=1, max_length=MAX_PLAN_TASKS)


def _find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    """A dependency cycle as ``[k1, k2, ..., k1]``, or ``None`` when acyclic."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(key: str) -> list[str] | None:
        state[key] = 1
        path.append(key)
        for dep in deps[key]:
            if state.get(dep) == 1:
                return [*path[path.index(dep) :], dep]
            if dep not in state:
                found = visit(dep)
                if found is not None:
                    return found
        path.pop()
        state[key] = 2
        return None

    for key in deps:
        if key not in state:
            found = visit(key)
            if found is not None:
                return found
    return None


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:_MAX_ERRORS_SHOWN]:
        where = ".".join(str(p) for p in err["loc"]) or "plan"
        parts.append(f"{where}: {err['msg']}")
    more = len(exc.errors()) - _MAX_ERRORS_SHOWN
    if more > 0:
        parts.append(f"(+{more} more)")
    return "; ".join(parts)


def parse_plan(text: str) -> list[PlanTaskSpec]:
    """Validate ``plan.json`` content and return its tasks in file order.

    :raises PlanError: On invalid JSON or schema, duplicate keys, a dependency
        on an unknown key, or a dependency cycle.
    """
    try:
        raw: Any = json.loads(text)
    except ValueError as exc:
        raise PlanError(f"plan.json is not valid JSON: {exc}") from exc
    try:
        plan = PlanModel.model_validate(raw)
    except ValidationError as exc:
        raise PlanError(f"plan.json does not match the schema: {_format_validation(exc)}") from exc
    keys = [t.key for t in plan.tasks]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise PlanError(f"plan.json has duplicate task keys: {', '.join(duplicates)}")
    known = set(keys)
    deps: dict[str, list[str]] = {}
    for task in plan.tasks:
        unknown = [d for d in task.depends_on if d not in known]
        if unknown:
            raise PlanError(
                f"task {task.key} depends on unknown keys: {', '.join(sorted(set(unknown)))}"
            )
        deps[task.key] = list(dict.fromkeys(task.depends_on))
    cycle = _find_cycle(deps)
    if cycle is not None:
        raise PlanError(f"plan.json has a dependency cycle: {' -> '.join(cycle)}")
    return [
        PlanTaskSpec(
            key=t.key,
            title=t.title,
            body=t.body,
            acceptance=[a for a in t.acceptance if a.strip()],
            role=t.role,
            depends_on=deps[t.key],
            owned_paths=list(t.owned_paths),
        )
        for t in plan.tasks
    ]


def read_plan_file(repo_path: str, started_at: float | None) -> str:
    """Content of the mission repo's ``plan.json``.

    :param started_at: When the planner run began; an older file is stale.
    :raises PlanError: When the file is missing, stale or unreadable.
    """
    path = Path(repo_path) / PLAN_FILE
    try:
        stat = path.stat()
        if started_at is not None and stat.st_mtime + _STALE_SLACK_S < started_at:
            raise PlanError(f"the planner did not write {PLAN_FILE} (the file predates its run)")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlanError(f"the planner did not write {PLAN_FILE}") from exc
    except OSError as exc:
        raise PlanError(f"cannot read {PLAN_FILE}: {exc}") from exc


def read_prd_file(repo_path: str) -> str | None:
    """The repo's ``.shipcrew/prd.md``, or ``None`` when absent or empty."""
    try:
        text = (Path(repo_path) / PRD_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def build_planner_prompt(mission: Mission, prd: str) -> str:
    """The planner session's first message."""
    return "\n".join(
        [
            f"# Plan the mission: {mission.title}",
            "",
            "Turn the PRD below into the mission plan. Follow your role instructions and "
            f"write the plan to `{PLAN_FILE.as_posix()}` (the only file you write). The "
            "board imports its `tasks` as cards: `key`s must be unique and `depends_on` "
            "may only list keys of this plan, with no cycles.",
            "",
            "## PRD",
            "",
            prd.strip(),
        ]
    )


class PlanRunner:
    """Starts planner sessions and imports their ``plan.json`` into the board."""

    def __init__(self, service: ShipcrewService) -> None:
        self._service = service
        self._busy: set[str] = set()

    async def _set(self, mission_id: str, **fields: Any) -> Mission:
        mission = await asyncio.to_thread(self._service.store.update_mission, mission_id, **fields)
        if mission is None:
            raise OmnigentError(f"mission {mission_id!r} not found", code=ErrorCode.NOT_FOUND)
        self._service.bus.mission_updated(mission)
        return mission

    async def _fail(self, mission: Mission, error: str) -> Mission:
        return await self._set(mission.id, plan_status="failed", plan_error=error)

    async def start(self, mission_id: str, prd: str | None, acting_user: str | None) -> Mission:
        """Start a planner session for the mission (``mission.plan.status = running``).

        :param prd: The PRD text; ``None`` or blank reads the repo's ``.shipcrew/prd.md``.
        :raises OmnigentError: ``CONFLICT`` while a plan runs, ``INVALID_INPUT``
            when there is no PRD.
        """
        # Claim before the first await: two clicks must not start two planners.
        if mission_id in self._busy:
            raise OmnigentError("a plan is already starting", code=ErrorCode.CONFLICT)
        self._busy.add(mission_id)
        try:
            mission = await self._service.require_mission(mission_id)
            if mission.plan_status == "running":
                raise OmnigentError("a plan is already running", code=ErrorCode.CONFLICT)
            text = prd if prd and prd.strip() else None
            if text is None:
                text = await asyncio.to_thread(read_prd_file, mission.repo_path)
            if text is None:
                raise OmnigentError(
                    f"no PRD: send one in the request or add {PRD_FILE.as_posix()} to the repo",
                    code=ErrorCode.INVALID_INPUT,
                )
            agent_dir = self._service.settings.agents_dir / PLANNER_ROLE
            mission = await self._set(
                mission.id,
                plan_status="running",
                plan_session_id=None,
                plan_error=None,
                plan_started_at=time.time(),
            )
            if not (agent_dir / "config.yaml").is_file():
                return await self._fail(
                    mission,
                    f"no agent bundle for role {PLANNER_ROLE!r} in "
                    f"{self._service.settings.agents_dir}",
                )
            request = RootSessionRequest(
                task_id=f"plan-{mission.id}",
                title=f"Plan: {mission.title}",
                prompt=build_planner_prompt(mission, text),
                repo_path=mission.repo_path,
                branch="",
                agent_dir=agent_dir,
                acting_user=acting_user or mission.owner_user_id,
                labels={MISSION_LABEL_KEY: mission.id, "shipcrew.role": PLANNER_ROLE},
                workspace=mission.repo_path,
            )
            try:
                session_id = await self._service.sessions.create_root_session(request)
            except Exception as exc:
                if not isinstance(exc, SessionServiceError):
                    _logger.exception("shipcrew: planner start failed for mission %s", mission.id)
                return await self._fail(mission, str(exc) or type(exc).__name__)
            return await self._set(mission.id, plan_session_id=session_id)
        finally:
            self._busy.discard(mission_id)

    async def import_plan(self, mission_id: str) -> Mission:
        """Validate the repo's ``plan.json`` and upsert its tasks (idempotent).

        On a bad plan the mission ends ``failed`` with the reason in
        ``plan.error`` and no task is touched.
        """
        mission = await self._service.require_mission(mission_id)
        try:
            text = await asyncio.to_thread(
                read_plan_file, mission.repo_path, mission.plan_started_at
            )
            specs = parse_plan(text)
        except PlanError as exc:
            return await self._fail(mission, str(exc))
        tasks = await asyncio.to_thread(self._service.store.upsert_plan_tasks, mission.id, specs)
        for task in tasks:
            self._service.bus.task_updated(task)
        mission = await self._set(
            mission.id, plan_status="imported", plan_error=None, plan_imported_count=len(tasks)
        )
        if mission.auto_run:
            # Same as "Run all tasks", for this import's tasks only; the
            # scheduler tick that imported the plan starts them next.
            await self._service.start_all(mission.id, {t.id for t in tasks})
        return mission

    async def _record_decisions(self, mission: Mission) -> None:
        """The planner's ``Decisions:`` list -> ``mission.plan_decisions`` (best effort)."""
        if mission.plan_session_id is None:
            return
        try:
            text = await self._service.sessions.last_agent_text(
                mission.plan_session_id, acting_user=mission.owner_user_id
            )
        except SessionServiceError as exc:
            _logger.warning("shipcrew: planner reply read failed for %s: %s", mission.id, exc)
            return
        await asyncio.to_thread(
            self._service.store.update_mission, mission.id, plan_decisions=parse_decisions(text)
        )

    async def _finish(self, mission: Mission, snap: SessionSnapshot | None) -> None:
        if snap is None:
            await self._fail(mission, "planner session no longer exists")
            return
        if snap.status == "failed":
            await self._fail(mission, snap.error or "planner session failed")
        elif snap.status == "idle" and snap.agent_replied and not snap.awaiting_human:
            await self._record_decisions(mission)
            await self.import_plan(mission.id)
        else:
            return
        # The planner is done either way: end its process.
        if mission.plan_session_id is not None:
            try:
                await self._service.sessions.stop(
                    mission.plan_session_id, acting_user=mission.owner_user_id
                )
            except SessionServiceError:
                _logger.warning("shipcrew: could not stop planner of mission %s", mission.id)

    async def sync(self) -> None:
        """Scheduler hook: import the plan of every planner that has finished."""
        for mission in await self._service.list_missions():
            if mission.plan_status != "running" or mission.plan_session_id is None:
                continue
            if mission.id in self._busy:
                continue
            try:
                snap = await self._service.sessions.snapshot(
                    mission.plan_session_id, acting_user=mission.owner_user_id
                )
            except SessionServiceError as exc:
                _logger.warning(
                    "shipcrew: planner read failed for mission %s: %s", mission.id, exc
                )
                continue
            await self._finish(mission, snap)
