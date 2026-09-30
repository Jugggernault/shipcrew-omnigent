"""The mission preview: early deploy, redeploy on merge, debounce, the ship stage, restarts."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine

from omnigent.shipcrew.deploy_targets import DeployContext, DeployError, DeployResult
from omnigent.shipcrew.events import MissionEventBus
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.store import Mission, ShipcrewStore, Task

from .conftest import FakeSessions
from .deploy_fakes import DeployFakes

P = "/v1/shipcrew"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def push(repo: Path, name: str, text: str) -> str:
    """Commit on main and push it to origin: what a PR merge does."""
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
    _git(repo, "push", "-q", "origin", "main")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    path = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(path))
    _git(path, "checkout", "-q", "-b", "main")
    push(path, "Dockerfile", "FROM scratch\n")
    return path


@dataclass
class FakeTarget:
    """A server-side target recording its deploys; ``gate`` holds one open."""

    name: str = "fake"
    server_side: bool = True
    deploys: list[DeployContext] = field(default_factory=list)
    fail: DeployError | None = None
    gate: asyncio.Event | None = None
    url: str = "http://127.0.0.1:9/preview"
    refreshed: list[str] = field(default_factory=list)
    torn_down: list[str] = field(default_factory=list)
    problem: str | None = None
    loop: asyncio.AbstractEventLoop | None = None

    def preflight(self) -> str | None:
        return self.problem

    def deploy(self, ctx: DeployContext) -> DeployResult:
        assert (ctx.worktree / "Dockerfile").is_file()  # a real checkout of main
        assert _git(ctx.worktree, "rev-parse", "HEAD") == ctx.sha
        self.deploys.append(ctx)
        if self.gate is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.gate.wait(), self.loop).result(timeout=10)
        if self.fail is not None:
            raise self.fail
        return DeployResult(self.url, detail={"build_s": 1.5, "image_mb": 42.0})

    def refresh(self, mission: Mission) -> str | None:
        self.refreshed.append(mission.id)
        return self.url

    def teardown(self, mission: Mission) -> None:
        self.torn_down.append(mission.id)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path: Path, agents_dir: Path) -> ShipcrewSettings:
    return ShipcrewSettings(
        agents_dir=agents_dir,
        scheduler_enabled=False,
        pr_loop_enabled=False,
        db_url=f"sqlite:///{tmp_path / 'shipcrew.db'}",
        ship_verify_s=1.0,
        ship_verify_interval_s=0.05,
        ship_allow_private_urls=True,
        preview_debounce_s=30.0,
        deploy_state_dir=tmp_path / "state",
    )


async def _ok_probe(url: str) -> tuple[int | None, str, str]:
    return 200, "OK", url


@pytest.fixture
def target(service: ShipcrewService) -> FakeTarget:
    fake = FakeTarget()
    service.set_deploy_target(fake)
    service.preview.probe = _ok_probe
    service.ship.probe = _ok_probe
    return fake


@pytest.fixture
def clock(service: ShipcrewService) -> Clock:
    c = Clock()
    service.preview.clock = c
    return c


async def _mission(service: ShipcrewService, repo: Path) -> Mission:
    return await service.create_mission("Polls", str(repo), "https://github.com/acme/polls", None)


async def _task(service: ShipcrewService, mission: Mission, title: str, **fields: Any) -> Task:
    task = await service.create_task(mission.id, title=title)
    updated = await asyncio.to_thread(service.store.update_task, task.id, **fields)
    assert updated is not None
    return updated


async def _merge(service: ShipcrewService, task: Task) -> None:
    await asyncio.to_thread(service.store.update_task, task.id, status="merged")


async def _preview(service: ShipcrewService, mission_id: str) -> dict[str, Any]:
    return (await service.require_mission(mission_id)).preview


async def _tick(service: ShipcrewService) -> None:
    await service.preview.tick()
    await service.preview.drain()


class TestEarlyAndContinuousDeploy:
    async def test_first_merge_deploys_right_away_and_publishes_the_url(
        self,
        service: ShipcrewService,
        target: FakeTarget,
        repo: Path,
        client: httpx.AsyncClient,
        clock: Clock,
    ) -> None:
        mission = await _mission(service, repo)
        foundation = await _task(service, mission, "Foundation", status="review")
        await _task(service, mission, "Polls API", status="running")
        await _tick(service)
        assert target.deploys == []  # nothing merged yet: nothing to show
        await _merge(service, foundation)
        async with service.bus.subscribe(mission.id) as queue:
            await _tick(service)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        (ctx,) = target.deploys
        head = _git(repo, "rev-parse", "HEAD")
        assert ctx.sha == head and ctx.slug.startswith("polls-") and not ctx.final
        preview = await _preview(service, mission.id)
        assert preview["status"] == "live"
        assert preview["url"] == target.url and preview["sha"] == head
        assert preview["live_since"] == preview["updated_at"] == clock.now
        statuses = [e["mission"]["preview"]["status"] for e in events if "mission" in e]
        assert statuses[:1] == ["deploying"] and statuses[-1] == "live"
        # The board sees it in the mission API.
        body = (await client.get(f"{P}/missions")).json()["missions"][0]["preview"]
        assert body["url"] == target.url and body["status"] == "live" and body["sha"] == head
        # The worktree is gone once the deploy is done.
        assert not ctx.worktree.exists()

    async def test_later_merges_redeploy_after_the_debounce_once(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, clock: Clock
    ) -> None:
        mission = await _mission(service, repo)
        a = await _task(service, mission, "A", status="merged")
        b = await _task(service, mission, "B", status="review")
        c = await _task(service, mission, "C", status="review")
        await _tick(service)
        first = await _preview(service, mission.id)
        push(repo, "b.txt", "b")
        await _merge(service, b)
        await _tick(service)  # starts the debounce window
        clock.now += 10
        push(repo, "c.txt", "c")
        await _merge(service, c)
        await _tick(service)  # a new merge restarts the window
        assert len(target.deploys) == 1
        clock.now += 29
        await _tick(service)
        assert len(target.deploys) == 1
        clock.now += 2
        await _tick(service)
        assert len(target.deploys) == 2  # the two merges deploy once
        preview = await _preview(service, mission.id)
        assert preview["sha"] == _git(repo, "rev-parse", "HEAD")
        assert preview["live_since"] == first["live_since"]  # first deploy time kept
        assert preview["updated_at"] == clock.now
        await _tick(service)
        assert len(target.deploys) == 2  # nothing new
        assert a.id

    async def test_one_deploy_at_a_time_per_mission(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, clock: Clock
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        b = await _task(service, mission, "B", status="review")
        target.gate = asyncio.Event()
        target.loop = asyncio.get_running_loop()
        await service.preview.tick()
        await asyncio.sleep(0.2)
        assert service.preview.busy(mission.id)
        assert (await _preview(service, mission.id))["status"] == "deploying"
        push(repo, "b.txt", "b")
        await _merge(service, b)
        clock.now += 100
        for _ in range(3):
            await service.preview.tick()
        assert len(target.deploys) == 1  # still the first one
        target.gate.set()
        await service.preview.drain()
        await _tick(service)  # the merge seen during the deploy: debounce starts
        clock.now += 31
        await _tick(service)
        assert len(target.deploys) == 2

    async def test_a_failed_redeploy_keeps_the_live_version(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, clock: Clock
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        b = await _task(service, mission, "B", status="review")
        await _tick(service)
        good = await _preview(service, mission.id)
        target.fail = DeployError("the new container exited (code 1)", kept_previous=True)
        push(repo, "b.txt", "b")
        await _merge(service, b)
        await _tick(service)
        clock.now += 31
        await _tick(service)
        preview = await _preview(service, mission.id)
        assert preview["status"] == "live"
        assert preview["url"] == good["url"] and preview["sha"] == good["sha"]
        assert "exited" in preview["error"]
        await _tick(service)
        assert len(target.deploys) == 2  # no retry loop: the next merge retries

    async def test_first_deploy_failure_and_preflight(
        self, service: ShipcrewService, target: FakeTarget, repo: Path
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        target.problem = "the docker daemon is not reachable"
        await _tick(service)
        preview = await _preview(service, mission.id)
        assert preview["status"] == "failed" and "daemon" in preview["error"]
        assert target.deploys == []

    async def test_vercel_target_has_no_preview(
        self, service: ShipcrewService, repo: Path
    ) -> None:
        assert service.deploy_target().name == "vercel"
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        await _tick(service)
        assert (await _preview(service, mission.id)) == {}

    async def test_dead_tunnel_url_is_refreshed(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, clock: Clock
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        await _tick(service)
        target.url = "https://fresh.trycloudflare.com"
        clock.now += 60
        await _tick(service)
        assert target.refreshed == [mission.id]
        assert (await _preview(service, mission.id))["url"] == "https://fresh.trycloudflare.com"

    async def test_teardown_and_resume_routes(
        self,
        service: ShipcrewService,
        target: FakeTarget,
        repo: Path,
        client: httpx.AsyncClient,
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        await _tick(service)
        r = await client.delete(f"{P}/missions/{mission.id}/preview")
        assert r.status_code == 200 and r.json()["preview"]["status"] == "idle"
        assert target.torn_down == [mission.id]
        await _tick(service)
        assert len(target.deploys) == 1  # stopped: no redeploy
        r = await client.post(f"{P}/missions/{mission.id}/preview")
        assert r.status_code == 200
        await service.preview.drain()
        assert len(target.deploys) == 2
        assert (await _preview(service, mission.id))["status"] == "live"


class TestShipStage:
    async def test_server_side_ship_is_a_redeploy_verify_and_report(
        self,
        service: ShipcrewService,
        target: FakeTarget,
        repo: Path,
        sessions: FakeSessions,
        clock: Clock,
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged", cost_usd=1.0, started_at=1000.0)
        await _tick(service)
        await service.ship.tick()
        await service.ship.drain()
        state = await service.require_mission(mission.id)
        assert sessions.created == []  # no devops session, no devops bundle needed
        assert state.ship_status == "done" and state.ship_url == target.url
        assert [c.final for c in target.deploys] == [False, True]
        report = state.ship_report_md or ""
        assert f"- **Deployment:** {target.url}" in report
        assert "- **Live since:** " in report and "(first deploy)" in report
        assert "- **Last deploy:** fake, commit `" in report and "image 42.0 MB" in report

    async def test_ship_fails_when_the_last_redeploy_fails(
        self, service: ShipcrewService, target: FakeTarget, repo: Path
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        await _tick(service)
        target.fail = DeployError("docker build failed", kept_previous=True)
        push(repo, "late.txt", "x")
        await service.ship.tick()
        await service.ship.drain()
        state = await service.require_mission(mission.id)
        assert state.ship_status == "failed"
        assert "the last redeploy failed: docker build failed" in (state.ship_error or "")
        assert target.url in (state.ship_error or "")  # the older version still serves


class TestRestart:
    def _restarted(
        self, service: ShipcrewService, sessions: FakeSessions, target: FakeTarget
    ) -> ShipcrewService:
        assert service.settings.db_url is not None
        fresh = ShipcrewService(
            ShipcrewStore(create_engine(service.settings.db_url)),
            MissionEventBus(),
            sessions,
            service.settings,
        )
        fresh.set_deploy_target(target)
        fresh.preview.probe = _ok_probe
        fresh.ship.probe = _ok_probe
        return fresh

    async def test_a_deploy_cut_off_by_a_restart_runs_again(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        # As left by a server killed mid-deploy.
        await asyncio.to_thread(
            service.store.update_mission,
            mission.id,
            preview={"status": "deploying", "deploying_sha": "0" * 40},
        )
        fresh = self._restarted(service, sessions, target)
        await fresh.preview.tick()
        await fresh.preview.drain()
        preview = (await fresh.require_mission(mission.id)).preview
        assert preview["status"] == "live" and "deploying_sha" not in preview
        assert len(target.deploys) == 1

    async def test_a_server_side_ship_cut_off_by_a_restart_resumes(
        self, service: ShipcrewService, target: FakeTarget, repo: Path, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service, repo)
        await _task(service, mission, "A", status="merged")
        await asyncio.to_thread(service.store.update_mission, mission.id, ship_status="deploying")
        fresh = self._restarted(service, sessions, target)
        await fresh.ship.tick()
        await fresh.ship.drain()
        state = await fresh.require_mission(mission.id)
        assert state.ship_status == "done" and state.ship_url == target.url
        assert sessions.created == []


class TestWithTheDockerTarget:
    """End to end through the scheduler: fake docker + fake cloudflared executables."""

    @pytest.fixture
    def fakes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DeployFakes]:
        f = DeployFakes(tmp_path / "fakes", monkeypatch)
        yield f
        f.kill_all(tmp_path / "state")

    async def test_first_merge_gets_a_live_tunnel_url(
        self,
        app_and_service: Any,
        service: ShipcrewService,
        repo: Path,
        fakes: DeployFakes,
    ) -> None:
        import dataclasses

        from omnigent.shipcrew.deploy_targets.docker import DockerTarget

        service.settings = dataclasses.replace(
            service.settings, deploy_target="docker", deploy_health_timeout_s=5.0,
            tunnel_url_timeout_s=5.0,
        )  # fmt: skip
        service.set_deploy_target(DockerTarget(service.settings))
        service.preview.probe = _ok_probe
        mission = await _mission(service, repo)
        await _task(service, mission, "Foundation", status="merged")
        await _task(service, mission, "Feature", status="running")
        await app_and_service[0].state.shipcrew_scheduler.tick()
        await service.preview.drain()
        preview = await _preview(service, mission.id)
        assert preview["status"] == "live", preview
        assert preview["url"] == "https://fake-1.trycloudflare.com"
        assert preview["target"] == "docker"
        assert preview["detail"]["image"].startswith("shipcrew/polls-")
        assert len(fakes.calls("build")) == 1
        await service.preview.teardown(mission.id)
        assert fakes.world()["containers"] == {}
