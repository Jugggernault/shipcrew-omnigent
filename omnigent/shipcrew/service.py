"""Board operations shared by the HTTP router and the background scheduler."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.shipcrew import main_deps
from omnigent.shipcrew.approvals import approved_write_paths
from omnigent.shipcrew.branches import task_branch
from omnigent.shipcrew.ci_install import CiInstall, ensure_ci_workflow
from omnigent.shipcrew.events import MissionEventBus
from omnigent.shipcrew.gates import GateContext, evaluate_gates, find_cycle
from omnigent.shipcrew.inherited_tests import grant_inherited_tests
from omnigent.shipcrew.issue_sync import GitHubSync
from omnigent.shipcrew.models import TASK_STATUSES
from omnigent.shipcrew.planner import PlanRunner
from omnigent.shipcrew.pr_loop import PrLoop, default_base_ref, is_loop_hold
from omnigent.shipcrew.preview import PreviewRunner
from omnigent.shipcrew.projects import MissionProjects
from omnigent.shipcrew.sessions import (
    RootSessionRequest,
    SessionService,
    SessionServiceError,
    SessionSnapshot,
)
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.ship import ShipRunner
from omnigent.shipcrew.store import ACTIVE_STATUSES, Assignee, Mission, ShipcrewStore, Task
from omnigent.shipcrew.verify import is_verify_hold

_logger = logging.getLogger(__name__)

TASK_LABEL_KEY = "shipcrew.task_id"
ROLE_LABEL_KEY = "shipcrew.role"
_PLAIN_FIELDS = ("title", "body", "acceptance", "owned_paths", "position")
# Tasks whose owned paths are off limits to another task of the mission.
CONTRACT_ACTIVE_STATUSES = frozenset({"ready", "running", "review", "intervention"})


def _not_found(what: str, ident: str) -> OmnigentError:
    return OmnigentError(f"{what} {ident!r} not found", code=ErrorCode.NOT_FOUND)


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _conflict(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.CONFLICT)


def build_prompt(task: Task, branch: str | None = None) -> str:
    """The first message the task's root agent receives."""
    branch = branch or task.branch or task_branch(task.id, task.title)
    lines = [f"# {task.title}", ""]
    if task.body.strip():
        lines += [task.body.strip(), ""]
    if task.acceptance:
        lines += ["## Acceptance criteria", *(f"- {a}" for a in task.acceptance), ""]
    lines.append(f"You work on branch `{branch}` in a dedicated git worktree of the repository.")
    if task.owned_paths:
        owned = ", ".join(f"`{p}`" for p in task.owned_paths)
        lines.append(f"Keep your changes inside the paths this task owns: {owned}.")
    lines.append(
        "Commit your work with clear messages; never push (shipcrew pushes and opens the PR). "
        "Stop when the acceptance criteria are met, and end with the final line `PASS` "
        "or `FAIL: <reason>`."
    )
    return "\n".join(lines)


# Review cards are still watched: Claude can end its turn while a background
# sub-agent keeps working, and resume when that sub-agent reports.
WATCHED_REVIEW_STATUSES = frozenset({"review"})
# Cards whose root session is (or may still be) alive.
SESSION_HOLDING_STATUSES = ACTIVE_STATUSES | WATCHED_REVIEW_STATUSES


def merge_ready(task: Task) -> bool:
    """A PR with green CI and an approved review: only the merge is left.

    The developer session is no longer needed then, so its runner dying (or
    its session disappearing) must not block the card: the PR loop merges.
    """
    approved = (task.review or {}).get("verdict") == "approve" or task.human_approved
    return task.pr_number is not None and task.ci == "green" and approved


