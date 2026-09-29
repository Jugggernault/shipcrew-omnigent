"""``POST /missions/{id}/plan``: planner session -> ``.shipcrew/plan.json`` -> backlog tasks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.shipcrew.planner import PlanError, parse_plan
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import SessionSnapshot

from .conftest import USER_HEADER, FakeSessions

P = "/v1/shipcrew"

PLAN = {
    "mission": {"title": "Shop"},
    "platform": "web",
    "stack": "Next.js",
    "needs_db": False,
    "data_model": [],
    "tasks": [
        {
            "key": "T01",
            "title": "Foundation",
            "body": "App shell.",
            "acceptance": ["the home page renders", "CI is green"],
            "role": "scaffolder",
            "depends_on": [],
            "owned_paths": ["app/layout.tsx", "lib/**"],
        },
        {
            "key": "T02",
            "title": "Cart",
            "acceptance": ["adding an item shows it in the cart"],
            "depends_on": ["T01"],
            "owned_paths": ["app/cart/**"],
        },
        {
            "key": "T03",
            "title": "QA",
            "acceptance": ["the e2e suite passes"],
            "role": "qa",
            "depends_on": ["T01", "T02"],
            "owned_paths": ["e2e/**"],
        },
    ],
}


@pytest.fixture
def repo(tmp_path: Path, agents_dir: Path) -> Path:
    (agents_dir / "planner").mkdir()
    (agents_dir / "planner" / "config.yaml").write_text("name: planner\n")
    root = tmp_path / "repo"
    (root / ".shipcrew").mkdir(parents=True)
    return root


def _write_plan(repo: Path, plan: dict[str, Any]) -> None:
    (repo / ".shipcrew" / "plan.json").write_text(json.dumps(plan))


async def _mission(client: httpx.AsyncClient, repo: Path) -> dict[str, Any]:
    r = await client.post(f"{P}/missions", json={"title": "Shop", "repo_path": str(repo)})
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(client: httpx.AsyncClient, mission_id: str, **body: Any) -> httpx.Response:
    return await client.post(f"{P}/missions/{mission_id}/plan", json=body)


async def _finish_planner(
    service: ShipcrewService, sessions: FakeSessions, session_id: str
) -> None:
    sessions.snapshots[session_id] = SessionSnapshot(status="idle", agent_replied=True)
    await service.planner.sync()


async def _tasks(client: httpx.AsyncClient, mission_id: str) -> list[dict[str, Any]]:
    return (await client.get(f"{P}/missions/{mission_id}/tasks")).json()["tasks"]


async def _by_key(
    client: httpx.AsyncClient, service: ShipcrewService, mission_id: str
) -> dict[str, dict[str, Any]]:
    """The mission's tasks keyed by their stored plan key (not part of the API)."""
    out = {}
    for task in await _tasks(client, mission_id):
        stored = await asyncio.to_thread(service.store.get_task, task["id"])
        assert stored is not None
        out[stored.plan_key] = task
    return out


async def _mission_state(client: httpx.AsyncClient, mission_id: str) -> dict[str, Any]:
    missions = (await client.get(f"{P}/missions")).json()["missions"]
    return next(m for m in missions if m["id"] == mission_id)


