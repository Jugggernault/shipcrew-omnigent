"""One mission = one omnigent project: creation, reuse, filing, rename, ACL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.shipcrew.pr_loop import _Ctx
from omnigent.shipcrew.projects import PROJECT_NAME_MAX, candidate_names, project_base_name
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import (
    ChildSessionRequest,
    OmnigentSessionService,
    RootSessionRequest,
)

from .conftest import USER_HEADER, FakeSessions
from .test_sessions import _patch_worktrees, _Registry, _Worktrees

P = "/v1/shipcrew"
ALICE = "alice@example.com"
BOB = {USER_HEADER: "bob@example.com"}


async def _mission(client: httpx.AsyncClient, title: str = "Tiny shop", **kw: Any) -> dict:
    r = await client.post(f"{P}/missions", json={"title": title, "repo_path": "/repo", **kw})
    assert r.status_code == 200, r.text
    return r.json()


class TestNames:
    def test_base_name_is_trimmed_and_capped(self) -> None:
        assert project_base_name("  Tiny   shop ") == "Tiny shop"
        assert project_base_name("   ") == "Mission"
        assert len(project_base_name("x" * 600)) <= PROJECT_NAME_MAX - 6

    def test_candidates_suffix_clashes(self) -> None:
        assert candidate_names("A")[:3] == ["A", "A (2)", "A (3)"]


class TestCreateAndReuse:
    async def test_mission_creates_its_project(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        assert mission["project_id"] == "proj1"
        assert sessions.projects == {ALICE: {"proj1": "Tiny shop"}}
        listed = (await client.get(f"{P}/missions")).json()["missions"]
        assert [m["project_id"] for m in listed] == ["proj1"]

    async def test_same_title_gets_its_own_project(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        first = await _mission(client)
        second = await _mission(client)
        assert first["project_id"] != second["project_id"]
        assert sorted(sessions.projects[ALICE].values()) == ["Tiny shop", "Tiny shop (2)"]

    async def test_adopts_an_unlinked_folder_of_the_same_name(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        sessions.projects[ALICE] = {"mine": "Tiny shop"}
        mission = await _mission(client)
        assert mission["project_id"] == "mine"
        assert sessions.project_calls == []

    async def test_existing_project_is_reused_by_every_start(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        for title in ("A", "B"):
            task = await service.create_task(mission["id"], title=title)
            await service.start_task(task.id, ALICE)
        assert [r.project_id for r in sessions.created] == ["proj1", "proj1"]
        assert [c[0] for c in sessions.project_calls] == ["create"]

    async def test_parallel_first_use_creates_one_project(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        sessions.fail_projects = "down"
        mission = await _mission(client)
        assert mission["project_id"] is None
        sessions.fail_projects = None
        tasks = [await service.create_task(mission["id"], title=t) for t in ("A", "B", "C")]
        await asyncio.gather(*(service.start_task(t.id, ALICE) for t in tasks))
        assert {r.project_id for r in sessions.created} == {"proj1"}
        assert len(sessions.projects[ALICE]) == 1

    async def test_existing_mission_gets_a_project_lazily(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        # A mission from before projects: no project_id stored.
        legacy = await asyncio.to_thread(
            service.store.create_mission, "Legacy", "/repo", None, owner_user_id=ALICE
        )
        assert legacy.project_id is None
        task = await service.create_task(legacy.id, title="T")
        await service.start_task(task.id, ALICE)
        assert sessions.created[0].project_id == "proj1"
        assert (await service.require_mission(legacy.id)).project_id == "proj1"

    async def test_deleted_project_is_replaced_on_next_use(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        sessions.projects[ALICE].clear()  # the user deleted the folder
        task = await service.create_task(mission["id"], title="T")
        await service.start_task(task.id, ALICE)
        assert sessions.created[0].project_id == "proj1"
        assert sessions.projects[ALICE] == {"proj1": "Tiny shop"}

    async def test_project_failure_never_blocks_a_start(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        sessions.fail_projects = "projects API down"
        task = await service.create_task(mission["id"], title="T")
        started = await service.start_task(task.id, ALICE)
        assert started.status == "running"
        assert sessions.created[0].project_id is None


class TestSessionsFiled:
    async def test_planner_and_children_are_filed(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        agents_dir: Path,
        tmp_path: Path,
    ) -> None:
        (agents_dir / "planner").mkdir()
        (agents_dir / "planner" / "config.yaml").write_text("name: planner\n")
        repo = tmp_path / "repo"
        repo.mkdir()
        created = await client.post(f"{P}/missions", json={"title": "M", "repo_path": str(repo)})
        mission = created.json()
        r = await client.post(f"{P}/missions/{mission['id']}/plan", json={"prd": "Build it."})
        assert r.status_code == 200, r.text
        assert sessions.created[-1].agent_dir.name == "planner"
        assert sessions.created[-1].project_id == mission["project_id"]

        task = await service.create_task(mission["id"], title="T")
        await service.start_task(task.id, ALICE)
        task = await service.require_task(task.id)
        ctx = _Ctx(
            task=task,
            mission=await service.require_mission(mission["id"]),
            repo=repo,
            branch="b",
            worktree=repo,
            owner=ALICE,
            base="main",
        )
        await service.pr_loop._start_child(ctx, "reviewer", "Review: T", "review")
        (child,) = sessions.children
        assert child.parent_session_id == task.root_session_id
        assert child.project_id == mission["project_id"]


class TestRenameAndAcl:
    async def test_rename_follows_to_the_project(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        r = await client.patch(f"{P}/missions/{mission['id']}", json={"title": "Big shop"})
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Big shop"
        assert sessions.projects[ALICE] == {"proj1": "Big shop"}

    async def test_rename_clash_keeps_the_project_name(
        self, client: httpx.AsyncClient, sessions: FakeSessions
    ) -> None:
        mission = await _mission(client)
        sessions.projects[ALICE]["other"] = "Taken"
        r = await client.patch(f"{P}/missions/{mission['id']}", json={"title": "Taken"})
        assert r.status_code == 200 and r.json()["title"] == "Taken"
        assert sessions.projects[ALICE]["proj1"] == "Tiny shop"

    async def test_board_links_are_per_owner(self, client: httpx.AsyncClient) -> None:
        mine = await _mission(client)
        second = await _mission(client, "Second")
        r = await client.post(
            f"{P}/missions", json={"title": "Bob's", "repo_path": "/repo"}, headers=BOB
        )
        bobs = r.json()
        links = (await client.get(f"{P}/project-links")).json()["links"]
        assert {link["mission_id"] for link in links} == {mine["id"], second["id"]}
        assert bobs["id"] not in {link["mission_id"] for link in links}
        bob_links = (await client.get(f"{P}/project-links", headers=BOB)).json()["links"]
        assert bob_links == [
            {"project_id": bobs["project_id"], "mission_id": bobs["id"], "title": "Bob's"}
        ]
        # Bob cannot resolve alice's project to her mission either.
        r = await client.get(
            f"{P}/missions", params={"project_id": mine["project_id"]}, headers=BOB
        )
        assert r.json() == {"missions": []}
        r = await client.get(f"{P}/missions", params={"project_id": mine["project_id"]})
        assert [m["id"] for m in r.json()["missions"]] == [mine["id"]]

    async def test_links_need_an_identity(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"{P}/project-links", headers={USER_HEADER: ""})
        assert r.status_code == 401


# ── OmnigentSessionService: project_id reaches POST /v1/sessions ──


class _ProjectStubApp:
    """``/v1/sessions`` + ``/v1/projects`` stubs; a listed project id may be gone."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []
        self.gone: set[str] = set()
        self.projects: dict[str, str] = {}
        self.app = FastAPI()
        self.app.state.host_registry = _Registry(["host_a"])
        self.app.state.host_store = None

        @self.app.post("/v1/sessions", response_model=None)
        async def create(request: Request) -> dict[str, Any] | JSONResponse:
            form = await request.form()
            metadata = json.loads(str(form["metadata"]))
            self.creates.append(metadata)
            if metadata.get("project_id") in self.gone:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "not_found", "message": "Project not found"}},
                )
            return {"session_id": f"conv_{len(self.creates)}"}

        @self.app.post("/v1/sessions/{session_id}/events")
        async def events(session_id: str) -> dict[str, Any]:
            return {"ok": True}

        @self.app.get("/v1/sessions/{session_id}")
        async def get(session_id: str) -> dict[str, Any]:
            return {"status": "idle", "runner_id": "run_1"}

        @self.app.get("/v1/runners/{runner_id}/status")
        async def runner_status(runner_id: str) -> dict[str, Any]:
            return {"online": True}

        @self.app.get("/v1/projects")
        async def list_projects() -> dict[str, Any]:
            data = [{"id": k, "name": v} for k, v in self.projects.items()]
            return {"object": "list", "data": data}

        @self.app.post("/v1/projects", response_model=None)
        async def create_project(request: Request) -> dict[str, Any] | JSONResponse:
            name = (await request.json())["name"]
            if name in self.projects.values():
                return JSONResponse(status_code=409, content={"detail": "taken"})
            pid = f"p{len(self.projects) + 1}"
            self.projects[pid] = name
            return {"id": pid, "name": name}

        @self.app.patch("/v1/projects/{project_id}", response_model=None)
        async def rename(project_id: str, request: Request) -> dict[str, Any] | JSONResponse:
            if project_id not in self.projects:
                return JSONResponse(status_code=404, content={"detail": "Project not found"})
            self.projects[project_id] = (await request.json())["name"]
            return {"id": project_id, "name": self.projects[project_id]}


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "developer"
    root.mkdir()
    (root / "config.yaml").write_text("name: developer\n")
    return root


