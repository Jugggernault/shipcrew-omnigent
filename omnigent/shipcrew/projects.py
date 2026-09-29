"""One mission = one omnigent project.

Every session shipcrew creates for a mission (planner, task roots, reviewer /
integrator children, the devops ship session) is filed into the mission's
omnigent project, so the sidebar groups them under one folder instead of a
flat list. The project is created (or adopted) on the mission's first use and
its id stored on ``Mission.project_id``.

Projects are owner-private in omnigent, so the project belongs to the mission
owner. Nothing here ever deletes a project: deleting a mission (or the user
deleting the folder) is left to the user. When the stored project is gone, the
next use files the sessions into a fresh one.

Best effort throughout: a project failure is logged and the session is created
unfiled, never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, cast

import httpx

from omnigent.shipcrew.sessions import ProjectNameTaken, ProjectRef, SessionServiceError

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService
    from omnigent.shipcrew.store import Mission

_logger = logging.getLogger(__name__)

# What a project call may raise; any of them leaves the session unfiled.
_PROJECT_ERRORS = (SessionServiceError, httpx.HTTPError, OSError, ValueError, KeyError)

# omnigent's CreateProjectRequest caps names at 100 characters.
PROJECT_NAME_MAX = 100
_SUFFIX_ROOM = 6  # " (99)"
_MAX_SUFFIX = 50


class ProjectApi(Protocol):
    """The slice of omnigent's ``/v1/projects`` API shipcrew uses."""

    async def list_projects(self, *, acting_user: str | None) -> list[ProjectRef]:
        """The acting user's projects."""
        ...

    async def create_project(self, name: str, *, acting_user: str | None) -> ProjectRef:
        """Create an empty project; raises :class:`ProjectNameTaken` on a name clash."""
        ...

    async def rename_project(
        self, project_id: str, name: str, *, acting_user: str | None
    ) -> ProjectRef | None:
        """Rename a project; ``None`` when it no longer exists."""
        ...


def project_base_name(title: str) -> str:
    """The project name for a mission title, within omnigent's length cap."""
    name = " ".join(title.split()) or "Mission"
    return name[: PROJECT_NAME_MAX - _SUFFIX_ROOM].rstrip()


def candidate_names(title: str) -> list[str]:
    """``title``, then ``title (2)``, ``title (3)``… for name clashes."""
    base = project_base_name(title)
    return [base, *(f"{base} ({n})" for n in range(2, _MAX_SUFFIX + 1))]


class MissionProjects:
    """Creates, reuses and renames the omnigent project of each mission."""

    def __init__(self, service: ShipcrewService) -> None:
        self._svc = service
        # One ensure per mission at a time: parallel task starts of the same
        # mission would otherwise each create a project.
        self._locks: dict[str, asyncio.Lock] = {}

    def _api(self) -> ProjectApi | None:
        sessions = self._svc.sessions
        if all(hasattr(sessions, name) for name in ("list_projects", "create_project")):
            return cast(ProjectApi, sessions)
        return None

    async def project_for(self, mission: Mission, acting_user: str | None = None) -> str | None:
        """The mission's project id, created or adopted on first use.

        :param acting_user: Falls back to the mission owner (projects are
            owner-private, so it should be the owner).
        :returns: ``None`` when projects are unavailable or the call failed.
        """
        api = self._api()
        if api is None:
            return None
        user = mission.owner_user_id or acting_user
        lock = self._locks.setdefault(mission.id, asyncio.Lock())
        async with lock:
            current = await self._svc.require_mission(mission.id)
            try:
                return await self._ensure(api, current, user)
            except _PROJECT_ERRORS as exc:  # best effort: never block a session start
                _logger.warning(
                    "shipcrew: no project for mission %s (%s); sessions stay unfiled",
                    mission.id,
                    exc,
                )
                return None

    async def _ensure(self, api: ProjectApi, mission: Mission, user: str | None) -> str:
        owned = await api.list_projects(acting_user=user)
        by_id = {p.id: p for p in owned}
        if mission.project_id and mission.project_id in by_id:
            return mission.project_id
        # Projects already linked to another mission of this owner are not adopted.
        linked = {
            m.project_id
            for m in await self._svc.list_missions()
            if m.project_id and m.id != mission.id
        }
        by_name = {p.name: p for p in owned}
        project: ProjectRef | None = None
        for name in candidate_names(mission.title):
            existing = by_name.get(name)
            if existing is not None:
                if existing.id not in linked:
                    project = existing  # a folder of the same name: adopt it
                    break
                continue
            try:
                project = await api.create_project(name, acting_user=user)
            except ProjectNameTaken:
                continue  # created concurrently elsewhere; try the next name
            break
        if project is None:
            raise SessionServiceError(f"no free project name for {mission.title!r}")
        await self._svc.set_mission_project(mission.id, project.id)
        return project.id

    async def rename(self, mission: Mission, acting_user: str | None = None) -> None:
        """Follow a mission rename; a name clash just keeps the old project name."""
        api = self._api()
        if api is None or not mission.project_id:
            return
        user = mission.owner_user_id or acting_user
        try:
            await api.rename_project(
                mission.project_id, project_base_name(mission.title), acting_user=user
            )
        except _PROJECT_ERRORS as exc:  # best effort
            _logger.info("shipcrew: project of mission %s not renamed (%s)", mission.id, exc)
