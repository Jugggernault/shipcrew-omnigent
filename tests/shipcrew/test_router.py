"""``/v1/shipcrew`` CRUD, auth and SSE."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.shipcrew.service import ShipcrewService

from .conftest import USER_HEADER

P = "/v1/shipcrew"


async def _mission(client: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {"title": "Ship it", "repo_path": "/repo", **kw}
    r = await client.post(f"{P}/missions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _task(client: httpx.AsyncClient, mission_id: str, **kw: Any) -> dict[str, Any]:
    r = await client.post(f"{P}/missions/{mission_id}/tasks", json={"title": "T", **kw})
    assert r.status_code == 200, r.text
    return r.json()


class TestAuth:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/missions"),
            ("POST", "/missions"),
            ("GET", "/missions/x/tasks"),
            ("POST", "/missions/x/tasks"),
            ("PATCH", "/tasks/x"),
            ("POST", "/tasks/x/start"),
            ("POST", "/tasks/x/stop"),
            ("POST", "/tasks/x/approve"),
            ("POST", "/tasks/x/request-changes"),
            ("GET", "/missions/x/stream"),
        ],
    )
    async def test_every_route_requires_identity(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> None:
        body = {"title": "t", "repo_path": "/r", "message": "m"} if method != "GET" else None
        r = await client.request(method, P + path, json=body, headers={USER_HEADER: ""})
        assert r.status_code == 401

    async def test_mission_records_its_creator(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        created = await _mission(client)
        mission = await service.require_mission(created["id"])
        assert mission.owner_user_id == "alice@example.com"


class TestOwnership:
    """A mission and its tasks are visible to, and driven by, their creator only."""

    async def test_other_users_missions_are_hidden(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        bob = {USER_HEADER: "bob@example.com"}
        listed = await client.get(f"{P}/missions", headers=bob)
        assert listed.json() == {"missions": []}
        for method, path in [
            ("GET", f"/missions/{mission['id']}/tasks"),
            ("POST", f"/missions/{mission['id']}/tasks"),
            ("GET", f"/missions/{mission['id']}/stream"),
            ("PATCH", f"/tasks/{task['id']}"),
            ("POST", f"/tasks/{task['id']}/start"),
            ("POST", f"/tasks/{task['id']}/stop"),
            ("POST", f"/tasks/{task['id']}/approve"),
            ("POST", f"/tasks/{task['id']}/request-changes"),
        ]:
            body = {"title": "t", "message": "m"} if method != "GET" else None
            r = await client.request(method, P + path, json=body, headers=bob)
            assert r.status_code == 404, (method, path, r.text)
        # Nothing bob sent reached alice's card.
        (alice_task,) = (await client.get(f"{P}/missions/{mission['id']}/tasks")).json()["tasks"]
        assert (alice_task["title"], alice_task["status"]) == ("T", "backlog")


class TestMissions:
    async def test_create_and_list(self, client: httpx.AsyncClient) -> None:
        created = await _mission(client, repo_url="https://github.com/o/r")
        assert created["status"] == "planning"
        assert set(created) == {
            "id", "title", "repo_path", "repo_url", "status", "created_at", "plan"
        }  # fmt: skip
        r = await client.get(f"{P}/missions")
        assert r.json() == {"missions": [created]}

    async def test_relative_repo_path_rejected(self, client: httpx.AsyncClient) -> None:
        r = await client.post(f"{P}/missions", json={"title": "x", "repo_path": "repo"})
        assert r.status_code == 422

    async def test_unknown_mission_tasks_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{P}/missions/nope/tasks")).status_code == 404


class TestTasks:
    async def test_create_defaults_match_contract(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"], acceptance=["works"], owned_paths=["src/**"])
        assert task["status"] == "backlog"
        assert task["role"] == "developer"
        assert task["assignee"] is None
        assert task["ci"] == "none"
        assert task["cost_usd"] == 0.0
        assert task["acceptance"] == ["works"]
        assert task["owned_paths"] == ["src/**"]
        assert set(task) == {
            "id", "mission_id", "title", "body", "acceptance", "status", "assignee", "role",
            "depends_on", "owned_paths", "issue_number", "issue_url", "pr_number", "pr_url", "ci",
            "root_session_id", "cost_usd", "position", "blocked_reason", "created_at",
            "updated_at", "branch", "ci_attempts", "review", "needs_human_approval",
            "approval_reasons",
        }  # fmt: skip
        assert (task["branch"], task["ci_attempts"], task["review"]) == (None, 0, None)
        assert (task["needs_human_approval"], task["approval_reasons"]) == (False, [])

    async def test_list_ordered_by_position(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        a = await _task(client, mission["id"], title="a")
        b = await _task(client, mission["id"], title="b")
        assert b["position"] > a["position"]
        await client.patch(f"{P}/tasks/{b['id']}", json={"position": a["position"] - 1})
        r = await client.get(f"{P}/missions/{mission['id']}/tasks")
        assert [t["title"] for t in r.json()["tasks"]] == ["b", "a"]

    async def test_patch_fields(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        r = await client.patch(
            f"{P}/tasks/{task['id']}",
            json={
                "title": "New",
                "body": "b",
                "acceptance": ["x"],
                "owned_paths": ["a/**"],
                "assignee": {"kind": "agent", "id": "developer"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert (body["title"], body["body"], body["acceptance"]) == ("New", "b", ["x"])
        assert body["assignee"] == {"kind": "agent", "id": "developer"}
        r = await client.patch(f"{P}/tasks/{task['id']}", json={"assignee": None})
        assert r.json()["assignee"] is None

    async def test_depends_on_validated(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        a = await _task(client, mission["id"])
        b = await _task(client, mission["id"], depends_on=[a["id"]])
        assert b["depends_on"] == [a["id"]]
        r = await client.patch(f"{P}/tasks/{a['id']}", json={"depends_on": [b["id"]]})
        assert r.status_code == 400
        assert "cycle" in r.json()["error"]["message"]
        r = await client.post(
            f"{P}/missions/{mission['id']}/tasks", json={"title": "c", "depends_on": ["ghost"]}
        )
        assert r.status_code == 400

    async def test_role_cannot_escape_agents_dir(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        r = await client.post(
            f"{P}/missions/{mission['id']}/tasks", json={"title": "x", "role": "../etc"}
        )
        assert r.status_code == 422

    async def test_unknown_status_rejected(self, client: httpx.AsyncClient) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        r = await client.patch(f"{P}/tasks/{task['id']}", json={"status": "done"})
        assert r.status_code == 422
        r = await client.patch(f"{P}/tasks/{task['id']}", json={"status": "intervention"})
        assert r.status_code == 400

    async def test_patch_unknown_task_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.patch(f"{P}/tasks/nope", json={"title": "x"})).status_code == 404

    async def test_moving_to_ready_clears_blocked_reason(
        self, client: httpx.AsyncClient, service: ShipcrewService
    ) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        await service.patch_task(task["id"], {"status": "blocked"}, None)
        service.store.update_task(task["id"], blocked_reason="stopped by user")
        r = await client.patch(f"{P}/tasks/{task['id']}", json={"status": "ready"})
        assert r.json()["status"] == "ready"
        assert r.json()["blocked_reason"] is None


class _SSEStream:
    """Drive the ASGI app directly: ``httpx.ASGITransport`` buffers whole bodies,
    so it can never observe an endless SSE response."""

    def __init__(self, app: FastAPI, path: str, count: int) -> None:
        self.events: list[dict[str, Any]] = []
        self.status: int | None = None
        self.content_type = ""
        self._count = count
        self._buf = ""
        self._done = asyncio.Event()
        self._requested = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(USER_HEADER.lower().encode(), b"alice@example.com")],
            "client": ("test", 1),
            "server": ("test", 80),
        }
        self._app_task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> dict[str, Any]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._done.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            headers = dict(message.get("headers", []))
            self.content_type = headers.get(b"content-type", b"").decode()
        elif message["type"] == "http.response.body":
            self._buf += message.get("body", b"").decode()
            while "\n\n" in self._buf:
                chunk, self._buf = self._buf.split("\n\n", 1)
                if chunk.startswith("data: ") and not self._done.is_set():
                    self.events.append(json.loads(chunk[len("data: ") :]))
                    if len(self.events) >= self._count:
                        self._done.set()

    async def wait(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        await asyncio.wait_for(self._done.wait(), timeout)
        await asyncio.wait_for(self._app_task, timeout)
        return self.events


async def _subscribed(service: ShipcrewService, mission_id: str) -> None:
    for _ in range(200):
        if service.bus.subscriber_count(mission_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream never subscribed")


class TestStream:
    async def test_snapshot_then_task_updated(
        self,
        client: httpx.AsyncClient,
        app_and_service: tuple[FastAPI, ShipcrewService],
    ) -> None:
        app, service = app_and_service
        mission = await _mission(client)
        existing = await _task(client, mission["id"], title="existing")
        stream = _SSEStream(app, f"{P}/missions/{mission['id']}/stream", count=3)
        await _subscribed(service, mission["id"])
        created = await _task(client, mission["id"], title="new")
        await client.patch(f"{P}/tasks/{created['id']}", json={"title": "renamed"})
        events = await stream.wait()
        assert stream.status == 200
        assert stream.content_type.startswith("text/event-stream")
        assert events[0] == {"type": "task.updated", "task": existing}
        assert events[1]["type"] == "task.updated"
        assert events[1]["task"]["id"] == created["id"]
        assert events[2]["task"]["title"] == "renamed"
        # The subscription is released once the client goes away.
        assert service.bus.subscriber_count(mission["id"]) == 0

    async def test_stream_scoped_to_mission(
        self,
        client: httpx.AsyncClient,
        app_and_service: tuple[FastAPI, ShipcrewService],
    ) -> None:
        app, service = app_and_service
        m1 = await _mission(client)
        m2 = await _mission(client)
        stream = _SSEStream(app, f"{P}/missions/{m1['id']}/stream", count=1)
        await _subscribed(service, m1["id"])
        await _task(client, m2["id"], title="other mission")
        mine = await _task(client, m1["id"], title="mine")
        assert await stream.wait() == [{"type": "task.updated", "task": mine}]

    async def test_task_deleted_event_shape(
        self,
        client: httpx.AsyncClient,
        app_and_service: tuple[FastAPI, ShipcrewService],
    ) -> None:
        app, service = app_and_service
        mission = await _mission(client)
        stream = _SSEStream(app, f"{P}/missions/{mission['id']}/stream", count=1)
        await _subscribed(service, mission["id"])
        service.bus.task_deleted(mission["id"], "gone")
        assert await stream.wait() == [{"type": "task.deleted", "id": "gone"}]

    async def test_unknown_mission_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{P}/missions/nope/stream")).status_code == 404
