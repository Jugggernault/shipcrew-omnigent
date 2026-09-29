"""Run everything on a mission: ``start-all``, ``auto_run`` after a plan import,
and the rule-based ``command`` box."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.shipcrew.commands import classify
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import SessionSnapshot

from .conftest import USER_HEADER, FakeSessions

P = "/v1/shipcrew"
BOB = {USER_HEADER: "bob@example.com"}


async def _mission(client: httpx.AsyncClient, repo_path: str = "/repo") -> dict[str, Any]:
    r = await client.post(f"{P}/missions", json={"title": "Ship", "repo_path": repo_path})
    assert r.status_code == 200, r.text
    return r.json()


async def _task(client: httpx.AsyncClient, mission_id: str, **kw: Any) -> dict[str, Any]:
    r = await client.post(f"{P}/missions/{mission_id}/tasks", json={"title": "T", **kw})
    assert r.status_code == 200, r.text
    return r.json()


async def _statuses(client: httpx.AsyncClient, mission_id: str) -> dict[str, str]:
    tasks = (await client.get(f"{P}/missions/{mission_id}/tasks")).json()["tasks"]
    return {t["title"]: t["status"] for t in tasks}


class TestStartAll:
    async def test_moves_backlog_to_ready_and_emits_updates(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission = await _mission(client)
        a = await _task(client, mission["id"], title="a")
        b = await _task(client, mission["id"], title="b")
        async with service.bus.subscribe(mission["id"]) as queue:
            r = await client.post(f"{P}/missions/{mission['id']}/start-all")
            await asyncio.sleep(0)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["started"] == [a["id"], b["id"]]
        assert body["mission"]["id"] == mission["id"]
        assert [(e["type"], e["task"]["status"]) for e in events] == [
            ("task.updated", "ready"),
            ("task.updated", "ready"),
        ]
        assert await _statuses(client, mission["id"]) == {"a": "ready", "b": "ready"}

    async def test_idempotent(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        await _task(client, mission["id"])
        first = await client.post(f"{P}/missions/{mission['id']}/start-all")
        second = await client.post(f"{P}/missions/{mission['id']}/start-all")
        assert len(first.json()["started"]) == 1
        assert second.json()["started"] == []
        assert await _statuses(client, mission["id"]) == {"T": "ready"}

    async def test_skips_human_blocked_merged_and_other_missions(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission = await _mission(client)
        other = await _mission(client)
        await _task(client, mission["id"], title="agent")
        human = await _task(client, mission["id"], title="human")
        blocked = await _task(client, mission["id"], title="blocked")
        merged = await _task(client, mission["id"], title="merged")
        await _task(client, other["id"], title="elsewhere")
        await client.patch(
            f"{P}/tasks/{human['id']}", json={"assignee": {"kind": "human", "id": "alice"}}
        )
        await client.patch(f"{P}/tasks/{blocked['id']}", json={"status": "blocked"})
        await client.patch(f"{P}/tasks/{merged['id']}", json={"status": "merged"})
        r = await client.post(f"{P}/missions/{mission['id']}/start-all")
        assert len(r.json()["started"]) == 1
        assert await _statuses(client, mission["id"]) == {
            "agent": "ready",
            "human": "backlog",
            "blocked": "blocked",
            "merged": "merged",
        }
        assert await _statuses(client, other["id"]) == {"elsewhere": "backlog"}

    async def test_scheduler_gates_still_decide(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        # max_parallel=2 (conftest): three free tasks + one waiting on a dependency.
        mission = await _mission(client)
        first = await _task(client, mission["id"], title="first", owned_paths=["a/**"])
        await _task(client, mission["id"], title="second", owned_paths=["b/**"])
        await _task(client, mission["id"], title="third", owned_paths=["c/**"])
        await _task(
            client,
            mission["id"],
            title="dependant",
            owned_paths=["d/**"],
            depends_on=[first["id"]],
        )
        await client.post(f"{P}/missions/{mission['id']}/start-all")
        started = await service.schedule_ready()
        assert len(started) == 2
        assert len(sessions.created) == 2
        statuses = await _statuses(client, mission["id"])
        assert statuses == {
            "first": "running",
            "second": "running",
            "third": "ready",
            "dependant": "ready",
        }
        tasks = (await client.get(f"{P}/missions/{mission['id']}/tasks")).json()["tasks"]
        reasons = {t["title"]: t["blocked_reason"] for t in tasks}
        assert reasons["dependant"] == "waiting on dependencies: first"
        assert reasons["third"] is not None and "capacity" in reasons["third"].lower()

    async def test_other_user_gets_404(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        await _task(client, mission["id"])
        r = await client.post(f"{P}/missions/{mission['id']}/start-all", headers=BOB)
        assert r.status_code == 404
        assert await _statuses(client, mission["id"]) == {"T": "backlog"}

    async def test_unknown_mission_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.post(f"{P}/missions/nope/start-all")).status_code == 404


class TestAutoRun:
    async def test_patch_mission_sets_auto_run_and_emits(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission = await _mission(client)
        assert mission["auto_run"] is False
        async with service.bus.subscribe(mission["id"]) as queue:
            r = await client.patch(f"{P}/missions/{mission['id']}", json={"auto_run": True})
            await asyncio.sleep(0)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert r.status_code == 200, r.text
        assert r.json()["auto_run"] is True
        assert [e["type"] for e in events] == ["mission.updated"]
        assert events[0]["mission"]["auto_run"] is True
        listed = (await client.get(f"{P}/missions")).json()["missions"]
        assert listed[0]["auto_run"] is True

    async def test_patch_mission_other_user_404(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        r = await client.patch(
            f"{P}/missions/{mission['id']}", json={"auto_run": True}, headers=BOB
        )
        assert r.status_code == 404
        assert (await client.get(f"{P}/missions")).json()["missions"][0]["auto_run"] is False

    @pytest.fixture
    def repo(self, tmp_path: Path, agents_dir: Path) -> Path:
        (agents_dir / "planner").mkdir()
        (agents_dir / "planner" / "config.yaml").write_text("name: planner\n")
        root = tmp_path / "repo"
        (root / ".shipcrew").mkdir(parents=True)
        return root

    async def _import(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        repo: Path,
        *,
        auto_run: bool,
    ) -> dict[str, Any]:
        mission = await _mission(client, str(repo))
        if auto_run:
            await client.patch(f"{P}/missions/{mission['id']}", json={"auto_run": True})
        manual = await _task(client, mission["id"], title="manual")
        r = await client.post(f"{P}/missions/{mission['id']}/plan", json={"prd": "Build it."})
        assert r.status_code == 200, r.text
        plan = {
            "tasks": [
                {"key": "T01", "title": "one"},
                {"key": "T02", "title": "two", "depends_on": ["T01"]},
            ]
        }
        (repo / ".shipcrew" / "plan.json").write_text(json.dumps(plan))
        session_id = f"sess{len(sessions.created)}"
        sessions.snapshots[session_id] = SessionSnapshot(status="idle", agent_replied=True)
        await service.planner.sync()
        return {"mission": mission, "manual": manual}

    async def test_import_with_auto_run_moves_imported_tasks_to_ready(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        repo: Path,
    ) -> None:
        state = await self._import(client, service, sessions, repo, auto_run=True)
        mission_id = state["mission"]["id"]
        # Only the plan's tasks move; a hand-made backlog card stays.
        assert await _statuses(client, mission_id) == {
            "manual": "backlog",
            "one": "ready",
            "two": "ready",
        }
        # The gates still apply on the next tick: two waits on one.
        started = await service.schedule_ready()
        assert len(started) == 1
        assert await _statuses(client, mission_id) == {
            "manual": "backlog",
            "one": "running",
            "two": "ready",
        }

    async def test_import_without_auto_run_leaves_backlog(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        repo: Path,
    ) -> None:
        state = await self._import(client, service, sessions, repo, auto_run=False)
        assert set((await _statuses(client, state["mission"]["id"])).values()) == {"backlog"}


class TestCommand:
    @pytest.mark.parametrize(
        ("text", "intent"),
        [
            ("run all", "start_all"),
            ("Run all tasks", "start_all"),
            ("start", "start_all"),
            ("Lance tout !", "start_all"),
            ("LANCER TOUTES LES TÂCHES", "start_all"),
            ("démarre", "start_all"),
            ("Démarrer tout, s'il te plaît", "start_all"),
            ("plan", "plan"),
            ("Plan from PRD", "plan"),
            ("planifie la mission", "plan"),
            ("sync", "sync"),
            ("Sync GitHub", "sync"),
            ("synchronise", "sync"),
            ("stop all", "stop_all"),
            ("Arrête tout", "stop_all"),
            ("ARRETER TOUTES LES TACHES", "stop_all"),
            ("ship", "ship"),
            ("deploy to production", "ship"),
            ("Déploie sur Vercel", "ship"),
        ],
    )
    def test_classify(self, text: str, intent: str) -> None:
        assert classify(text) == intent

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "hello",
            "don't run all",
            "ne lance pas tout",
            "run all and stop",
            "lance tout puis arrête",
            "deploy and stop",
            "rm -rf /",
        ],
    )
    def test_unknown_or_ambiguous(self, text: str) -> None:
        assert classify(text) is None

    async def test_run_all_command(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        r = await client.post(f"{P}/missions/{mission['id']}/command", json={"text": "Lance tout"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["intent"], body["started"]) == ("start_all", [task["id"]])
        assert body["message"] == "Moved 1 task to Ready."
        again = await client.post(
            f"{P}/missions/{mission['id']}/command", json={"text": "run all"}
        )
        assert again.json()["message"] == "No backlog task to run."

    async def test_stop_all_command(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        running = await _task(client, mission["id"], title="running")
        await _task(client, mission["id"], title="idle")
        await client.post(f"{P}/tasks/{running['id']}/start")
        r = await client.post(
            f"{P}/missions/{mission['id']}/command", json={"text": "arrête tout"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["stopped"] == [running["id"]]
        assert r.json()["message"] == "Stopped 1 running task."
        assert sessions.stopped == ["sess1"]
        assert await _statuses(client, mission["id"]) == {"running": "blocked", "idle": "backlog"}

    async def test_sync_command_reports(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        r = await client.post(f"{P}/missions/{mission['id']}/command", json={"text": "sync"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["intent"] == "sync"
        assert body["sync"]["ok"] is False  # no repo_url on this mission
        assert body["message"].startswith("GitHub sync skipped")

    async def test_plan_command_uses_repo_prd(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        tmp_path: Path,
        agents_dir: Path,
    ) -> None:
        (agents_dir / "planner").mkdir()
        (agents_dir / "planner" / "config.yaml").write_text("name: planner\n")
        repo = tmp_path / "repo"
        (repo / ".shipcrew").mkdir(parents=True)
        (repo / ".shipcrew" / "prd.md").write_text("Build a shop.")
        mission = await _mission(client, str(repo))
        r = await client.post(f"{P}/missions/{mission['id']}/command", json={"text": "planifie"})
        assert r.status_code == 200, r.text
        assert r.json()["mission"]["plan"]["status"] == "running"
        assert "Build a shop." in sessions.created[0].prompt

    async def test_unknown_command_is_400_with_the_list(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        await _task(client, mission["id"])
        r = await client.post(
            f"{P}/missions/{mission['id']}/command", json={"text": "build a rocket"}
        )
        assert r.status_code == 400
        message = r.json()["error"]["message"]
        for command in ("run all", "plan", "sync", "stop all", "lance tout", "arrête tout"):
            assert command in message
        assert await _statuses(client, mission["id"]) == {"T": "backlog"}

    async def test_empty_text_is_422(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        r = await client.post(f"{P}/missions/{mission['id']}/command", json={"text": ""})
        assert r.status_code == 422

    async def test_other_user_gets_404(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        await _task(client, mission["id"])
        r = await client.post(
            f"{P}/missions/{mission['id']}/command", json={"text": "run all"}, headers=BOB
        )
        assert r.status_code == 404
        assert await _statuses(client, mission["id"]) == {"T": "backlog"}
