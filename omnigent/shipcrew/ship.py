"""The ship stage: deploy a finished mission to Vercel, verify it, write the report.

When every agent task of a mission is merged (and ``auto_ship`` is on), or on
``POST /missions/{id}/ship``, the scheduler:

1. **Checks** that nothing needs a human: a blocked or intervention card stops
   the ship, and the reason is stored in ``ship.error``. Human-assigned cards
   are ignored.
2. **Preflights** the ``vercel`` CLI on the server (``tools.resolve``, so
   ``SHIPCREW_VERCEL`` can point at a fake): not found or not logged in fails
   in a second instead of after an agent session.
3. **Deploys** through a session of the ``devops`` bundle, in a fresh worktree
   of ``origin/<base>`` on a throwaway ``shipcrew/<mission8>-ship-<stamp>``
   branch: ``vercel link --yes --project <repo-name>``, then
   ``vercel deploy --prod --yes``. Its final line is ``DEPLOYED: <url>`` (or
   ``FAIL: <reason>``, e.g. the names of missing env vars).
4. **Verifies** the URL itself, never trusting the agent: HTTP GET with
   redirects; 2xx is live, 401/403 is live but protected (with a note). It
   retries every ``ship_verify_interval_s`` until ``ship_verify_until`` (a
   stored deadline, so a restart resumes the same window).
5. **Reports**: :func:`omnigent.shipcrew.report.build_report` into
   ``ship.report_md``, for a failed ship too.

Every step is re-derived from the DB row and the session on each tick, so a
server restart resumes where it stopped. The devops session and its worktree
are removed as soon as its reply is read.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.shipcrew import tools
from omnigent.shipcrew.branches import task_branch
from omnigent.shipcrew.decisions import parse_decisions
from omnigent.shipcrew.pr_loop import (
    GitError,
    _cleanup_worktree,
    _fetch,
    default_base_ref,
    find_worktree,
)
from omnigent.shipcrew.report import build_report
from omnigent.shipcrew.sessions import RootSessionRequest, SessionServiceError, SessionSnapshot
from omnigent.shipcrew.store import Mission, Task

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)

DEVOPS_ROLE = "devops"
SHIP_LABEL_KEY = "shipcrew.ship"
MISSION_LABEL_KEY = "shipcrew.mission_id"
SHIPPING_STATUSES = frozenset({"deploying", "verifying"})
# Card states only a human can move on: they stop a ship.
STUCK_STATUSES = frozenset({"blocked", "intervention"})
_PREFLIGHT_TIMEOUT_S = 30.0
_PROBE_TIMEOUT_S = 10.0
_MAX_REDIRECTS = 10
_PROJECT_MAX = 100


def _conflict(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.CONFLICT)


# ── Readiness ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Readiness:
    """Whether a mission may ship now.

    :param stuck: True when the reason is a card that needs a human (blocked /
        intervention), as opposed to work still in progress.
    """

    ok: bool
    reason: str | None = None
    stuck: bool = False


def _titles(tasks: Sequence[Task], limit: int = 3) -> str:
    shown = ", ".join(repr(t.title) for t in tasks[:limit])
    return shown + (f" (+{len(tasks) - limit} more)" if len(tasks) > limit else "")


def ship_readiness(tasks: Sequence[Task]) -> Readiness:
    """Ship only when every agent task is merged and no card waits on a human."""
    agent = [t for t in tasks if not t.human_assigned]
    if not agent:
        return Readiness(False, "the mission has no agent task to ship")
    stuck = [t for t in agent if t.status in STUCK_STATUSES]
    if stuck:
        noun = "card needs" if len(stuck) == 1 else "cards need"
        return Readiness(
            False, f"not shipping: {len(stuck)} {noun} a human: {_titles(stuck)}", stuck=True
        )
    pending = [t for t in agent if t.status != "merged"]
    if pending:
        noun = "task is" if len(pending) == 1 else "tasks are"
        return Readiness(False, f"{len(pending)} {noun} not merged yet: {_titles(pending)}")
    return Readiness(True)


# ── Names, prompt, reply ────────────────────────────────────────

_GITHUB_NAME = re.compile(r"[/:](?P<name>[^/:]+?)(?:\.git)?/?$")


def vercel_project_name(repo_url: str | None, repo_path: str) -> str:
    """A valid Vercel project name from the repo name (lowercase, ``[a-z0-9._-]``)."""
    raw = ""
    if repo_url:
        match = _GITHUB_NAME.search(repo_url.strip())
        raw = match.group("name") if match else ""
    raw = raw or Path(repo_path).name
    name = re.sub(r"[^a-z0-9._-]+", "-", raw.lower())
    name = re.sub(r"-{2,}", "-", name).strip("-._")[:_PROJECT_MAX].strip("-._")
    return name or "shipcrew-app"


def ship_prompt(mission: Mission, project: str) -> str:
    """The devops session's only message."""
    repo = mission.repo_url or mission.repo_path
    return "\n".join(
        [
            f"# Ship: {mission.title}",
            "",
            "Every task of this mission is merged. Your cwd is a fresh worktree of `main` "
            f"of {repo}.",
            "Deploy it to Vercel production, nothing else:",
            "",
            "1. `vercel whoami` (not logged in: stop with `FAIL: vercel is not logged in`).",
            f"2. `vercel link --yes --project {project}`",
            "3. `vercel deploy --prod --yes` (Vercel builds remotely: do not build locally).",
            "4. If the deployment fails, read `vercel inspect <deployment-url> --logs` and report",
            "   the cause. If the build needs environment variables, list their NAMES in the FAIL",
            "   reason; never invent values or secrets.",
            "",
            "Do not edit files, do not commit, do not push. The server checks the URL itself.",
            "",
            "End your reply with a `Decisions:` list (what you chose without asking, one line",
            "each, or `Decisions: none`), then the final line, nothing after it:",
            "`DEPLOYED: <https production url>` (prefer the production alias Vercel prints,",
            "e.g. `https://<project>.vercel.app`) or `FAIL: <reason>`.",
        ]
    )


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip("*`_#> \t") if lines else ""


