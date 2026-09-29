"""GitHub issue sync through ``gh.py``, against the fake ``gh`` (never real GitHub)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.shipcrew import gh
from omnigent.shipcrew.issue_sync import CLOSED_REASON
from omnigent.shipcrew.service import ShipcrewService

from .conftest import USER_HEADER, FakeSessions

P = "/v1/shipcrew"
REPO_URL = "https://github.com/acme/shop"
FAKE_GH = Path(__file__).with_name("fake_gh.py")


class FakeGitHub:
    """The fake ``gh``'s JSON state, read and edited by tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state))

    def edit_issue(self, number: int, **fields: Any) -> None:
        state = self.load()
        issue = next(i for i in state["issues"] if i["number"] == number)
        issue.update(fields)
        self.save(state)

    def add_pr(self, **pr: Any) -> None:
        state = self.load()
        state.setdefault("prs", []).append(pr)
        self.save(state)

    def calls(self) -> list[list[str]]:
        return self.load().get("calls", [])


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    wrapper = tmp_path / "bin" / "gh"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "gh_state.json"
    monkeypatch.setenv("SHIPCREW_GH", str(wrapper))
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    assert os.access(wrapper, os.X_OK)
    fake = FakeGitHub(state)
    fake.save({"authenticated": True})
    return fake