def _root(agent_dir: Path, project_id: str | None) -> RootSessionRequest:
    return RootSessionRequest(
        task_id="t1",
        title="Add login",
        prompt="# Add login",
        repo_path="/repo",
        branch="task/t1",
        agent_dir=agent_dir,
        project_id=project_id,
    )


class TestOmnigentSessionService:
    async def test_root_and_child_carry_the_project(
        self, monkeypatch: pytest.MonkeyPatch, bundle: Path
    ) -> None:
        _patch_worktrees(monkeypatch, _Worktrees(listed=[]))
        stub = _ProjectStubApp()
        service = OmnigentSessionService(stub.app, auth_provider=None)
        await service.create_root_session(_root(bundle, "p1"))
        await service.create_child_session(
            ChildSessionRequest(
                parent_session_id="conv_1",
                title="Review",
                prompt="review",
                workspace="/wt/task",
                agent_dir=bundle,
                project_id="p1",
            )
        )
        assert [c["project_id"] for c in stub.creates] == ["p1", "p1"]
        assert stub.creates[1]["parent_session_id"] == "conv_1"

    async def test_no_project_keeps_the_metadata_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, bundle: Path
    ) -> None:
        _patch_worktrees(monkeypatch, _Worktrees(listed=[]))
        stub = _ProjectStubApp()
        await OmnigentSessionService(stub.app, None).create_root_session(_root(bundle, None))
        assert "project_id" not in stub.creates[0]

    async def test_gone_project_falls_back_to_unfiled(
        self, monkeypatch: pytest.MonkeyPatch, bundle: Path
    ) -> None:
        _patch_worktrees(monkeypatch, _Worktrees(listed=[]))
        stub = _ProjectStubApp()
        stub.gone.add("p9")
        service = OmnigentSessionService(stub.app, None)
        assert await service.create_root_session(_root(bundle, "p9")) == "conv_2"
        assert [c.get("project_id") for c in stub.creates] == ["p9", None]

    async def test_project_calls(self) -> None:
        from omnigent.shipcrew.sessions import ProjectNameTaken

        stub = _ProjectStubApp()
        service = OmnigentSessionService(stub.app, None)
        created = await service.create_project("Shop", acting_user=None)
        assert created.name == "Shop"
        assert [p.id for p in await service.list_projects(acting_user=None)] == [created.id]
        with pytest.raises(ProjectNameTaken):
            await service.create_project("Shop", acting_user=None)
        renamed = await service.rename_project(created.id, "Big", acting_user=None)
        assert renamed is not None and renamed.name == "Big"
        assert await service.rename_project("nope", "X", acting_user=None) is None