def map_session_state(task: Task, snap: SessionSnapshot | None) -> dict[str, Any]:
    """Task field changes implied by the root session's current state."""
    if (snap is None or snap.status == "failed") and merge_ready(task):
        kept: dict[str, Any] = {}
        if snap is not None and snap.cost_usd is not None and snap.cost_usd != task.cost_usd:
            kept["cost_usd"] = snap.cost_usd
        if task.status == "running":
            kept.update(status="review", blocked_reason=None)
        return kept
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
        if snap.pending_ask and task.status != "intervention":
            # What is asked, on the card (reason badge) and in the report.
            changes["intervention"] = dict(snap.pending_ask)
            ask = snap.pending_ask
            changes["blocked_reason"] = (
                f"Needs approval: {ask.get('policy') or 'agent'}: {ask.get('preview') or ''}"
            ).rstrip(": ")
    elif snap.status in ("running", "waiting"):
        changes.update(status="running", session_seen_active=True)
        if task.status == "intervention" and (task.blocked_reason or "").startswith(
            "Needs approval: "
        ):
            changes["blocked_reason"] = None  # the ask was answered
    elif snap.status == "failed":
        changes.update(status="blocked", blocked_reason=snap.error or "agent session failed")
    elif snap.status == "idle" and (task.session_seen_active or snap.agent_replied):
        changes["status"] = "review"
    # ``intervention`` is not a column: store.update_task logs it with the entry.
    return {k: v for k, v in changes.items() if getattr(task, k, None) != v}


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
        # Missions whose CI workflow is settled (installed, present or refused).
        self._ci_done: dict[str, CiInstall] = {}
        self._ci_locks: dict[str, asyncio.Lock] = {}
        self.pr_loop = PrLoop(self)
        self.planner = PlanRunner(self)
        self.github = GitHubSync(self)
        self.ship = ShipRunner(self)
        self.preview = PreviewRunner(self)
        self._deploy_target: Any | None = None
        self.projects = MissionProjects(self)

    def deploy_target(self) -> Any:
        """The deploy target (``SHIPCREW_DEPLOY_TARGET``), resolved once per process.

        ``auto`` runs ``docker info`` the first time: call it from a thread.
        """
        if self._deploy_target is None:
            from omnigent.shipcrew.deploy_targets import select_target

            self._deploy_target = select_target(self.settings)
            _logger.info("shipcrew: deploy target %s", self._deploy_target.name)
        return self._deploy_target

    def set_deploy_target(self, target: Any) -> None:
        """Replace the deploy target (tests, embedders)."""
        self._deploy_target = target

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
        """Create the mission and its omnigent project (best effort)."""
        mission = await self._call(
            self.store.create_mission, title, repo_path, repo_url, owner_user_id=owner
        )
        if await self.projects.project_for(mission, owner) is not None:
            mission = await self.require_mission(mission.id)
        return mission

    async def set_mission_project(self, mission_id: str, project_id: str | None) -> Mission:
        """Store the mission's omnigent project (``mission.updated``)."""
        mission = await self._call(self.store.update_mission, mission_id, project_id=project_id)
        if mission is None:
            raise _not_found("mission", mission_id)
        self.bus.mission_updated(mission)
        return mission

    async def rename_mission(
        self, mission_id: str, title: str, acting_user: str | None
    ) -> Mission:
        """Rename the mission and, when that name is free, its project."""
        mission = await self._call(self.store.update_mission, mission_id, title=title)
        if mission is None:
            raise _not_found("mission", mission_id)
        await self.projects.rename(mission, acting_user)
        self.bus.mission_updated(mission)
        return mission

    async def mission_project_id(self, mission: Mission, acting_user: str | None) -> str | None:
        """The project new sessions of ``mission`` are filed in (created lazily)."""
        return await self.projects.project_for(mission, acting_user)

    async def require_mission(self, mission_id: str) -> Mission:
        mission = await self._call(self.store.get_mission, mission_id)
        if mission is None:
            raise _not_found("mission", mission_id)
        return mission

    @staticmethod
    def visible_to(mission: Mission, user_id: str | None) -> bool:
        """Whether ``user_id`` may see and drive ``mission``.

        ``None`` means auth is disabled (single user). Otherwise only the
        mission's creator does: its tasks start agents on that user's host.
        """
        return user_id is None or mission.owner_user_id in (None, user_id)

    async def list_missions_for(self, user_id: str | None) -> list[Mission]:
        return [m for m in await self.list_missions() if self.visible_to(m, user_id)]

    async def authorize_mission(self, mission_id: str, user_id: str | None) -> Mission:
        """The mission, or 404 when it does not exist or belongs to someone else."""
        mission = await self._call(self.store.get_mission, mission_id)
        if mission is None or not self.visible_to(mission, user_id):
            raise _not_found("mission", mission_id)
        return mission

    async def authorize_task(self, task_id: str, user_id: str | None) -> Task:
        """The task, or 404 when it does not exist or its mission is someone else's."""
        task = await self._call(self.store.get_task, task_id)
        mission = (
            await self._call(self.store.get_mission, task.mission_id) if task is not None else None
        )
        if task is None or mission is None or not self.visible_to(mission, user_id):
            raise _not_found("task", task_id)
        return task

    async def set_auto_ship(self, mission_id: str, auto_ship: bool) -> Mission:
        """Turn "deploy when every task is merged" on or off (``mission.updated``)."""
        mission = await self._call(self.store.update_mission, mission_id, auto_ship=auto_ship)
        if mission is None:
            raise _not_found("mission", mission_id)
        self.bus.mission_updated(mission)
        return mission

    async def set_auto_run(self, mission_id: str, auto_run: bool) -> Mission:
        """Turn "run automatically after planning" on or off (``mission.updated``)."""
        mission = await self._call(self.store.update_mission, mission_id, auto_run=auto_run)
        if mission is None:
            raise _not_found("mission", mission_id)
        self.bus.mission_updated(mission)
        return mission

    async def start_all(self, mission_id: str, task_ids: set[str] | None = None) -> list[Task]:
        """Queue every backlog task of the mission (``ready``); idempotent.

        The scheduler's gates still decide what actually runs.

        :param task_ids: Restrict to these tasks (a plan import's own).
        :returns: The tasks moved to ready.
        """
        await self.require_mission(mission_id)
        moved = await self._call(self.store.ready_backlog, mission_id, task_ids)
        for task in moved:
            self.bus.task_updated(task)
        return moved

    async def stop_all(self, mission_id: str, acting_user: str | None) -> list[Task]:
        """Stop every running (or intervention) task of the mission.

        :returns: The tasks that were stopped (now ``blocked``).
        """
        stopped: list[Task] = []
        for task in await self.list_tasks(mission_id):
            if task.status in ACTIVE_STATUSES:
                stopped.append(await self.stop_task(task.id, acting_user))
        return stopped

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
        interrupt_agent = False
        terminate_agent = False
        if "assignee" in changes:
            raw = changes["assignee"]
            assignee = Assignee(kind=raw["kind"], id=raw["id"]) if raw else None
            update["assignee"] = assignee
            # A human taking the card stops the agent's turn; the process stays
            # so the human can take over its terminal.
            interrupt_agent = assignee is not None and assignee.kind == "human"
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
            # Moving a card off its session ends that session: a restart
            # opens a new one, so the old agent process must not linger.
            terminate_agent = task.status in SESSION_HOLDING_STATUSES
            update["status"] = status
            if status in ("ready", "backlog", "merged"):
                update["blocked_reason"] = None
        if terminate_agent:
            await self._stop_quietly(task, acting_user)
        elif interrupt_agent and task.status in ACTIVE_STATUSES:
            await self._cancel_quietly(task, acting_user)
        if not update:
            return task
        return await self._update(task.id, **update)

    async def _cancel_quietly(
        self, task: Task, acting_user: str | None, session_id: str | None = None
    ) -> None:
        session_id = session_id or task.root_session_id
        if session_id is None:
            return
        try:
            await self.sessions.cancel(session_id, acting_user=acting_user)
        except SessionServiceError:
            _logger.warning("shipcrew: could not interrupt session of task %s", task.id)

    async def _stop_quietly(
        self, task: Task, acting_user: str | None, session_id: str | None = None
    ) -> None:
        session_id = session_id or task.root_session_id
        if session_id is None:
            return
        try:
            await self.sessions.stop(session_id, acting_user=acting_user)
        except SessionServiceError:
            _logger.warning("shipcrew: could not stop session of task %s", task.id)

    async def start_task(self, task_id: str, acting_user: str | None) -> Task:
        """Create the task's worktree + root session and mark it running."""
        # Claim before the first await: the API and the scheduler can both try
        # to start the same card, and each check below yields to the loop.
        if task_id in self._starting:
            raise _conflict("task is already starting")
        self._starting.add(task_id)
        try:
            task = await self.require_task(task_id)
            if task.status in ACTIVE_STATUSES:
                return task
            if task.status == "merged":
                raise _conflict("task is already merged")
            if task.human_assigned:
                raise _conflict("task is assigned to a human")
            mission = await self.require_mission(task.mission_id)
            agent_dir = self.settings.agents_dir / task.role
            if not (agent_dir / "config.yaml").is_file():
                reason = f"no agent bundle for role {task.role!r} in {self.settings.agents_dir}"
                return await self._update(task.id, status="blocked", blocked_reason=reason)
            if task.status in WATCHED_REVIEW_STATUSES:
                # A restart from Review opens a new session: end the idle one.
                await self._stop_quietly(task, acting_user)
            # Claim a capacity slot before the slow worktree/session work.
            # The branch name is fixed at the first start (titles can change).
            branch = task.branch or task_branch(task.id, task.title)
            task = await self._update(
                task.id,
                status="running",
                blocked_reason=None,
                session_seen_active=False,
                branch=branch,
                started_at=task.started_at or time.time(),
            )
            # Before the first branch of the mission is cut: CI on main.
            await self.ensure_ci(mission)
            if self.settings.pr_loop_enabled:
                # A main checkout with a lockfile but no node_modules gets one
                # background install, so later worktrees seed from it.
                await self._call(
                    main_deps.refresh_after_merge, Path(mission.repo_path), self.settings.pr_base
                )
            base_branch = self.settings.base_branch
            if base_branch is None and self.settings.pr_loop_enabled:
                base_branch = await self._call(
                    default_base_ref, mission.repo_path, self.settings.pr_base
                )
            others = await self._other_active_tasks(task)
            task = await self._grant_inherited_tests(task, mission, base_branch, others)
            request = RootSessionRequest(
                task_id=task.id,
                title=task.title,
                prompt=build_prompt(task, branch),
                repo_path=mission.repo_path,
                branch=branch,
                agent_dir=agent_dir,
                acting_user=acting_user or mission.owner_user_id,
                base_branch=base_branch,
                labels={TASK_LABEL_KEY: task.id, ROLE_LABEL_KEY: task.role},
                owned_paths=tuple(str(p) for p in task.owned_paths),
                project_id=await self.mission_project_id(mission, acting_user),
                other_tasks=tuple(
                    {"title": o.title, "owned_paths": list(o.owned_paths)} for o in others
                ),
            )
            try:
                session_id = await self.sessions.create_root_session(request)
            except Exception as exc:
                # Any failure (not only SessionServiceError) must release the
                # slot claimed above, or the card stays "running" with no session.
                if not isinstance(exc, SessionServiceError):
                    _logger.exception("shipcrew: session start failed for task %s", task.id)
                reason = str(exc) or type(exc).__name__
                return await self._update(task.id, status="blocked", blocked_reason=reason)
            if mission.status == "planning":
                await self._call(self.store.set_mission_status, mission.id, "active")
            # The card may have been stopped, moved or taken by a human while
            # the session was being created: never leave that agent running.
            current = await self.require_task(task.id)
            if current.status not in ACTIVE_STATUSES:
                await self._stop_quietly(current, acting_user, session_id)
            elif current.human_assigned:
                await self._cancel_quietly(current, acting_user, session_id)
            return await self._update(task.id, root_session_id=session_id)
        finally:
            self._starting.discard(task_id)

    async def record_approved_write(self, session_id: str, reason: str | None) -> Task | None:
        """A human accepted an owned-paths ASK in *session_id*: remember the paths.

        Only a task's root session counts (loop children do not write). The
        merge gate skips these paths (``approved_paths``).
        """
        paths = approved_write_paths(reason)
        if not paths:
            return None
        task = await self._call(self.store.task_for_session, session_id)
        if task is None:
            return None
        updated = await self._call(self.store.add_approved_paths, task.id, paths)
        if updated is not None and updated.approved_paths != task.approved_paths:
            self.bus.task_updated(updated)
        return updated

    async def _other_active_tasks(self, task: Task) -> list[Task]:
        """The mission's other ready / running / review / intervention tasks with owned paths."""
        tasks = await self._call(self.store.list_tasks, task.mission_id)
        return [
            t
            for t in tasks
            if t.id != task.id and t.status in CONTRACT_ACTIVE_STATUSES and t.owned_paths
        ]

    async def _grant_inherited_tests(
        self, task: Task, mission: Mission, base_ref: str | None, others: list[Task]
    ) -> Task:
        """Own the existing tests that only import this task's modules (see ``inherited_tests``).

        Best effort: a failed scan starts the task with its plan contract.
        """
        if not task.owned_paths:
            return task
        other_globs = [g for o in others for g in o.owned_paths]
        try:
            granted = await self._call(
                grant_inherited_tests,
                mission.repo_path,
                base_ref or "HEAD",
                task.owned_paths,
                other_globs,
            )
        except Exception:
            _logger.exception("shipcrew: test grant failed for task %s", task.id)
            return task
        new = [p for p in granted if p not in task.owned_paths]
        if not new:
            return task
        _logger.info("shipcrew: task %s inherits tests %s", task.id, new)
        return await self._update(task.id, owned_paths=[*task.owned_paths, *new])

    async def ensure_ci(self, mission: Mission) -> CiInstall | None:
        """Install the shipcrew CI workflow on the mission's base once (see ``ci_install``).

        Serialized per mission, so parallel first starts all fork from the
        commit that has CI. ``skipped`` (no origin or no base yet) is retried at
        the next start; any other outcome is kept for the server's lifetime.
        Never raises: a failure is logged and the task starts anyway.
        """
        if not (self.settings.install_ci and self.settings.pr_loop_enabled):
            return None
        lock = self._ci_locks.setdefault(mission.id, asyncio.Lock())
        async with lock:
            done = self._ci_done.get(mission.id)
            if done is not None:
                return done
            try:
                result = await self._call(
                    ensure_ci_workflow, mission.repo_path, self.settings.pr_base
                )
            except Exception:
                _logger.exception("shipcrew: CI install failed for mission %s", mission.id)
                result = CiInstall("failed", "unexpected error")
            if result.status == "failed":
                _logger.warning("shipcrew: CI not installed: %s", result.detail)
            if result.status != "skipped":
                self._ci_done[mission.id] = result
            return result

    async def update_fields(self, task_id: str, **fields: Any) -> Task:
        """Write task columns and publish ``task.updated`` (no status side effects)."""
        return await self._update(task_id, **fields)

    async def block_task(self, task_id: str, reason: str, acting_user: str | None) -> Task:
        """Park a card as blocked with ``reason``, ending its agent session if any."""
        task = await self.require_task(task_id)
        if task.status in SESSION_HOLDING_STATUSES:
            await self._stop_quietly(task, acting_user)
        return await self._update(task.id, status="blocked", blocked_reason=reason)

    async def stop_task(self, task_id: str, acting_user: str | None) -> Task:
        """Terminate the agent's session and park the card as blocked."""
        task = await self.require_task(task_id)
        await self._stop_quietly(task, acting_user)
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
            if self._loop_owned(task, snap):
                # The PR loop drives this card: its reviewer / integrator
                # children make the root look busy, so only the cost syncs.
                changes = {k: v for k, v in changes.items() if k == "cost_usd"}
            if changes:
                await self._update(task.id, **changes)

    def _loop_owned(self, task: Task, snap: SessionSnapshot | None) -> bool:
        if not self.settings.pr_loop_enabled:
            return False
        # A loop hold only moves when a human talks to the agent in its session.
        human_resumed = snap is not None and (
            snap.awaiting_human or snap.status in ("running", "waiting")
        )
        if task.pr_number is None:
            # A verify card held after its last fix cycle has no PR.
            return is_verify_hold(task) and not human_resumed
        if task.status in WATCHED_REVIEW_STATUSES:
            return True
        return is_loop_hold(task) and not human_resumed

    async def advance_reviews(self) -> None:
        """PR loop step for every review card (no-op when the loop is off)."""
        if self.settings.pr_loop_enabled:
            await self.pr_loop.tick()

    async def approve_task(self, task_id: str) -> Task:
        return await self.pr_loop.approve(task_id)

    async def request_changes(self, task_id: str, message: str) -> Task:
        return await self.pr_loop.request_changes(task_id, message)

    async def schedule_ready(self) -> list[str]:
        """Start every ready task whose four gates pass, in parallel; returns started ids.

        The gates are evaluated one task at a time against a local view in
        which each task picked earlier this tick already counts as running, so
        capacity and owned-path overlap stay exact. The picked tasks then start
        concurrently: a start is mostly waiting (worktree, runner launch), and
        the one unsafe step, ``git worktree add`` on a shared repository, is
        serialized per repository by the session layer.
        """
        tasks = {t.id: t for t in await self._call(self.store.list_tasks)}
        missions = {m.id: m for m in await self.list_missions()}
        picked: list[Task] = []
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
            picked.append(task)
            # Holds a slot (and its paths) for the gates of the next ready tasks.
            tasks[task.id] = dataclasses.replace(task, status="running")

        async def _start(task: Task) -> str | None:
            mission = missions.get(task.mission_id)
            owner = mission.owner_user_id if mission is not None else None
            try:
                await self.start_task(task.id, owner)
            except OmnigentError as exc:
                _logger.info("shipcrew: skipped start of task %s: %s", task.id, exc)
                return None
            return task.id

        results = await asyncio.gather(*(_start(t) for t in picked))
        return [task_id for task_id in results if task_id is not None]
