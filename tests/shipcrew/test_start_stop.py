"""Start/stop, session-state mapping and scheduling, with the session seam faked."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omnigent.shipcrew.scheduler import ShipcrewScheduler
from omnigent.shipcrew.service import ShipcrewService, build_prompt, map_session_state
from omnigent.shipcrew.sessions import SessionSnapshot
from omnigent.shipcrew.store import Task

from .conftest import FakeSessions

P = "/v1/shipcrew"


async def _setup(client: httpx.AsyncClient, **task_kw: Any) -> tuple[str, dict[str, Any]]:
    mission = (
        await client.post(f"{P}/missions", json={"title": "M", "repo_path": "/repo"})
    ).json()
    task = (
        await client.post(
            f"{P}/missions/{mission['id']}/tasks",
            json={
                "title": "Add login",
                "body": "Use OAuth.",
                "acceptance": ["tests pass"],
                **task_kw,
            },
        )
    ).json()
    return mission["id"], task


class TestStart:
    async def test_start_creates_root_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        mission_id, task = await _setup(client, owned_paths=["src/auth/**"])
        r = await client.post(f"{P}/tasks/{task['id']}/start")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "running"
        assert body["root_session_id"] == "sess1"
        (req,) = sessions.created
        assert req.branch == f"shipcrew/{task['id'][:8]}-add-login"
        assert body["branch"] == req.branch
        assert req.repo_path == "/repo"
        assert req.agent_dir == service.settings.agents_dir / "developer"
        assert req.acting_user == "alice@example.com"
        assert req.labels == {"shipcrew.task_id": task["id"], "shipcrew.role": "developer"}
        assert "# Add login" in req.prompt
        assert "Use OAuth." in req.prompt
        assert "- tests pass" in req.prompt
        assert "`src/auth/**`" in req.prompt
        mission = await service.require_mission(mission_id)
        assert mission.status == "active"

    async def test_start_uses_role_bundle(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client, role="reviewer")
        await client.post(f"{P}/tasks/{task['id']}/start")
        assert sessions.created[0].agent_dir == service.settings.agents_dir / "reviewer"

    async def test_missing_bundle_blocks(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client, role="designer")
        body = (await client.post(f"{P}/tasks/{task['id']}/start")).json()
        assert body["status"] == "blocked"
        assert "designer" in body["blocked_reason"]
        assert sessions.created == []

    async def test_session_failure_blocks_with_reason(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        sessions.fail_create = "no online host"
        _, task = await _setup(client)
        body = (await client.post(f"{P}/tasks/{task['id']}/start")).json()
        assert body["status"] == "blocked"
        assert body["blocked_reason"] == "no online host"
        assert body["root_session_id"] is None

    async def test_restart_reuses_branch(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        await client.post(f"{P}/tasks/{task['id']}/stop")
        await client.post(f"{P}/tasks/{task['id']}/start")
        assert [r.branch for r in sessions.created] == [f"shipcrew/{task['id'][:8]}-add-login"] * 2

    async def test_start_is_idempotent_while_running(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        await client.post(f"{P}/tasks/{task['id']}/start")
        assert len(sessions.created) == 1

    async def test_human_assigned_task_cannot_start(self, client: httpx.AsyncClient) -> None:
        _, task = await _setup(client)
        await client.patch(
            f"{P}/tasks/{task['id']}", json={"assignee": {"kind": "human", "id": "bob"}}
        )
        assert (await client.post(f"{P}/tasks/{task['id']}/start")).status_code == 409

    async def test_patch_status_running_starts(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        body = (await client.patch(f"{P}/tasks/{task['id']}", json={"status": "running"})).json()
        assert body["status"] == "running"
        assert len(sessions.created) == 1


class TestStop:
    async def test_stop_cancels_and_blocks(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        body = (await client.post(f"{P}/tasks/{task['id']}/stop")).json()
        # Terminated (process + runner), not just interrupted.
        assert sessions.stopped == ["sess1"]
        assert sessions.cancelled == []
        assert body["status"] == "blocked"
        assert body["blocked_reason"] == "stopped by user"

    async def test_assigning_human_stops_agent(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        body = (
            await client.patch(
                f"{P}/tasks/{task['id']}", json={"assignee": {"kind": "human", "id": "bob"}}
            )
        ).json()
        # Interrupted only: the human takes over the live terminal.
        assert sessions.cancelled == ["sess1"]
        assert sessions.stopped == []
        assert body["assignee"] == {"kind": "human", "id": "bob"}

    async def test_dragging_out_of_running_stops_agent(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        body = (await client.patch(f"{P}/tasks/{task['id']}", json={"status": "backlog"})).json()
        assert body["status"] == "backlog"
        assert sessions.stopped == ["sess1"]

    async def test_moving_a_review_card_ends_its_idle_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        await asyncio.to_thread(service.store.update_task, task["id"], status="review")
        await client.patch(f"{P}/tasks/{task['id']}", json={"status": "ready"})
        assert sessions.stopped == ["sess1"]

    async def test_restarting_a_review_card_ends_its_idle_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        await asyncio.to_thread(service.store.update_task, task["id"], status="review")
        body = (await client.post(f"{P}/tasks/{task['id']}/start")).json()
        assert sessions.stopped == ["sess1"]
        assert (body["status"], body["root_session_id"]) == ("running", "sess2")


class TestStartRaces:
    async def test_concurrent_starts_create_one_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client)
        first, second = await asyncio.gather(
            service.start_task(task["id"], None),
            service.start_task(task["id"], None),
            return_exceptions=True,
        )
        assert len(sessions.created) == 1
        outcomes = sorted(type(r).__name__ for r in (first, second))
        assert outcomes == ["OmnigentError", "Task"]

    async def test_stop_during_start_ends_the_new_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client)
        sessions.gate = asyncio.Event()
        starting = asyncio.create_task(service.start_task(task["id"], None))
        while not sessions.created:
            await asyncio.sleep(0)
        stopped = await service.stop_task(task["id"], None)
        assert stopped.status == "blocked"
        sessions.gate.set()
        final = await starting
        assert sessions.stopped == ["sess1"]
        assert (final.status, final.root_session_id) == ("blocked", "sess1")

    async def test_human_taking_card_during_start_interrupts_new_session(
        self, client: httpx.AsyncClient, sessions: FakeSessions, service: ShipcrewService
    ) -> None:
        _, task = await _setup(client)
        sessions.gate = asyncio.Event()
        starting = asyncio.create_task(service.start_task(task["id"], None))
        while not sessions.created:
            await asyncio.sleep(0)
        await service.patch_task(task["id"], {"assignee": {"kind": "human", "id": "bob"}}, None)
        sessions.gate.set()
        final = await starting
        assert sessions.cancelled == ["sess1"]
        assert sessions.stopped == []
        assert final.root_session_id == "sess1"

    async def test_unexpected_start_error_frees_the_slot(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        sessions.crash_create = RuntimeError("tunnel closed")
        _, task = await _setup(client)
        body = (await client.post(f"{P}/tasks/{task['id']}/start")).json()
        assert (body["status"], body["blocked_reason"]) == ("blocked", "tunnel closed")


class TestSessionMapping:
    def _task(self, **kw: Any) -> Task:
        return Task(id="t", mission_id="m", title="t", **{"status": "running", **kw})

    def test_approval_prompt_means_intervention(self) -> None:
        snap = SessionSnapshot(status="running", awaiting_human=True)
        assert map_session_state(self._task(), snap) == {
            "status": "intervention",
            "session_seen_active": True,
        }

    def test_idle_after_work_means_review(self) -> None:
        snap = SessionSnapshot(status="idle", cost_usd=0.42)
        changes = map_session_state(self._task(session_seen_active=True), snap)
        assert changes == {"status": "review", "cost_usd": 0.42}

    def test_turn_finished_between_ticks_means_review(self) -> None:
        snap = SessionSnapshot(status="idle", agent_replied=True)
        assert map_session_state(self._task(), snap) == {"status": "review"}

    def test_idle_before_first_turn_stays_running(self) -> None:
        assert map_session_state(self._task(), SessionSnapshot(status="idle")) == {}

    def test_answered_prompt_returns_to_running(self) -> None:
        task = self._task(status="intervention", session_seen_active=True)
        assert map_session_state(task, SessionSnapshot(status="running")) == {"status": "running"}

    def test_review_card_resumes_when_its_agent_runs_again(self) -> None:
        task = self._task(status="review", session_seen_active=True)
        assert map_session_state(task, SessionSnapshot(status="running")) == {"status": "running"}

    def test_review_card_ignores_idle_failed_or_stopped_sessions(self) -> None:
        task = self._task(status="review", session_seen_active=True, cost_usd=0.1)
        assert map_session_state(task, SessionSnapshot(status="idle")) == {}
        assert map_session_state(task, SessionSnapshot(status="failed", error="x")) == {}
        assert map_session_state(task, None) == {}
        assert map_session_state(task, SessionSnapshot(status="idle", cost_usd=0.3)) == {
            "cost_usd": 0.3
        }

    def test_failure_blocks(self) -> None:
        snap = SessionSnapshot(status="failed", error="rate limited")
        assert map_session_state(self._task(), snap) == {
            "status": "blocked",
            "blocked_reason": "rate limited",
        }

    def test_vanished_session_blocks(self) -> None:
        assert map_session_state(self._task(), None)["status"] == "blocked"


class TestScheduler:
    async def _ready(self, client: httpx.AsyncClient, mission_id: str, **kw: Any) -> str:
        r = await client.post(f"{P}/missions/{mission_id}/tasks", json={"title": "t", **kw})
        task_id = r.json()["id"]
        await client.patch(f"{P}/tasks/{task_id}", json={"status": "ready"})
        return task_id

    async def test_tick_starts_ready_tasks_up_to_capacity(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission_id, _ = await _setup(client)
        ids = [await self._ready(client, mission_id, owned_paths=[f"p{i}/**"]) for i in range(3)]
        scheduler = ShipcrewScheduler(lambda: service, 60)
        started = await scheduler.tick()
        assert started == ids[:2]  # max_parallel=2 in the test settings
        waiting = await service.require_task(ids[2])
        assert waiting.status == "ready"
        assert waiting.blocked_reason == "capacity: 2/2 agents running"

    async def test_tick_respects_dependencies_and_overlap(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission_id, dep = await _setup(client)
        blocked_by_dep = await self._ready(
            client, mission_id, depends_on=[dep["id"]], owned_paths=["a/**"]
        )
        first = await self._ready(client, mission_id, owned_paths=["src/**"])
        overlapping = await self._ready(client, mission_id, owned_paths=["src/app.py"])
        started = await ShipcrewScheduler(lambda: service, 60).tick()
        assert started == [first]
        assert (await service.require_task(blocked_by_dep)).blocked_reason.startswith(
            "waiting on dependencies"
        )
        assert "overlap" in (await service.require_task(overlapping)).blocked_reason

    async def test_merged_dependency_unblocks(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission_id, dep = await _setup(client)
        task_id = await self._ready(client, mission_id, depends_on=[dep["id"]], owned_paths=["a"])
        scheduler = ShipcrewScheduler(lambda: service, 60)
        assert await scheduler.tick() == []
        await client.patch(f"{P}/tasks/{dep['id']}", json={"status": "merged"})
        assert await scheduler.tick() == [task_id]

    async def test_budget_blocks_new_starts(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission_id, spent = await _setup(client)
        service.store.update_task(spent["id"], cost_usd=10.0)
        task_id = await self._ready(client, mission_id, owned_paths=["a"])
        assert await ShipcrewScheduler(lambda: service, 60).tick() == []
        assert (await service.require_task(task_id)).blocked_reason.startswith("budget")

    async def test_human_assigned_ready_task_is_skipped(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission_id, _ = await _setup(client)
        task_id = await self._ready(client, mission_id, owned_paths=["a"])
        await client.patch(
            f"{P}/tasks/{task_id}", json={"assignee": {"kind": "human", "id": "bob"}}
        )
        assert await ShipcrewScheduler(lambda: service, 60).tick() == []

    async def test_tick_syncs_session_state(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        _, task = await _setup(client)
        await client.post(f"{P}/tasks/{task['id']}/start")
        scheduler = ShipcrewScheduler(lambda: service, 60)
        sessions.snapshots["sess1"] = SessionSnapshot(status="running", cost_usd=0.1)
        await scheduler.tick()
        assert (await service.require_task(task["id"])).status == "running"
        sessions.snapshots["sess1"] = SessionSnapshot(status="running", awaiting_human=True)
        await scheduler.tick()
        assert (await service.require_task(task["id"])).status == "intervention"
        sessions.snapshots["sess1"] = SessionSnapshot(status="idle", cost_usd=0.9)
        await scheduler.tick()
        done = await service.require_task(task["id"])
        assert (done.status, done.cost_usd) == ("review", 0.9)


def test_prompt_without_optional_sections() -> None:
    prompt = build_prompt(Task(id="abc12345xyz", mission_id="m", title="Fix it!"))
    assert prompt.startswith("# Fix it!")
    assert "Acceptance" not in prompt
    assert "shipcrew/abc12345-fix-it" in prompt
    assert prompt.rstrip().endswith("or `FAIL: <reason>`.")


class TestSchedulerLoop:
    async def test_poke_runs_a_tick_and_survives_failures(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        calls = 0

        def get_service() -> ShipcrewService:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database not ready")
            return service

        _, task = await _setup(client)
        await client.patch(f"{P}/tasks/{task['id']}", json={"status": "ready"})
        scheduler = ShipcrewScheduler(get_service, 60)
        scheduler.start()
        try:
            for done in (lambda: calls >= 1, lambda: bool(sessions.created)):
                scheduler.poke()
                for _ in range(200):
                    if done():
                        break
                    await asyncio.sleep(0.01)
        finally:
            await scheduler.stop()
        assert calls >= 2
        assert [r.task_id for r in sessions.created] == [task["id"]]
        assert (await service.require_task(task["id"])).status == "running"
