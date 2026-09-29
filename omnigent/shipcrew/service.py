"""Board operations shared by the HTTP router and the background scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.shipcrew.events import MissionEventBus
from omnigent.shipcrew.gates import GateContext, evaluate_gates, find_cycle
from omnigent.shipcrew.models import TASK_STATUSES
from omnigent.shipcrew.sessions import (
    RootSessionRequest,
    SessionService,
    SessionServiceError,
    SessionSnapshot,
)
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.store import ACTIVE_STATUSES, Assignee, Mission, ShipcrewStore, Task

_logger = logging.getLogger(__name__)

TASK_LABEL_KEY = "shipcrew.task_id"
ROLE_LABEL_KEY = "shipcrew.role"
_PLAIN_FIELDS = ("title", "body", "acceptance", "owned_paths", "position")


def _not_found(what: str, ident: str) -> OmnigentError:
    return OmnigentError(f"{what} {ident!r} not found", code=ErrorCode.NOT_FOUND)


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _conflict(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.CONFLICT)


def build_prompt(task: Task) -> str:
    """The first message the task's root agent receives."""
    lines = [f"# {task.title}", ""]
    if task.body.strip():
        lines += [task.body.strip(), ""]
    if task.acceptance:
        lines += ["## Acceptance criteria", *(f"- {a}" for a in task.acceptance), ""]
    lines.append(
        f"You work on branch `task/{task.id}` in a dedicated git worktree of the repository."
    )
    if task.owned_paths:
        owned = ", ".join(f"`{p}`" for p in task.owned_paths)
        lines.append(f"Keep your changes inside the paths this task owns: {owned}.")
    lines.append(
        "Commit your work with clear messages. Stop when the acceptance criteria are met."
    )
    return "\n".join(lines)


# Review cards are still watched: Claude can end its turn while a background
# sub-agent keeps working, and resume when that sub-agent reports.
WATCHED_REVIEW_STATUSES = frozenset({"review"})


def map_session_state(task: Task, snap: SessionSnapshot | None) -> dict[str, Any]:
    """Task field changes implied by the root session's current state."""
    if snap is None:
        if task.status in WATCHED_REVIEW_STATUSES:
            return {}
        return {"status": "blocked", "blocked_reason": "agent session no longer exists"}
    changes: dict[str, Any] = {}
    if snap.cost_usd is not None and snap.cost_usd != task.cost_usd:
        changes["cost_usd"] = snap.cost_usd
    resumed = snap.awaiting_human or snap.status in ("running", "waiting")
    if task.status in WATCHED_REVIEW_STATUSES and not resumed:
        # A review card only moves again when its agent picks work back up
        # (e.g. a background sub-agent reported after the turn ended).
        return {k: v for k, v in changes.items() if getattr(task, k) != v}
    if snap.awaiting_human:
        changes.update(status="intervention", session_seen_active=True)
    elif snap.status in ("running", "waiting"):
        changes.update(status="running", session_seen_active=True)
    elif snap.status == "failed":
        changes.update(status="blocked", blocked_reason=snap.error or "agent session failed")
    elif snap.status == "idle" and (task.session_seen_active or snap.agent_replied):
        changes["status"] = "review"
    return {k: v for k, v in changes.items() if getattr(task, k) != v}