_DEPLOYED = re.compile(r"^\W*DEPLOYED\W*:?\s*<?`?(?P<url>\S+?)`?>?[.)]*$", re.IGNORECASE)


def parse_deploy_reply(text: str | None) -> tuple[str, str] | None:
    """``("deployed", url)``, ``("fail", reason)`` or ``None`` (no verdict).

    The final line wins; a ``DEPLOYED:`` line further up counts when the final
    line is no verdict (an agent adding a sign-off).
    """
    line = _last_line(text)
    fail = re.match(r"FAIL\b[\s:.-]*(.*)", line, re.IGNORECASE)
    if fail:
        return "fail", fail.group(1).strip() or "no reason given"
    for candidate in [line, *reversed([ln.strip() for ln in (text or "").splitlines()])]:
        match = _DEPLOYED.match(candidate.strip("*_#> \t"))
        if match:
            return "deployed", match.group("url")
    return None


def check_url(url: str, *, allow_private: bool = False) -> str | None:
    """Why ``url`` cannot be the deployment URL, or ``None`` when it can.

    Only ``https`` to a public host name: the agent's URL is untrusted, so the
    server never probes loopback, private networks or IP literals with it.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"not a URL: {url[:200]!r}"
    if parts.scheme not in (("https", "http") if allow_private else ("https",)):
        return f"the deployment URL must be https: {url[:200]!r}"
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        return f"not a plain https URL: {url[:200]!r}"
    if allow_private:
        return None
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return f"the deployment URL points at a local host: {host}"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None if "." in host else f"not a public host name: {host}"
    return f"the deployment URL is an IP address, not a host name: {host}"


# ── URL check ───────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :func:`verify_url`.

    :param status: The last HTTP status, ``None`` when no answer came.
    :param note: Set for a protected (401/403) deployment.
    """

    ok: bool
    attempts: int
    status: int | None = None
    note: str | None = None
    error: str | None = None
    final_url: str | None = None


