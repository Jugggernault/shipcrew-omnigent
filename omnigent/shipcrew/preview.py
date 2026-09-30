"""The mission preview: ``main`` live on a public URL from the first merge on.

For a server-side deploy target (``docker``, ``argocd``; see
:mod:`omnigent.shipcrew.deploy_targets`), every scheduler tick:

1. **Triggers** on the set of merged tasks: a new merge (or the first one)
   means ``main`` moved. The first deploy starts right away; later ones wait
   ``preview_debounce_s`` after the last merge, so a burst of merges deploys
   once.
2. **Deploys** one at a time per mission (a per-mission lock, plus
   ``deploy_parallel`` builds across missions), from a clean detached
   worktree of ``origin/<base>`` at its current commit, removed afterwards.
   ``main`` unchanged since the live deploy only marks the merge as seen.
3. **Verifies** the public URL itself (the ship stage's
   :func:`~omnigent.shipcrew.ship.verify_url`) before calling it live.
4. **Stores** ``Mission.preview`` = ``{status, url, sha, updated_at,
   live_since, error, target, deploying_sha, merged_key, detail}`` and
   publishes ``mission.updated``: the board shows "Live: <url>".
   A failed redeploy keeps the previous version serving and says so.
5. **Keeps the URL alive**: ``target.refresh`` restarts a dead tunnel; a new
   URL is stored.

Restart-safe: ``merged_key`` is only written when a deploy ends, so a deploy
cut off by a restart runs again on the next tick (targets are idempotent).
The ship stage's last redeploy goes through :meth:`PreviewRunner.deploy_now`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.shipcrew.deploy_targets import DeployContext, DeployError, mission_slug
from omnigent.shipcrew.pr_loop import GitError, _fetch, _git, _rev
from omnigent.shipcrew.store import Mission, Task

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)

REFRESH_EVERY_S = 15.0
_SHIPPING = frozenset({"deploying", "verifying"})


@dataclass(frozen=True)
class PreviewOutcome:
    """How a deploy ended. ``url`` is set when the app is reachable (maybe an older sha)."""

    ok: bool
    url: str | None = None
    sha: str | None = None
    error: str | None = None
    note: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def merged_key(tasks: Sequence[Task]) -> str | None:
    """A fingerprint of the mission's merged tasks, ``None`` when none is merged."""
    ids = sorted(t.id for t in tasks if t.status == "merged")
    if not ids:
        return None
    return hashlib.sha1(",".join(ids).encode()).hexdigest()[:16]