class ShipcrewService:
    """Mission/task logic over the store, the event bus and the session seam."""

    def __init__(
        self,
        store: ShipcrewStore,
        bus: MissionEventBus,
        sessions: SessionService,
        settings: ShipcrewSettings,
    ) -> None:
        self.store = store
        self.bus = bus
        self.sessions = sessions
        self.settings = settings
        self._starting: set[str] = set()

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _update(self, task_id: str, **fields: Any) -> Task:
        task = await self._call(self.store.update_task, task_id, **fields)
        if task is None:
            raise _not_found("task", task_id)
        self.bus.task_updated(task)
        return task

    # ── Missions ────────────────────────────────────────────────

    async def list_missions(self) -> list[Mission]:
        return await self._call(self.store.list_missions)

    async def create_mission(
        self, title: str, repo_path: str, repo_url: str | None, owner: str | None
    ) -> Mission:
        return await self._call(
            self.store.create_mission, title, repo_path, repo_url, owner_user_id=owner
        )

    async def require_mission(self, mission_id: str) -> Mission:
        mission = await self._call(self.store.get_mission, mission_id)
        if mission is None:
            raise _not_found("mission", mission_id)
        return mission

    # ── Tasks ───────────────────────────────────────────────────

    async def require_task(self, task_id: str) -> Task:
        task = await self._call(self.store.get_task, task_id)
        if task is None:
            raise _not_found("task", task_id)
        return task

    async def list_tasks(self, mission_id: str) -> list[Task]:
        await self.require_mission(mission_id)
        return await self._call(self.store.list_tasks, mission_id)

    async def _validate_deps(self, mission_id: str, task_id: str | None, deps: list[str]) -> None:
        siblings = {t.id: t for t in await self._call(self.store.list_tasks, mission_id)}
        unknown = [d for d in deps if d not in siblings]
        if unknown:
            raise _invalid(f"depends_on references unknown tasks: {', '.join(unknown)}")
        if task_id is not None and find_cycle(task_id, deps, siblings):
            raise _invalid("depends_on would create a dependency cycle")

    async def create_task(self, mission_id: str, **fields: Any) -> Task:
        await self.require_mission(mission_id)
        await self._validate_deps(mission_id, None, fields.get("depends_on") or [])
        task = await self._call(self.store.create_task, mission_id, **fields)
        self.bus.task_updated(task)
        return task

    async def patch_task(
        self, task_id: str, changes: dict[str, Any], acting_user: str | None
    ) -> Task:
        """Apply a board edit; status moves carry the side effects of the contract."""
        task = await self.require_task(task_id)
        fields = {k: changes[k] for k in _PLAIN_FIELDS if k in changes}
        if "depends_on" in changes:
            deps = list(changes["depends_on"] or [])
            await self._validate_deps(task.mission_id, task.id, deps)
            fields["depends_on"] = deps
        update: dict[str, Any] = dict(fields)
        stop_agent = False
        if "assignee" in changes:
            raw = changes["assignee"]
            assignee = Assignee(kind=raw["kind"], id=raw["id"]) if raw else None
            update["assignee"] = assignee
            # A human taking the card stops the agent working on it.
            stop_agent = assignee is not None and assignee.kind == "human"
        status = changes.get("status")
        if status is not None and status != task.status:
            if status not in TASK_STATUSES:
                raise _invalid(f"unknown status {status!r}")
            if status == "intervention":
                raise _invalid("intervention is set by the agent session, not by the board")
            if status == "running":
                if fields or "assignee" in update:
                    await self._update(task.id, **update)
                return await self.start_task(task.id, acting_user)
            if task.status in ACTIVE_STATUSES:
                stop_agent = True
            update["status"] = status
            if status in ("ready", "backlog", "merged"):
                update["blocked_reason"] = None
        if stop_agent and task.status in ACTIVE_STATUSES and task.root_session_id:
            await self._cancel_quietly(task, acting_user)
        if not update:
            return task
        return await self._update(task.id, **update)

    async def _cancel_quietly(self, task: Task, acting_user: str | None) -> None:
        if task.root_session_id is None:
            return
        try:
            await self.sessions.cancel(task.root_session_id, acting_user=acting_user)
        except SessionServiceError:
            _logger.warning("shipcrew: could not interrupt session of task %s", task.id)

    async def start_task(self, task_id: str, acting_user: str | None) -> Task:
        """Create the task's worktree + root session and mark it running."""
        task = await self.require_task(task_id)
        if task.status in ACTIVE_STATUSES:
            return task
        if task.status == "merged":
            raise _conflict("task is already merged")
        if task.human_assigned:
            raise _conflict("task is assigned to a human")
        if task.id in self._starting:
            raise _conflict("task is already starting")
        mission = await self.require_mission(task.mission_id)
        agent_dir = self.settings.agents_dir / task.role
        if not (agent_dir / "config.yaml").is_file():
            reason = f"no agent bundle for role {task.role!r} in {self.settings.agents_dir}"
            return await self._update(task.id, status="blocked", blocked_reason=reason)
        self._starting.add(task.id)
        try:
            # Claim a capacity slot before the slow worktree/session work.
            task = await self._update(
                task.id, status="running", blocked_reason=None, session_seen_active=False
            )
            request = RootSessionRequest(
                task_id=task.id,
                title=task.title,
                prompt=build_prompt(task),
                repo_path=mission.repo_path,
                branch=f"task/{task.id}",
                agent_dir=agent_dir,
                acting_user=acting_user or mission.owner_user_id,
                base_branch=self.settings.base_branch,
                labels={TASK_LABEL_KEY: task.id, ROLE_LABEL_KEY: task.role},
            )
            try:
                session_id = await self.sessions.create_root_session(request)
            except SessionServiceError as exc:
                return await self._update(task.id, status="blocked", blocked_reason=str(exc))
            if mission.status == "planning":
                await self._call(self.store.set_mission_status, mission.id, "active")
            return await self._update(task.id, root_session_id=session_id)
        finally:
            self._starting.discard(task.id)

    async def stop_task(self, task_id: str, acting_user: str | None) -> Task:
        """Interrupt the agent and park the card as blocked."""
        task = await self.require_task(task_id)
        await self._cancel_quietly(task, acting_user)
        if task.status not in ACTIVE_STATUSES:
            return task
        return await self._update(task.id, status="blocked", blocked_reason="stopped by user")

    # ── Scheduler hooks ─────────────────────────────────────────

    async def sync_active(self) -> None:
        """Pull each agent-run task's session state back onto its card."""
        tasks = await self._call(
            self.store.list_tasks_by_status, ACTIVE_STATUSES | WATCHED_REVIEW_STATUSES
        )
        missions = {m.id: m for m in await self.list_missions()}
        for task in tasks:
            if task.human_assigned or task.root_session_id is None:
                continue
            if task.id in self._starting:
                continue
            owner = (
                missions[task.mission_id].owner_user_id if task.mission_id in missions else None
            )
            try:
                snap = await self.sessions.snapshot(task.root_session_id, acting_user=owner)
            except SessionServiceError as exc:
                _logger.warning("shipcrew: session read failed for task %s: %s", task.id, exc)
                continue
            changes = map_session_state(task, snap)
            if changes:
                await self._update(task.id, **changes)

    async def schedule_ready(self) -> list[str]:
        """Start every ready task whose four gates pass; returns started ids."""
        tasks = {t.id: t for t in await self._call(self.store.list_tasks)}
        missions = {m.id: m for m in await self.list_missions()}
        started: list[str] = []
        ready = [t for t in tasks.values() if t.status == "ready" and not t.human_assigned]
        for task in sorted(ready, key=lambda t: (t.position, t.created_at)):
            ctx = GateContext(
                tasks_by_id=tasks,
                repo_of={m.id: m.repo_path for m in missions.values()},
                max_parallel=self.settings.max_parallel,
                max_usd=self.settings.max_usd,
            )
            reason = evaluate_gates(task, ctx)
            if reason is not None:
                if reason != task.blocked_reason:
                    tasks[task.id] = await self._update(task.id, blocked_reason=reason)
                continue
            mission = missions.get(task.mission_id)
            owner = mission.owner_user_id if mission is not None else None
            try:
                tasks[task.id] = await self.start_task(task.id, owner)
            except OmnigentError as exc:
                _logger.info("shipcrew: skipped start of task %s: %s", task.id, exc)
                continue
            started.append(task.id)
        return started