async def _mission(client: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {"title": "Shop", "repo_path": "/repo", "repo_url": REPO_URL, **kw}
    r = await client.post(f"{P}/missions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _task(client: httpx.AsyncClient, mission_id: str, **kw: Any) -> dict[str, Any]:
    body = {"title": "Cart", "acceptance": ["adding an item shows it"], **kw}
    r = await client.post(f"{P}/missions/{mission_id}/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _sync(client: httpx.AsyncClient, mission_id: str) -> dict[str, Any]:
    r = await client.post(f"{P}/missions/{mission_id}/sync")
    assert r.status_code == 200, r.text
    return r.json()


async def _get(client: httpx.AsyncClient, mission_id: str, task_id: str) -> dict[str, Any]:
    tasks = (await client.get(f"{P}/missions/{mission_id}/tasks")).json()["tasks"]
    return next(t for t in tasks if t["id"] == task_id)


class TestNoOp:
    async def test_without_repo_url(self, client: httpx.AsyncClient, github: FakeGitHub) -> None:
        mission = await _mission(client, repo_url=None)
        await _task(client, mission["id"])
        body = await _sync(client, mission["id"])
        assert body["sync"]["ok"] is False
        assert "no repo_url" in body["sync"]["reason"]
        assert github.calls() == []

    async def test_gh_not_logged_in(self, client: httpx.AsyncClient, github: FakeGitHub) -> None:
        github.save({"authenticated": False})
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        body = await _sync(client, mission["id"])
        assert body["sync"]["ok"] is False
        assert "not logged in" in body["sync"]["reason"]
        assert github.calls() == [["auth", "status"]]
        assert (await _get(client, mission["id"], task["id"]))["issue_number"] is None

    async def test_routes_need_identity_and_ownership(
        self, client: httpx.AsyncClient, github: FakeGitHub
    ) -> None:
        mission = await _mission(client)
        path = f"{P}/missions/{mission['id']}/sync"
        assert (await client.post(path, headers={USER_HEADER: ""})).status_code == 401
        assert (await client.post(path, headers={USER_HEADER: "bob@x"})).status_code == 404
        assert github.calls() == []


class TestIssueCreation:
    async def test_creates_one_issue_per_task(
        self, client: httpx.AsyncClient, github: FakeGitHub
    ) -> None:
        mission = await _mission(client)
        a = await _task(client, mission["id"], body="Build the cart.", role="developer")
        b = await _task(client, mission["id"], title="Audit", role="security", acceptance=[])
        body = await _sync(client, mission["id"])
        assert body["sync"] == {**body["sync"], "ok": True, "reason": None, "created": 2}
        a_now = await _get(client, mission["id"], a["id"])
        b_now = await _get(client, mission["id"], b["id"])
        assert (a_now["issue_number"], b_now["issue_number"]) == (1, 2)
        assert a_now["issue_url"] == f"{REPO_URL}/issues/1"
        issues = {i["number"]: i for i in github.load()["issues"]}
        assert issues[1]["title"] == "Cart"
        assert "Build the cart." in issues[1]["body"]
        assert "- [ ] adding an item shows it" in issues[1]["body"]
        assert [lb["name"] for lb in issues[1]["labels"]] == ["shipcrew", "role:developer"]
        assert [lb["name"] for lb in issues[2]["labels"]] == ["shipcrew", "role:security"]
        assert {"shipcrew", "shipcrew:human", "role:developer"} <= set(github.load()["labels"])
        # Every call targets the mission's repository explicitly.
        creates = [c for c in github.calls() if c[:2] == ["issue", "create"]]
        assert all(c[c.index("--repo") + 1] == REPO_URL for c in creates)

        # A second sync opens nothing new.
        again = await _sync(client, mission["id"])
        assert again["sync"]["created"] == 0
        assert len(github.load()["issues"]) == 2

    async def test_merged_tasks_get_no_issue(
        self, client: httpx.AsyncClient, github: FakeGitHub, service: ShipcrewService
    ) -> None:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        await asyncio.to_thread(service.store.update_task, task["id"], status="merged")
        assert (await _sync(client, mission["id"]))["sync"]["created"] == 0
        assert github.load().get("issues", []) == []


class TestPullFromGitHub:
    async def _running_task_with_issue(
        self, client: httpx.AsyncClient
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mission = await _mission(client)
        task = await _task(client, mission["id"])
        await _sync(client, mission["id"])
        started = (await client.post(f"{P}/tasks/{task['id']}/start")).json()
        assert started["status"] == "running"
        return mission, started

    async def test_assigned_issue_goes_to_the_human(
        self, client: httpx.AsyncClient, github: FakeGitHub, sessions: FakeSessions
    ) -> None:
        mission, task = await self._running_task_with_issue(client)
        github.edit_issue(1, assignees=[{"login": "octocat"}])
        body = await _sync(client, mission["id"])
        assert body["sync"]["updated"] == 1
        now = await _get(client, mission["id"], task["id"])
        assert now["assignee"] == {"kind": "human", "id": "octocat"}
        # The board's usual path: the agent's turn is interrupted.
        assert sessions.cancelled == ["sess1"]
        # Already human: the next sync changes nothing.
        assert (await _sync(client, mission["id"]))["sync"]["updated"] == 0

    async def test_human_label_goes_to_a_human(
        self, client: httpx.AsyncClient, github: FakeGitHub
    ) -> None:
        mission, task = await self._running_task_with_issue(client)
        github.edit_issue(1, labels=[{"name": "shipcrew"}, {"name": "shipcrew:human"}])
        await _sync(client, mission["id"])
        now = await _get(client, mission["id"], task["id"])
        assert now["assignee"]["kind"] == "human"

    async def test_closed_issue_blocks_the_task(
        self, client: httpx.AsyncClient, github: FakeGitHub, sessions: FakeSessions
    ) -> None:
        mission, task = await self._running_task_with_issue(client)
        github.edit_issue(1, state="CLOSED")
        await _sync(client, mission["id"])
        now = await _get(client, mission["id"], task["id"])
        assert (now["status"], now["blocked_reason"]) == ("blocked", CLOSED_REASON)
        assert sessions.stopped == ["sess1"]
        assert (await _sync(client, mission["id"]))["sync"]["updated"] == 0

    async def test_pr_merged_outside_shipcrew(
        self, client: httpx.AsyncClient, github: FakeGitHub, sessions: FakeSessions
    ) -> None:
        mission, task = await self._running_task_with_issue(client)
        github.add_pr(
            number=7, state="MERGED", headRefName=f"task/{task['id']}", url=f"{REPO_URL}/pull/7"
        )
        # GitHub closes the issue on merge; merged wins over "closed".
        github.edit_issue(1, state="CLOSED")
        await _sync(client, mission["id"])
        now = await _get(client, mission["id"], task["id"])
        assert now["status"] == "merged"
        assert (now["pr_number"], now["pr_url"]) == (7, f"{REPO_URL}/pull/7")
        assert now["blocked_reason"] is None
        assert sessions.stopped == ["sess1"]

    async def test_gh_failure_is_reported(
        self, client: httpx.AsyncClient, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mission = await _mission(client)
        await _task(client, mission["id"])

        def boom(*args: Any, **kwargs: Any) -> tuple[int, str]:
            raise RuntimeError("gh issue create failed: HTTP 502")

        monkeypatch.setattr(gh, "create_issue", boom)
        body = await _sync(client, mission["id"])
        assert body["sync"]["ok"] is False
        assert body["sync"]["reason"] == "GitHub sync failed: gh issue create failed: HTTP 502"


class TestPeriodicTick:
    async def test_tick_is_rate_limited_and_skips_missions_without_repo(
        self, client: httpx.AsyncClient, github: FakeGitHub, service: ShipcrewService
    ) -> None:
        with_repo = await _mission(client)
        await _mission(client, repo_url=None)
        await _task(client, with_repo["id"])
        first = await service.github.tick(now=1000.0)
        assert [(r.mission_id, r.ok, r.created) for r in first] == [(with_repo["id"], True, 1)]
        assert await service.github.tick(now=1001.0) == []
        later = await service.github.tick(now=1000.0 + service.settings.sync_interval_s)
        assert [r.mission_id for r in later] == [with_repo["id"]]

    async def test_scheduler_tick_runs_the_sync(
        self, client: httpx.AsyncClient, github: FakeGitHub, service: ShipcrewService
    ) -> None:
        from omnigent.shipcrew.scheduler import ShipcrewScheduler

        mission = await _mission(client)
        task = await _task(client, mission["id"])
        await ShipcrewScheduler(lambda: service, 60.0).tick()
        assert (await _get(client, mission["id"], task["id"]))["issue_number"] == 1