class TestPlanStart:
    async def test_starts_planner_in_the_repo(
        self, client: httpx.AsyncClient, sessions: FakeSessions, repo: Path
    ) -> None:
        mission = await _mission(client, repo)
        assert mission["plan"] == {
            "status": "idle",
            "session_id": None,
            "error": None,
            "imported_count": 0,
        }
        r = await _plan(client, mission["id"], prd="Build a tiny shop.")
        assert r.status_code == 200, r.text
        assert r.json()["plan"]["status"] == "running"
        assert r.json()["plan"]["session_id"] == "sess1"
        (request,) = sessions.created
        assert request.agent_dir.name == "planner"
        # The planner runs in the mission repo itself, not in a task worktree.
        assert request.workspace == str(repo)
        assert "Build a tiny shop." in request.prompt
        assert ".shipcrew/plan.json" in request.prompt

    async def test_reads_prd_from_repo(
        self, client: httpx.AsyncClient, sessions: FakeSessions, repo: Path
    ) -> None:
        (repo / ".shipcrew" / "prd.md").write_text("# PRD\nA shop with a cart.\n")
        mission = await _mission(client, repo)
        r = await client.post(f"{P}/missions/{mission['id']}/plan")
        assert r.status_code == 200, r.text
        assert "A shop with a cart." in sessions.created[0].prompt

    async def test_no_prd_is_rejected(
        self, client: httpx.AsyncClient, sessions: FakeSessions, repo: Path
    ) -> None:
        mission = await _mission(client, repo)
        r = await _plan(client, mission["id"])
        assert r.status_code == 400
        assert "prd.md" in r.json()["error"]["message"]
        assert sessions.created == []

    async def test_second_plan_while_running_conflicts(
        self, client: httpx.AsyncClient, sessions: FakeSessions, repo: Path
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="x")
        assert (await _plan(client, mission["id"], prd="x")).status_code == 409
        assert len(sessions.created) == 1

    async def test_session_failure_marks_plan_failed(
        self, client: httpx.AsyncClient, sessions: FakeSessions, repo: Path
    ) -> None:
        sessions.fail_create = "no online host"
        mission = await _mission(client, repo)
        plan = (await _plan(client, mission["id"], prd="x")).json()["plan"]
        assert (plan["status"], plan["error"]) == ("failed", "no online host")

    async def test_planner_crash_marks_plan_failed(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        service: ShipcrewService,
        repo: Path,
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="x")
        sessions.snapshots["sess1"] = SessionSnapshot(status="failed", error="model overloaded")
        await service.planner.sync()
        plan = (await _mission_state(client, mission["id"]))["plan"]
        assert (plan["status"], plan["error"]) == ("failed", "model overloaded")

    async def test_other_users_cannot_plan(self, client: httpx.AsyncClient, repo: Path) -> None:
        mission = await _mission(client, repo)
        r = await _plan(client, mission["id"], prd="x")
        assert r.status_code == 200
        bob = {USER_HEADER: "bob@example.com"}
        r = await client.post(f"{P}/missions/{mission['id']}/plan", json={}, headers=bob)
        assert r.status_code == 404
        r = await client.post(f"{P}/missions/{mission['id']}/plan", headers={USER_HEADER: ""})
        assert r.status_code == 401


class TestPlanImport:
    async def test_happy_path(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        service: ShipcrewService,
        repo: Path,
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="Build a tiny shop.")
        # Still working: nothing is imported yet.
        await service.planner.sync()
        assert await _tasks(client, mission["id"]) == []
        _write_plan(repo, PLAN)
        async with service.bus.subscribe(mission["id"]) as queue:
            await _finish_planner(service, sessions, "sess1")
            await asyncio.sleep(0)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        tasks = await _tasks(client, mission["id"])
        by_key = await _by_key(client, service, mission["id"])
        # Plan order is board order.
        assert [t["id"] for t in tasks] == [by_key[k]["id"] for k in ("T01", "T02", "T03")]
        assert [t["title"] for t in tasks] == ["Foundation", "Cart", "QA"]
        assert all(t["status"] == "backlog" for t in tasks)
        assert by_key["T01"]["role"] == "scaffolder"
        assert by_key["T02"]["role"] == "developer"
        assert by_key["T01"]["acceptance"] == ["the home page renders", "CI is green"]
        assert by_key["T01"]["owned_paths"] == ["app/layout.tsx", "lib/**"]
        assert by_key["T02"]["depends_on"] == [by_key["T01"]["id"]]
        assert by_key["T03"]["depends_on"] == [by_key["T01"]["id"], by_key["T02"]["id"]]
        plan = (await _mission_state(client, mission["id"]))["plan"]
        assert plan == {"status": "imported", "session_id": "sess1", "error": None,
                        "imported_count": 3}  # fmt: skip
        # One task.updated per task, then the mission.
        assert [e["type"] for e in events] == ["task.updated"] * 3 + ["mission.updated"]
        assert events[-1]["mission"]["plan"]["status"] == "imported"
        # The finished planner is ended.
        assert sessions.stopped == ["sess1"]

    async def test_cycle_is_rejected(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        service: ShipcrewService,
        repo: Path,
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="x")
        cyclic = json.loads(json.dumps(PLAN))
        cyclic["tasks"][0]["depends_on"] = ["T03"]
        _write_plan(repo, cyclic)
        await _finish_planner(service, sessions, "sess1")
        plan = (await _mission_state(client, mission["id"]))["plan"]
        assert plan["status"] == "failed"
        assert "dependency cycle: T01 -> T03 -> T01" in plan["error"]
        assert await _tasks(client, mission["id"]) == []

    async def test_missing_plan_file_fails(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        service: ShipcrewService,
        repo: Path,
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="x")
        await _finish_planner(service, sessions, "sess1")
        plan = (await _mission_state(client, mission["id"]))["plan"]
        assert plan["status"] == "failed"
        assert "did not write .shipcrew/plan.json" in plan["error"]

    async def test_reimport_updates_without_duplicating(
        self,
        client: httpx.AsyncClient,
        sessions: FakeSessions,
        service: ShipcrewService,
        repo: Path,
    ) -> None:
        mission = await _mission(client, repo)
        await _plan(client, mission["id"], prd="x")
        _write_plan(repo, PLAN)
        await _finish_planner(service, sessions, "sess1")
        first = await _by_key(client, service, mission["id"])
        # A human moved T01 on; a re-plan must not reset it.
        await client.patch(f"{P}/tasks/{first['T01']['id']}", json={"status": "ready"})

        replan = json.loads(json.dumps(PLAN))
        replan["tasks"][1]["title"] = "Cart and checkout"
        replan["tasks"].append(
            {"key": "T04", "title": "Security", "role": "security", "depends_on": ["T02"]}
        )
        r = await _plan(client, mission["id"], prd="x v2")
        assert r.json()["plan"]["session_id"] == "sess2"
        _write_plan(repo, replan)
        await _finish_planner(service, sessions, "sess2")

        tasks = await _tasks(client, mission["id"])
        second = await _by_key(client, service, mission["id"])
        assert len(tasks) == 4
        for key in ("T01", "T02", "T03"):
            assert second[key]["id"] == first[key]["id"]
        assert second["T01"]["status"] == "ready"
        assert second["T02"]["title"] == "Cart and checkout"
        assert second["T04"]["depends_on"] == [first["T02"]["id"]]
        assert (await _mission_state(client, mission["id"]))["plan"]["imported_count"] == 4

        # Importing the same file again changes nothing.
        await service.planner.import_plan(mission["id"])
        assert len(await _tasks(client, mission["id"])) == 4

        # A started task keeps the contract its agents and reviewer work to.
        await asyncio.to_thread(service.store.update_task, first["T02"]["id"], status="review")
        replan["tasks"][1]["title"] = "Rewritten"
        replan["tasks"][1]["owned_paths"] = ["**"]
        _write_plan(repo, replan)
        await service.planner.import_plan(mission["id"])
        third = await _by_key(client, service, mission["id"])
        assert third["T02"]["title"] == "Cart and checkout"
        assert third["T02"]["owned_paths"] == second["T02"]["owned_paths"]