class PreviewRunner:
    """Continuous deploys of server-side targets. See the module docstring.

    :param probe: One HTTP check of a URL (``ship.probe_url``); tests replace it.
    """

    def __init__(
        self,
        service: ShipcrewService,
        *,
        probe: Callable[[str], Awaitable[tuple[int | None, str, str]]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._svc = service
        self._inflight: dict[str, asyncio.Task[PreviewOutcome]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sem: asyncio.Semaphore | None = None
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._last_refresh: dict[str, float] = {}
        self.probe = probe
        self.clock = clock

    # ── plumbing ──

    def _server_side(self) -> Any | None:
        target = self._svc.deploy_target()
        return target if getattr(target, "server_side", False) else None

    def _lock(self, mission_id: str) -> asyncio.Lock:
        return self._locks.setdefault(mission_id, asyncio.Lock())

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._svc.settings.deploy_parallel)
        return self._sem

    async def _set(self, mission_id: str, **fields: Any) -> Mission:
        """Merge ``fields`` into ``Mission.preview`` (``None`` removes a key)."""
        mission = await self._svc.require_mission(mission_id)
        preview = {**mission.preview, **fields}
        preview = {k: v for k, v in preview.items() if v is not None}
        updated = await asyncio.to_thread(
            self._svc.store.update_mission, mission_id, preview=preview
        )
        assert updated is not None
        self._svc.bus.mission_updated(updated)
        return updated

    def busy(self, mission_id: str) -> bool:
        task = self._inflight.get(mission_id)
        return task is not None and not task.done()

    # ── tick ──

    async def tick(self) -> None:
        """Scheduler hook: start due deploys, keep live URLs up."""
        if not self._svc.settings.preview_enabled or self._server_side() is None:
            return
        missions = await asyncio.to_thread(self._svc.store.list_missions)
        tasks = await asyncio.to_thread(self._svc.store.list_tasks)
        by_mission: dict[str, list[Task]] = {}
        for task in tasks:
            by_mission.setdefault(task.mission_id, []).append(task)
        for mission in missions:
            try:
                await self._step(mission, by_mission.get(mission.id, []))
            except Exception:
                _logger.exception("shipcrew preview: mission %s crashed", mission.id)

    async def _step(self, mission: Mission, tasks: list[Task]) -> None:
        if self.busy(mission.id) or mission.ship_status in _SHIPPING:
            return
        if mission.preview.get("stopped"):
            return
        key = merged_key(tasks)
        if key is None:
            return
        preview = mission.preview
        if preview.get("merged_key") == key:
            self._maybe_refresh(mission)
            return
        if preview.get("url"):
            # Debounce every redeploy (never the first deploy).
            now = self.clock()
            if preview.get("pending_key") != key:
                await self._set(mission.id, pending_key=key, requested_at=now)
                return
            requested = float(preview.get("requested_at") or 0.0)
            if now - requested < self._svc.settings.preview_debounce_s:
                return
        self.launch(mission.id, key)

    def launch(
        self, mission_id: str, key: str | None, *, final: bool = False
    ) -> asyncio.Task[PreviewOutcome]:
        """Start a deploy in the background (at most one per mission)."""
        running = self._inflight.get(mission_id)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(
            self._deploy(mission_id, key, final=final),
            name=f"shipcrew-preview-{mission_id[:8]}",
        )
        self._inflight[mission_id] = task
        return task

    async def deploy_now(self, mission_id: str, *, final: bool = False) -> PreviewOutcome:
        """Deploy the current ``main`` now (after any running deploy): the ship stage."""
        running = self._inflight.get(mission_id)
        if running is not None and not running.done():
            with contextlib.suppress(Exception):
                await running
        tasks = await asyncio.to_thread(self._svc.store.list_tasks, mission_id)
        return await self.launch(mission_id, merged_key(tasks), final=final)

    # ── deploy ──

    async def _deploy(self, mission_id: str, key: str | None, *, final: bool) -> PreviewOutcome:
        async with self._lock(mission_id), self._semaphore():
            try:
                return await self._deploy_locked(mission_id, key, final=final)
            except Exception as exc:
                _logger.exception("shipcrew preview: deploy of %s crashed", mission_id)
                error = f"deploy crashed: {type(exc).__name__}: {exc}"[:2000]
                with contextlib.suppress(Exception):
                    await self._set(mission_id, status="failed", error=error, merged_key=key)
                return PreviewOutcome(False, error=error)

    async def _deploy_locked(
        self, mission_id: str, key: str | None, *, final: bool
    ) -> PreviewOutcome:
        target = self._server_side()
        if target is None:
            return PreviewOutcome(False, error="the deploy target is not server-side")
        mission = await self._svc.require_mission(mission_id)
        before = mission.preview
        problem = await asyncio.to_thread(target.preflight)
        if problem is not None:
            return await self._failed(mission_id, key, problem, before, kept=False)
        repo = Path(mission.repo_path)
        base = self._svc.settings.pr_base
        sha = await asyncio.to_thread(_main_sha, repo, base)
        if sha is None:
            return await self._failed(
                mission_id, key, f"no commit to deploy: {repo} has no {base}", before, kept=False
            )
        await self._set(
            mission_id, status="deploying", deploying_sha=sha, target=target.name,
            pending_key=None,
        )  # fmt: skip
        slug = mission_slug(mission)
        worktree = self._svc.settings.deploy_state_dir / "worktrees" / f"{slug}-{sha[:12]}"
        try:
            await asyncio.to_thread(_add_worktree, repo, worktree, sha)
        except GitError as exc:
            return await self._failed(
                mission_id, key, str(exc), before, kept=bool(before.get("url"))
            )
        ctx = DeployContext(
            mission_id=mission.id,
            slug=slug,
            title=mission.title,
            repo_path=repo,
            repo_url=mission.repo_url,
            sha=sha,
            worktree=worktree,
            base=base,
            final=final,
        )
        started = self.clock()
        try:
            result = await asyncio.to_thread(target.deploy, ctx)
        except DeployError as exc:
            kept = exc.kept_previous and bool(before.get("url"))
            return await self._failed(mission_id, key, str(exc), before, kept=kept)
        finally:
            await asyncio.to_thread(_remove_worktree, repo, worktree)
        verdict = await self._verify(result.url)
        now = self.clock()
        detail = {**result.detail, "deploy_s": round(now - started, 1)}
        if verdict is not None:
            await self._set(
                mission_id,
                status="failed",
                url=result.url,
                deploying_sha=None,
                error=verdict,
                merged_key=key,
                detail=detail,
            )
            return PreviewOutcome(False, url=result.url, sha=sha, error=verdict, detail=detail)
        await self._set(
            mission_id,
            status="live",
            url=result.url,
            sha=sha,
            updated_at=now,
            live_since=before.get("live_since") or now,
            error=None,
            deploying_sha=None,
            merged_key=key,
            note=result.note,
            detail=detail,
        )
        _logger.info("shipcrew preview: %s live at %s (%s)", mission_id, result.url, sha[:12])
        return PreviewOutcome(True, url=result.url, sha=sha, note=result.note, detail=detail)

    async def _failed(
        self,
        mission_id: str,
        key: str | None,
        error: str,
        before: dict[str, Any],
        *,
        kept: bool,
    ) -> PreviewOutcome:
        """Record a failed deploy; with ``kept`` the previous version still serves."""
        error = error[:2000]
        _logger.info("shipcrew preview: deploy of %s failed: %s", mission_id, error)
        status = "live" if kept and before.get("status") == "live" else "failed"
        await self._set(mission_id, status=status, error=error, deploying_sha=None, merged_key=key)
        url = before.get("url") if kept else None
        return PreviewOutcome(False, url=url, sha=before.get("sha") if kept else None, error=error)

    async def _verify(self, url: str) -> str | None:
        """Why the public URL is not live (after the retry window), or ``None``."""
        from omnigent.shipcrew.ship import check_url, probe_url, verify_url

        settings = self._svc.settings
        problem = check_url(url, allow_private=settings.ship_allow_private_urls)
        if problem is not None:
            return problem
        result = await verify_url(
            url,
            until=time.time() + settings.ship_verify_s,
            interval_s=settings.ship_verify_interval_s,
            probe=self.probe or probe_url,
        )
        return None if result.ok else (result.error or "the public URL did not answer")

    # ── keep the URL alive ──

    def _maybe_refresh(self, mission: Mission) -> None:
        if not mission.preview.get("url") or mission.preview.get("status") == "deploying":
            return
        now = self.clock()
        if now - self._last_refresh.get(mission.id, 0.0) < REFRESH_EVERY_S:
            return
        running = self._refreshing.get(mission.id)
        if running is not None and not running.done():
            return
        self._last_refresh[mission.id] = now
        self._refreshing[mission.id] = asyncio.create_task(
            self.refresh(mission.id), name=f"shipcrew-preview-refresh-{mission.id[:8]}"
        )

    async def refresh(self, mission_id: str) -> None:
        """Restart a dead tunnel; store the URL when it changed."""
        target = self._server_side()
        if target is None:
            return
        async with self._lock(mission_id):
            mission = await self._svc.require_mission(mission_id)
            try:
                url = await asyncio.to_thread(target.refresh, mission)
            except Exception:
                _logger.exception("shipcrew preview: refresh of %s crashed", mission_id)
                return
            if url and url != mission.preview.get("url"):
                _logger.info("shipcrew preview: %s has a new URL %s", mission_id, url)
                updated = await self._set(mission_id, url=url, updated_at=self.clock())
                if updated.ship_url and updated.ship_status == "done":
                    await asyncio.to_thread(
                        self._svc.store.update_mission, mission_id, ship_url=url
                    )

    async def teardown(self, mission_id: str) -> Mission:
        """Stop the mission's preview (containers, tunnel, images); no redeploy after."""
        mission = await self._svc.require_mission(mission_id)
        target = self._server_side()
        async with self._lock(mission_id):
            if target is not None:
                await asyncio.to_thread(target.teardown, mission)
            preview = {"status": "idle", "stopped": True}
            updated = await asyncio.to_thread(
                self._svc.store.update_mission, mission_id, preview=preview
            )
        assert updated is not None
        self._svc.bus.mission_updated(updated)
        return updated

    async def resume(self, mission_id: str) -> Mission:
        """Deploy again after a teardown (and right now)."""
        mission = await self._svc.require_mission(mission_id)
        preview = {k: v for k, v in mission.preview.items() if k not in ("stopped", "merged_key")}
        updated = await asyncio.to_thread(
            self._svc.store.update_mission, mission_id, preview=preview
        )
        assert updated is not None
        tasks = await asyncio.to_thread(self._svc.store.list_tasks, mission_id)
        self.launch(mission_id, merged_key(tasks))
        return updated

    async def drain(self) -> None:
        """Wait for the running deploys and refreshes (tests, shutdown)."""
        pending = [
            t for t in [*self._inflight.values(), *self._refreshing.values()] if not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        for task in [*self._inflight.values(), *self._refreshing.values()]:
            task.cancel()
        await self.drain()


def _main_sha(repo: Path, base: str) -> str | None:
    if not repo.is_dir():
        return None
    _fetch(repo, base)
    return _rev(repo, f"origin/{base}") or _rev(repo, base)


def _add_worktree(repo: Path, worktree: Path, sha: str) -> None:
    if worktree.exists():
        _remove_worktree(repo, worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "--detach", "--force", str(worktree), sha], repo)


def _remove_worktree(repo: Path, worktree: Path) -> None:
    with contextlib.suppress(GitError):
        _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)
        _git(["worktree", "prune"], repo, check=False)
    if worktree.exists():
        import shutil

        shutil.rmtree(worktree, ignore_errors=True)
