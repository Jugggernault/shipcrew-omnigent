"""``/v1/shipcrew`` routes and the one-call server mount.

Routes are hidden from the OpenAPI schema so the checked-in ``openapi.json``
(upstream-owned) does not drift in this fork.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._auth_helpers import require_user
from omnigent.shipcrew.commands import MAX_COMMAND_CHARS, classify, unknown_command_message
from omnigent.shipcrew.events import MissionEventBus
from omnigent.shipcrew.scheduler import ShipcrewScheduler
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import OmnigentSessionService, SessionService
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.store import ShipcrewStore

PREFIX = "/v1/shipcrew"
_HEARTBEAT_S = 15.0
_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

TaskStatusLiteral = Literal[
    "backlog", "ready", "running", "review", "intervention", "merged", "blocked"
]


class AssigneeBody(BaseModel):
    kind: Literal["agent", "human"]
    id: str = Field(min_length=1, max_length=256)


class CreateMissionBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    repo_path: str = Field(min_length=1, max_length=2048)
    repo_url: str | None = Field(default=None, max_length=2048)

    @field_validator("repo_path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("repo_path must be an absolute path on the host")
        return value


class CreateTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    body: str = ""
    acceptance: list[str] = Field(default_factory=list)
    role: str = "developer"
    depends_on: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)

    @field_validator("role")
    @classmethod
    def _bundle_name(cls, value: str) -> str:
        # The role names a directory under SHIPCREW_AGENTS_DIR: no traversal.
        if not _ROLE_RE.match(value) or ".." in value:
            raise ValueError("role must be a bundle directory name")
        return value


class PatchTaskBody(BaseModel):
    status: TaskStatusLiteral | None = None
    assignee: AssigneeBody | None = None
    position: float | None = None
    depends_on: list[str] | None = None
    owned_paths: list[str] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    body: str | None = None
    acceptance: list[str] | None = None

    def changes(self) -> dict[str, Any]:
        """Fields the client sent; ``assignee: null`` (unassign) is kept."""
        sent = self.model_dump(include=self.model_fields_set)
        return {k: v for k, v in sent.items() if v is not None or k == "assignee"}


class RequestChangesBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class PlanBody(BaseModel):
    # ``None``/blank: the planner reads the repo's ``.shipcrew/prd.md``.
    prd: str | None = Field(default=None, max_length=200_000)


class PatchMissionBody(BaseModel):
    auto_run: bool | None = None


class CommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def create_shipcrew_router(
    get_service: Callable[[], ShipcrewService],
    auth_provider: Any | None = None,
    on_ready: Callable[[], None] | None = None,
) -> APIRouter:
    """Build the shipcrew router (mount under :data:`PREFIX`).

    :param get_service: Returns the (lazily built) board service.
    :param auth_provider: Same provider as the other ``/v1`` routes; ``None``
        disables auth (single-user).
    :param on_ready: Called when a card moves to ``ready`` so the scheduler
        runs without waiting for its next tick.
    """
    router = APIRouter()

    async def _svc() -> ShipcrewService:
        return await asyncio.to_thread(get_service)

    @router.get("/missions")
    async def list_missions(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        missions = await (await _svc()).list_missions_for(user_id)
        return {"missions": [m.to_api() for m in missions]}

    @router.post("/missions")
    async def create_mission(request: Request, body: CreateMissionBody) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        mission = await (await _svc()).create_mission(
            body.title, body.repo_path, body.repo_url, user_id
        )
        return mission.to_api()

    @router.patch("/missions/{mission_id}")
    async def patch_mission(
        request: Request, mission_id: str, body: PatchMissionBody
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        mission = await service.authorize_mission(mission_id, user_id)
        if body.auto_run is not None:
            mission = await service.set_auto_run(mission_id, body.auto_run)
        return mission.to_api()

    async def _start_all(service: ShipcrewService, mission_id: str) -> list[str]:
        started = [t.id for t in await service.start_all(mission_id)]
        if started and on_ready is not None:
            on_ready()
        return started

    @router.post("/missions/{mission_id}/start-all")
    async def start_all(request: Request, mission_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        started = await _start_all(service, mission_id)
        mission = await service.require_mission(mission_id)
        return {"mission": mission.to_api(), "started": started}

    @router.post("/missions/{mission_id}/command")
    async def mission_command(
        request: Request, mission_id: str, body: CommandBody
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        intent = classify(body.text)
        if intent is None:
            raise OmnigentError(unknown_command_message(body.text), code=ErrorCode.INVALID_INPUT)
        result: dict[str, Any] = {"intent": intent}
        if intent == "start_all":
            started = await _start_all(service, mission_id)
            result["started"] = started
            result["message"] = (
                f"Moved {_plural(len(started), 'task')} to Ready."
                if started
                else "No backlog task to run."
            )
        elif intent == "stop_all":
            stopped = [t.id for t in await service.stop_all(mission_id, user_id)]
            result["stopped"] = stopped
            result["message"] = (
                f"Stopped {_plural(len(stopped), 'running task')}."
                if stopped
                else "No running task to stop."
            )
        elif intent == "plan":
            # The PRD comes from the repo (.shipcrew/prd.md); the dialog sends text.
            await service.planner.start(mission_id, None, user_id)
            result["message"] = "Planner started from the repository PRD."
        else:
            report = await service.github.sync_mission(mission_id)
            result["sync"] = report.to_api()
            result["message"] = (
                "GitHub sync done." if report.ok else f"GitHub sync skipped: {report.reason}"
            )
        result["mission"] = (await service.require_mission(mission_id)).to_api()
        return result

    @router.post("/missions/{mission_id}/plan")
    async def plan_mission(
        request: Request, mission_id: str, body: PlanBody | None = None
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        prd = body.prd if body is not None else None
        return (await service.planner.start(mission_id, prd, user_id)).to_api()

    @router.post("/missions/{mission_id}/sync")
    async def sync_mission(request: Request, mission_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        report = await service.github.sync_mission(mission_id)
        mission = await service.require_mission(mission_id)
        # ``sync`` is extra to the Mission contract: why a sync did nothing.
        return {**mission.to_api(), "sync": report.to_api()}

    @router.get("/missions/{mission_id}/tasks")
    async def list_tasks(request: Request, mission_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        tasks = await service.list_tasks(mission_id)
        return {"tasks": [t.to_api() for t in tasks]}

    @router.post("/missions/{mission_id}/tasks")
    async def create_task(
        request: Request, mission_id: str, body: CreateTaskBody
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)
        task = await service.create_task(mission_id, **body.model_dump())
        return task.to_api()

    @router.patch("/tasks/{task_id}")
    async def patch_task(request: Request, task_id: str, body: PatchTaskBody) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_task(task_id, user_id)
        task = await service.patch_task(task_id, body.changes(), user_id)
        if task.status == "ready" and on_ready is not None:
            on_ready()
        return task.to_api()

    @router.post("/tasks/{task_id}/start")
    async def start_task(request: Request, task_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_task(task_id, user_id)
        return (await service.start_task(task_id, user_id)).to_api()

    @router.post("/tasks/{task_id}/stop")
    async def stop_task(request: Request, task_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_task(task_id, user_id)
        return (await service.stop_task(task_id, user_id)).to_api()

    @router.post("/tasks/{task_id}/approve")
    async def approve_task(request: Request, task_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_task(task_id, user_id)
        task = await service.approve_task(task_id)
        if on_ready is not None:
            on_ready()  # merge on the next tick, not the next poll
        return task.to_api()

    @router.post("/tasks/{task_id}/request-changes")
    async def request_changes(
        request: Request, task_id: str, body: RequestChangesBody
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_task(task_id, user_id)
        return (await service.request_changes(task_id, body.message)).to_api()

    @router.get("/missions/{mission_id}/stream")
    async def stream(request: Request, mission_id: str) -> StreamingResponse:
        user_id = require_user(request, auth_provider)
        service = await _svc()
        await service.authorize_mission(mission_id, user_id)

        async def events() -> AsyncIterator[str]:
            async with service.bus.subscribe(mission_id) as queue:
                # Full state first, so a (re)connecting board never misses edits.
                for task in await service.list_tasks(mission_id):
                    yield _sse({"type": "task.updated", "task": task.to_api()})
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield _sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


class _Lazy:
    """Builds the store/service on first use, so mounting costs nothing."""

    def __init__(self, build: Callable[[], ShipcrewService]) -> None:
        self._build = build
        self._lock = threading.Lock()
        self._service: ShipcrewService | None = None

    def __call__(self) -> ShipcrewService:
        with self._lock:
            if self._service is None:
                self._service = self._build()
            return self._service


def mount_shipcrew(
    app: FastAPI,
    *,
    conversation_store: Any,
    auth_provider: Any | None = None,
    settings: ShipcrewSettings | None = None,
    session_service: SessionService | None = None,
) -> Callable[[], ShipcrewService]:
    """Mount ``/v1/shipcrew`` and hook the scheduler into the app lifespan.

    :param conversation_store: Any omnigent store; its database is reused.
    :param auth_provider: The app's auth provider (same auth as ``/v1``).
    :param settings: Defaults to :meth:`ShipcrewSettings.from_env`.
    :param session_service: Defaults to :class:`OmnigentSessionService`.
    :returns: The lazy service getter (handy for tests and embedders).
    """
    cfg = settings or ShipcrewSettings.from_env()
    bus = MissionEventBus()

    def build() -> ShipcrewService:
        from sqlalchemy import create_engine

        from omnigent.db.utils import get_or_create_engine

        # A dedicated SHIPCREW_DB_URL holds only shipcrew tables; otherwise share
        # omnigent's (already migrated, cached) engine.
        engine = (
            create_engine(cfg.db_url)
            if cfg.db_url
            else get_or_create_engine(conversation_store.storage_location)
        )
        sessions = session_service or OmnigentSessionService(
            app, auth_provider, host_id=cfg.host_id
        )
        return ShipcrewService(ShipcrewStore(engine), bus, sessions, cfg)

    get_service = _Lazy(build)
    scheduler = ShipcrewScheduler(get_service, cfg.poll_interval_s)
    app.include_router(
        create_shipcrew_router(get_service, auth_provider, on_ready=scheduler.poke),
        prefix=PREFIX,
        tags=["shipcrew"],
        include_in_schema=False,
    )
    app.state.shipcrew_scheduler = scheduler

    if cfg.scheduler_enabled:
        inner = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app_: Any) -> AsyncIterator[Any]:
            async with inner(app_) as state:
                scheduler.start()
                try:
                    yield state
                finally:
                    with contextlib.suppress(Exception):
                        await scheduler.stop()

        app.router.lifespan_context = lifespan
    return get_service