class TestParsePlan:
    def test_unknown_dependency_key(self) -> None:
        bad = json.loads(json.dumps(PLAN))
        bad["tasks"][1]["depends_on"] = ["T01", "T09"]
        with pytest.raises(PlanError, match="T02 depends on unknown keys: T09"):
            parse_plan(json.dumps(bad))

    def test_duplicate_keys(self) -> None:
        bad = json.loads(json.dumps(PLAN))
        bad["tasks"][2]["key"] = "T01"
        with pytest.raises(PlanError, match="duplicate task keys: T01"):
            parse_plan(json.dumps(bad))

    def test_self_dependency_is_a_cycle(self) -> None:
        bad = json.loads(json.dumps(PLAN))
        bad["tasks"][0]["depends_on"] = ["T01"]
        with pytest.raises(PlanError, match="cycle: T01 -> T01"):
            parse_plan(json.dumps(bad))

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("{not json", "not valid JSON"),
            (json.dumps({"tasks": []}), "tasks"),
            (json.dumps({"tasks": [{"key": "T01"}]}), "tasks.0.title"),
            (json.dumps({"tasks": [{"key": "T01", "title": "x", "role": "../x"}]}), "role"),
            # The PRD is untrusted: no orchestrator / reviewer bundle for a task.
            (json.dumps({"tasks": [{"key": "T01", "title": "x", "role": "shipcrew"}]}), "role"),
            (
                json.dumps({"tasks": [{"key": "T", "title": "x", "owned_paths": ["../w/**"]}]}),
                "must not contain '..'",
            ),
            (
                json.dumps({"tasks": [{"key": "T", "title": "x", "owned_paths": ["/etc/**"]}]}),
                "relative to the repository",
            ),
            (json.dumps({"tasks": [{"key": f"T{i}", "title": "x"} for i in range(201)]}), "tasks"),
        ],
    )
    def test_schema_errors(self, text: str, message: str) -> None:
        with pytest.raises(PlanError, match=r"plan\.json") as info:
            parse_plan(text)
        assert message in str(info.value)