async def probe_url(url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> tuple[int | None, str, str]:
    """One GET with redirects: ``(status or None, detail, final url)``."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, max_redirects=_MAX_REDIRECTS, timeout=timeout_s
        ) as client:
            response = await client.get(url, headers={"User-Agent": "shipcrew-ship-check/1"})
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300], url
    return response.status_code, response.reason_phrase or "", str(response.url)


async def verify_url(
    url: str,
    *,
    until: float,
    interval_s: float,
    probe: Callable[[str], Awaitable[tuple[int | None, str, str]]] = probe_url,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> VerifyResult:
    """Retry ``url`` until it answers 2xx (or 401/403) or ``until`` passes.

    At least one attempt is made, even past the deadline (a restart after it).
    """
    attempts = 0
    last_status: int | None = None
    last_detail = ""
    while True:
        attempts += 1
        status, detail, final = await probe(url)
        last_status, last_detail = status, detail
        if status is not None and 200 <= status < 300:
            return VerifyResult(True, attempts, status, final_url=final)
        if status in (401, 403):
            note = (
                f"The deployment answers HTTP {status}: it is live but protected "
                "(Vercel Deployment Protection). Open it logged in to Vercel, or turn "
                "protection off for production."
            )
            return VerifyResult(True, attempts, status, note=note, final_url=final)
        if clock() + interval_s > until:
            break
        await sleep(interval_s)
    got = f"HTTP {last_status}" if last_status is not None else (last_detail or "no answer")
    return VerifyResult(
        False,
        attempts,
        last_status,
        error=f"{url} did not answer 2xx after {attempts} attempts (last: {got})",
    )


# ── vercel preflight ────────────────────────────────────────────


def _vercel_tool() -> tools.Tool:
    return next(t for t in tools.registry() if t.key == "VERCEL")


def vercel_preflight() -> str | None:
    """Why the ``vercel`` CLI cannot deploy from this machine, or ``None``."""
    path = tools.resolve(_vercel_tool())
    if path is None:
        return "vercel CLI not found: npm i -g vercel (or set SHIPCREW_VERCEL)"
    try:
        run = subprocess.run(
            [path, "whoami"],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT_S,
            env=tools.session_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"vercel whoami failed: {exc}"
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        tail = f" ({detail[-1][:200]})" if detail else ""
        return f"vercel is not logged in on the server machine: run `vercel login`{tail}"
    return None


# ── The runner ──────────────────────────────────────────────────


class ShipRunner:
    """Starts, polls and verifies ship runs. See the module docstring.

    :param service: The board service (store, sessions, event bus, settings).
    :param preflight: Server-side CLI check; tests replace it.
    :param probe: One HTTP check of a URL; tests replace it.
    """

    def __init__(
        self,
        service: ShipcrewService,
        *,
        preflight: Callable[[], str | None] = vercel_preflight,
        probe: Callable[[str], Awaitable[tuple[int | None, str, str]]] = probe_url,
    ) -> None:
        self._svc = service
        self._busy: set[str] = set()
        self._verifying: dict[str, asyncio.Task[None]] = {}
        self.preflight = preflight
        self.probe = probe

    # ── plumbing ──

    async def _set(self, mission_id: str, **fields: Any) -> Mission:
        mission = await asyncio.to_thread(self._svc.store.update_mission, mission_id, **fields)
        if mission is None:
            raise OmnigentError(f"mission {mission_id!r} not found", code=ErrorCode.NOT_FOUND)
        self._svc.bus.mission_updated(mission)
        return mission

    async def _tasks(self, mission_id: str) -> list[Task]:
        return await asyncio.to_thread(self._svc.store.list_tasks, mission_id)

    async def _end(self, mission: Mission, status: str, **fields: Any) -> Mission:
        """Finish the run (``done`` / ``failed``): session, worktree, report."""
        await self._stop_session(mission)
        await self._remove_worktree(mission)
        final = dataclasses.replace(
            mission,
            ship_status=status,
            ship_finished_at=time.time(),
            ship_session_id=None,
            ship_verify_until=None,
            **fields,
        )
        report = build_report(final, await self._tasks(mission.id))
        update: dict[str, Any] = {
            "ship_status": status,
            "ship_finished_at": final.ship_finished_at,
            "ship_session_id": None,
            "ship_verify_until": None,
            "ship_report_md": report,
            **fields,
        }
        if status == "done":
            update["status"] = "done"
        return await self._set(mission.id, **update)

    async def _fail(self, mission: Mission, error: str) -> Mission:
        _logger.info("shipcrew: ship of mission %s failed: %s", mission.id, error)
        return await self._end(mission, "failed", ship_error=error[:2000])

    async def _stop_session(self, mission: Mission) -> None:
        if mission.ship_session_id is None:
            return
        try:
            await self._svc.sessions.stop(
                mission.ship_session_id, acting_user=mission.owner_user_id
            )
        except SessionServiceError:
            _logger.warning("shipcrew: could not stop the ship session of %s", mission.id)

    async def _remove_worktree(self, mission: Mission) -> None:
        if not mission.ship_branch:
            return
        repo = Path(mission.repo_path)
        if not repo.is_dir():
            return
        base = self._svc.settings.pr_base

        def _cleanup() -> None:
            try:
                worktree = find_worktree(repo, mission.ship_branch or "")
                _cleanup_worktree(repo, worktree, mission.ship_branch or "", base)
            except GitError as exc:
                _logger.warning("shipcrew: ship worktree cleanup failed: %s", exc)

        await asyncio.to_thread(_cleanup)

    # ── start ──

    async def start(self, mission_id: str, acting_user: str | None, *, manual: bool) -> Mission:
        """Start a ship run (``ship.status = deploying``).

        :param manual: ``POST /ship``: a mission that cannot ship raises
            ``CONFLICT`` with the reason, and a finished ship may run again.
        :raises OmnigentError: ``CONFLICT`` while a ship runs or (manual) when
            the mission is not ready.
        """
        if mission_id in self._busy:
            raise _conflict("a ship is already starting")
        self._busy.add(mission_id)
        try:
            mission = await self._svc.require_mission(mission_id)
            if mission.ship_status in SHIPPING_STATUSES:
                raise _conflict(f"the mission is already shipping ({mission.ship_status})")
            readiness = ship_readiness(await self._tasks(mission.id))
            if not readiness.ok:
                if manual:
                    raise _conflict(readiness.reason or "the mission is not ready to ship")
                return mission
            now = time.time()
            branch = task_branch(mission.id, f"ship {int(now)}")
            mission = await self._set(
                mission.id,
                ship_status="deploying",
                ship_started_at=now,
                ship_finished_at=None,
                ship_url=None,
                ship_error=None,
                ship_note=None,
                ship_report_md=None,
                ship_session_id=None,
                ship_verify_until=None,
                ship_decisions=[],
                ship_cost_usd=0.0,
                ship_branch=None,
            )
            agent_dir = self._svc.settings.agents_dir / DEVOPS_ROLE
            if not (agent_dir / "config.yaml").is_file():
                return await self._fail(
                    mission,
                    f"no agent bundle for role {DEVOPS_ROLE!r} in {self._svc.settings.agents_dir}",
                )
            problem = await asyncio.to_thread(self.preflight)
            if problem is not None:
                return await self._fail(mission, problem)
            repo = Path(mission.repo_path)
            base = self._svc.settings.pr_base
            if repo.is_dir():
                await asyncio.to_thread(_fetch, repo, base)
            base_ref = await asyncio.to_thread(default_base_ref, mission.repo_path, base)
            project = vercel_project_name(mission.repo_url, mission.repo_path)
            mission = await self._set(mission.id, ship_branch=branch)
            request = RootSessionRequest(
                task_id=f"ship-{mission.id}",
                title=f"Ship: {mission.title}",
                prompt=ship_prompt(mission, project),
                repo_path=mission.repo_path,
                branch=branch,
                agent_dir=agent_dir,
                acting_user=acting_user or mission.owner_user_id,
                base_branch=base_ref,
                labels={
                    MISSION_LABEL_KEY: mission.id,
                    "shipcrew.role": DEVOPS_ROLE,
                    SHIP_LABEL_KEY: "deploy",
                },
                project_id=await self._svc.mission_project_id(mission, acting_user),
                harness=self._svc.settings.harness_for(DEVOPS_ROLE),
            )
            try:
                session_id = await self._svc.sessions.create_root_session(request)
            except Exception as exc:
                if not isinstance(exc, SessionServiceError):
                    _logger.exception("shipcrew: ship start failed for mission %s", mission.id)
                return await self._fail(mission, str(exc) or type(exc).__name__)
            return await self._set(mission.id, ship_session_id=session_id)
        finally:
            self._busy.discard(mission_id)

    # ── tick ──

    async def tick(self) -> None:
        """Scheduler hook: auto-ship ready missions, poll deploys, run URL checks."""
        missions = await asyncio.to_thread(self._svc.store.list_missions)
        tasks = await asyncio.to_thread(self._svc.store.list_tasks)
        by_mission: dict[str, list[Task]] = {}
        for task in tasks:
            by_mission.setdefault(task.mission_id, []).append(task)
        steps = [self._step_safely(m, by_mission.get(m.id, [])) for m in missions]
        await asyncio.gather(*steps)

    async def _step_safely(self, mission: Mission, tasks: list[Task]) -> None:
        if mission.id in self._busy:
            return
        try:
            await self._step(mission, tasks)
        except (OmnigentError, SessionServiceError, GitError) as exc:
            _logger.warning("shipcrew ship: mission %s: %s", mission.id, exc)
        except Exception:
            _logger.exception("shipcrew ship: mission %s crashed", mission.id)

    async def _step(self, mission: Mission, tasks: list[Task]) -> None:
        if mission.ship_status == "deploying":
            await self._poll_deploy(mission)
        elif mission.ship_status == "verifying":
            self._ensure_verifying(mission)
        elif mission.ship_status == "idle":
            await self._maybe_auto_ship(mission, tasks)

    async def _maybe_auto_ship(self, mission: Mission, tasks: list[Task]) -> None:
        if not (self._svc.settings.ship_enabled and mission.auto_ship):
            return
        if mission.plan_status == "running":
            return
        readiness = ship_readiness(tasks)
        if readiness.ok:
            await self.start(mission.id, None, manual=False)
            return
        # Say why a finished-looking mission does not ship; clear it otherwise.
        wanted = readiness.reason if readiness.stuck else None
        if mission.ship_error != wanted:
            await self._set(mission.id, ship_error=wanted)

    async def _poll_deploy(self, mission: Mission) -> None:
        if mission.ship_session_id is None:
            # Re-read: a start may have finished since this tick listed the missions.
            mission = await self._svc.require_mission(mission.id)
            if mission.id in self._busy or mission.ship_status != "deploying":
                return
            if mission.ship_session_id is None:
                # Not starting any more, so a restart cut the start off.
                await self._fail(
                    mission, "the ship start was interrupted (server restart); ship again"
                )
                return
        session_id = mission.ship_session_id
        assert session_id is not None
        snap = await self._svc.sessions.snapshot(session_id, acting_user=mission.owner_user_id)
        cost = snap.cost_usd if snap is not None else None
        if cost is not None and cost != mission.ship_cost_usd:
            mission = await self._set(mission.id, ship_cost_usd=cost)
        if snap is None:
            await self._fail(mission, "the deploy session no longer exists")
            return
        if snap.status == "failed":
            await self._fail(mission, f"the deploy session failed: {snap.error or 'unknown'}")
            return
        if not _finished(snap):
            return
        text = await self._svc.sessions.last_agent_text(
            session_id, acting_user=mission.owner_user_id
        )
        decisions = parse_decisions(text)
        reply = parse_deploy_reply(text)
        if reply is None:
            await self._end(
                mission,
                "failed",
                ship_error="the devops agent ended without a DEPLOYED: <url> or FAIL line",
                ship_decisions=decisions,
            )
            return
        kind, value = reply
        if kind == "fail":
            error = f"deploy failed: {value}"[:2000]
            await self._end(mission, "failed", ship_error=error, ship_decisions=decisions)
            return
        problem = check_url(value, allow_private=self._svc.settings.ship_allow_private_urls)
        if problem is not None:
            await self._end(mission, "failed", ship_error=problem, ship_decisions=decisions)
            return
        # The agent is done: free its process and worktree before the URL check.
        await self._stop_session(mission)
        await self._remove_worktree(mission)
        mission = await self._set(
            mission.id,
            ship_status="verifying",
            ship_url=value,
            ship_decisions=decisions,
            ship_session_id=None,
            ship_branch=None,
            ship_verify_until=time.time() + self._svc.settings.ship_verify_s,
        )
        self._ensure_verifying(mission)

    # ── verification ──

    def _ensure_verifying(self, mission: Mission) -> None:
        running = self._verifying.get(mission.id)
        if running is not None and not running.done():
            return
        self._verifying[mission.id] = asyncio.create_task(
            self._verify(mission.id), name=f"shipcrew-ship-verify-{mission.id[:8]}"
        )

    async def _verify(self, mission_id: str) -> None:
        try:
            mission = await self._svc.require_mission(mission_id)
            if mission.ship_status != "verifying" or not mission.ship_url:
                return
            settings = self._svc.settings
            result = await verify_url(
                mission.ship_url,
                until=mission.ship_verify_until or time.time(),
                interval_s=settings.ship_verify_interval_s,
                probe=self.probe,
            )
            current = await self._svc.require_mission(mission_id)
            if current.ship_status != "verifying":
                return  # changed meanwhile (a new ship run)
            if result.ok:
                await self._end(current, "done", ship_note=result.note, ship_error=None)
            else:
                await self._fail(current, result.error or "the deployment URL did not answer")
        except Exception:
            _logger.exception("shipcrew ship: URL check of mission %s crashed", mission_id)

    async def drain(self) -> None:
        """Wait for the in-flight URL checks (tests, shutdown)."""
        pending = [t for t in self._verifying.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Cancel the in-flight URL checks; a restart resumes them from the DB."""
        for task in self._verifying.values():
            task.cancel()
        await self.drain()
        self._verifying.clear()


def _finished(snap: SessionSnapshot) -> bool:
    return snap.status == "idle" and snap.agent_replied and not snap.awaiting_human
