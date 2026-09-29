"""The ship stage: readiness, the devops deploy session, the URL check, restarts."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine

from omnigent.shipcrew.events import MissionEventBus
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import SessionSnapshot
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.ship import (
    check_url,
    parse_deploy_reply,
    ship_readiness,
    vercel_preflight,
    vercel_project_name,
    verify_url,
)
from omnigent.shipcrew.store import Assignee, Mission, ShipcrewStore, Task

from .conftest import USER_HEADER, FakeSessions

P = "/v1/shipcrew"
BOB = {USER_HEADER: "bob@example.com"}


@pytest.fixture
def settings(tmp_path: Path, agents_dir: Path) -> ShipcrewSettings:
    (agents_dir / "devops").mkdir()
    (agents_dir / "devops" / "config.yaml").write_text("name: devops\n")
    return ShipcrewSettings(
        agents_dir=agents_dir,
        max_parallel=2,
        max_usd=10.0,
        scheduler_enabled=False,
        pr_loop_enabled=False,
        db_url=f"sqlite:///{tmp_path / 'shipcrew.db'}",
        ship_verify_s=1.0,
        ship_verify_interval_s=0.05,
        ship_allow_private_urls=True,
    )


@pytest.fixture(autouse=True)
def _no_preflight(service: ShipcrewService) -> None:
    service.ship.preflight = lambda: None


class _Site:
    """A local HTTP server answering a scripted list of status codes (the last repeats)."""

    def __init__(self) -> None:
        self.statuses: list[int] = [200]
        self.hits = 0
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index = min(site.hits, len(site.statuses) - 1)
                site.hits += 1
                code = site.statuses[index]
                if code in (301, 302):
                    self.send_response(code)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    return
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site() -> Iterator[_Site]:
    s = _Site()
    yield s
    s.close()


async def _mission(service: ShipcrewService, owner: str | None = "alice@example.com") -> Mission:
    return await service.create_mission(
        "Tiny CLI", "/nowhere/tiny-cli", "https://github.com/acme/Tiny_CLI.git", owner
    )


async def _task(service: ShipcrewService, mission: Mission, title: str, **fields: Any) -> Task:
    task = await service.create_task(mission.id, title=title)
    if fields:
        updated = await asyncio.to_thread(service.store.update_task, task.id, **fields)
        assert updated is not None
        return updated
    return task


async def _state(service: ShipcrewService, mission_id: str) -> Mission:
    return await service.require_mission(mission_id)


def _finish_deploy(sessions: FakeSessions, session_id: str, text: str) -> None:
    sessions.snapshots[session_id] = SessionSnapshot(
        status="idle", agent_replied=True, cost_usd=0.4
    )
    sessions.agent_texts[session_id] = text


class TestReadiness:
    def _t(self, status: str, human: bool = False, title: str = "t") -> Task:
        return Task(
            id=title,
            mission_id="m",
            title=title,
            status=status,
            assignee=Assignee("human", "ana") if human else None,
        )

    def test_all_merged_ignoring_human_cards(self) -> None:
        tasks = [self._t("merged"), self._t("backlog", human=True), self._t("blocked", True)]
        assert ship_readiness(tasks).ok

    def test_blocked_or_intervention_card_prevents_ship_with_reason(self) -> None:
        tasks = [
            self._t("merged"),
            self._t("blocked", title="Cart"),
            self._t("intervention", title="QA"),
        ]
        verdict = ship_readiness(tasks)
        assert not verdict.ok and verdict.stuck
        assert verdict.reason == "not shipping: 2 cards need a human: 'Cart', 'QA'"

    def test_pending_work_is_not_stuck(self) -> None:
        verdict = ship_readiness([self._t("merged"), self._t("running", title="API")])
        assert not verdict.ok and not verdict.stuck
        assert verdict.reason == "1 task is not merged yet: 'API'"

    def test_no_agent_task(self) -> None:
        assert not ship_readiness([self._t("merged", human=True)]).ok
        assert not ship_readiness([]).ok


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "path", "name"),
        [
            ("https://github.com/acme/Tiny_CLI.git", "/x", "tiny_cli"),
            ("git@github.com:acme/my-app.git", "/x", "my-app"),
            ("https://github.com/acme/web/", "/x", "web"),
            (None, "/work/My Great App!!", "my-great-app"),
            (None, "/work/---", "shipcrew-app"),
        ],
    )
    def test_vercel_project_name(self, url: str | None, path: str, name: str) -> None:
        assert vercel_project_name(url, path) == name

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Linked.\nDecisions: none\nDEPLOYED: https://tiny.vercel.app", ("deployed", "https://tiny.vercel.app")),
            ("**DEPLOYED:** `https://tiny.vercel.app`.", ("deployed", "https://tiny.vercel.app")),
            ("DEPLOYED: <https://tiny.vercel.app>", ("deployed", "https://tiny.vercel.app")),
            ("DEPLOYED: https://a.vercel.app\nThanks!", ("deployed", "https://a.vercel.app")),
            ("FAIL: missing env vars: DATABASE_URL", ("fail", "missing env vars: DATABASE_URL")),
            ("All done.", None),
            (None, None),
        ],
    )  # fmt: skip
    def test_parse_deploy_reply(self, text: str | None, expected: Any) -> None:
        assert parse_deploy_reply(text) == expected

    @pytest.mark.parametrize(
        ("url", "ok"),
        [
            ("https://tiny.vercel.app", True),
            ("https://tiny-abc123-acme.vercel.app/path?q=1", True),
            ("http://tiny.vercel.app", False),
            ("https://127.0.0.1:3000", False),
            ("https://localhost", False),
            ("https://10.0.0.8", False),
            ("https://metadata.internal", False),
            ("https://user:pw@tiny.vercel.app", False),
            ("https://intranet", False),
            ("ftp://tiny.vercel.app", False),
        ],
    )
    def test_check_url_refuses_private_targets(self, url: str, ok: bool) -> None:
        assert (check_url(url) is None) is ok
        assert check_url("http://127.0.0.1:8000/", allow_private=True) is None

    def test_preflight_with_a_fake_vercel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "vercel"
        fake.write_text(
            "#!/bin/sh\n"
            '[ "$1" = whoami ] && [ -z "$FAKE_LOGGED_OUT" ] && echo ana && exit 0\n'
            "echo 'Error: not logged in' >&2\nexit 1\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("SHIPCREW_VERCEL", str(fake))
        assert vercel_preflight() is None
        monkeypatch.setenv("FAKE_LOGGED_OUT", "1")
        problem = vercel_preflight()
        assert problem is not None and "not logged in" in problem
        assert "Error: not logged in" in problem

    def test_preflight_without_vercel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("omnigent.shipcrew.tools.resolve", lambda tool: None)
        problem = vercel_preflight()
        assert problem is not None and "not found" in problem


class TestVerifyUrl:
    async def test_ok_first_time(self, site: _Site) -> None:
        result = await verify_url(site.url, until=0, interval_s=0.01)
        assert result.ok and result.attempts == 1 and result.status == 200

    async def test_retries_until_the_deployment_answers(self, site: _Site) -> None:
        site.statuses = [503, 404, 302, 200]
        result = await verify_url(site.url, until=time.time() + 60, interval_s=0.01)
        # The redirect is followed within one attempt.
        assert result.ok and result.attempts == 3
        assert result.final_url is not None and result.final_url.endswith("/final")

    async def test_protected_counts_as_live_with_a_note(self, site: _Site) -> None:
        site.statuses = [401]
        result = await verify_url(site.url, until=0, interval_s=0.01)
        assert result.ok and result.status == 401
        assert result.note is not None and "protected" in result.note

    async def test_gives_up_at_the_deadline(self, site: _Site) -> None:
        site.statuses = [500]
        result = await verify_url(site.url, until=time.time() + 0.2, interval_s=0.05)
        assert not result.ok and result.attempts >= 2
        assert result.error is not None and "last: HTTP 500" in result.error

    async def test_connection_refused_is_retried_then_reported(self) -> None:
        result = await verify_url("http://127.0.0.1:9/", until=0, interval_s=0.01)
        assert not result.ok and result.status is None
        assert result.error is not None and "ConnectError" in result.error


class TestAutoShip:
    async def test_ships_only_when_every_agent_task_is_merged(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        a = await _task(service, mission, "A", status="merged")
        b = await _task(service, mission, "B", status="review")
        await _task(service, mission, "Human", assignee=Assignee("human", "ana"))
        await service.ship.tick()
        assert sessions.created == []
        assert (await _state(service, mission.id)).ship_status == "idle"
        await asyncio.to_thread(service.store.update_task, b.id, status="merged")
        async with service.bus.subscribe(mission.id) as queue:
            await service.ship.tick()
            await asyncio.sleep(0)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert len(sessions.created) == 1
        request = sessions.created[0]
        assert request.agent_dir.name == "devops"
        assert request.task_id == f"ship-{mission.id}"
        assert request.branch.startswith(f"shipcrew/{mission.id[:8]}-ship-")
        assert request.labels["shipcrew.ship"] == "deploy"
        assert request.acting_user == "alice@example.com"
        # Filed in the mission's omnigent project, like every mission session.
        assert mission.project_id and request.project_id == mission.project_id
        assert "vercel link --yes --project tiny_cli" in request.prompt
        assert "vercel deploy --prod --yes" in request.prompt
        assert "DEPLOYED: <https production url>" in request.prompt
        state = await _state(service, mission.id)
        assert (state.ship_status, state.ship_session_id) == ("deploying", "sess1")
        assert state.ship_branch == request.branch and state.ship_started_at
        assert events and events[-1]["mission"]["ship"]["status"] == "deploying"
        # One run: the next tick polls the session, it does not start another.
        await service.ship.tick()
        assert len(sessions.created) == 1
        assert a.id  # the merged card stays as it is

    async def test_a_blocked_card_prevents_the_ship_and_says_why(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        qa = await _task(service, mission, "QA", status="blocked", blocked_reason="FAIL")
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert sessions.created == []
        assert state.ship_status == "idle"
        assert state.ship_error == "not shipping: 1 card needs a human: 'QA'"
        await asyncio.to_thread(service.store.update_task, qa.id, status="ready")
        await service.ship.tick()
        assert (await _state(service, mission.id)).ship_error is None

    async def test_auto_ship_off_and_planning_missions_wait(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        r = await client.patch(f"{P}/missions/{mission.id}", json={"auto_ship": False})
        assert r.status_code == 200 and r.json()["auto_ship"] is False
        await service.ship.tick()
        await asyncio.to_thread(
            service.store.update_mission, mission.id, auto_ship=True, plan_status="running"
        )
        await service.ship.tick()
        assert sessions.created == []

    async def test_disabled_by_setting(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        service.settings = _replace_settings(service.settings, ship_enabled=False)
        await service.ship.tick()
        assert sessions.created == []

    async def test_scheduler_tick_ships_right_after_the_last_merge(
        self, app_and_service: Any, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        await app_and_service[0].state.shipcrew_scheduler.tick()
        assert (await _state(service, mission.id)).ship_status == "deploying"


def _replace_settings(settings: ShipcrewSettings, **fields: Any) -> ShipcrewSettings:
    import dataclasses

    return dataclasses.replace(settings, **fields)


class TestDeployAndVerify:
    async def _shipping(self, service: ShipcrewService) -> Mission:
        mission = await _mission(service)
        await _task(
            service,
            mission,
            "A",
            status="merged",
            cost_usd=1.5,
            started_at=1000.0,
            decisions=["kept it small"],
        )
        await service.ship.tick()
        return mission

    async def test_deployed_verified_done_with_report(
        self, service: ShipcrewService, sessions: FakeSessions, site: _Site
    ) -> None:
        site.statuses = [503, 200]
        mission = await self._shipping(service)
        _finish_deploy(
            sessions,
            "sess1",
            "Linked and deployed.\nDecisions:\n- project name from the repo\n"
            f"DEPLOYED: {site.url}",
        )
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert state.ship_status == "verifying"
        assert state.ship_url == site.url
        assert state.ship_decisions == ["project name from the repo"]
        assert state.ship_cost_usd == 0.4
        assert "sess1" in sessions.stopped  # the agent is freed before the URL check
        await service.ship.drain()
        state = await _state(service, mission.id)
        assert state.ship_status == "done"
        assert state.status == "done"
        assert site.hits == 2
        assert state.ship_finished_at is not None and state.ship_error is None
        report = state.ship_report_md or ""
        assert f"- **Deployment:** {site.url}" in report
        assert "- **Repository:** https://github.com/acme/Tiny_CLI.git" in report
        assert "- **Total cost:** $1.90 (tasks $1.50, deploy $0.40)" in report
        assert "### Deploy\n\n- project name from the repo" in report
        assert "### A\n\n- kept it small" in report

    async def test_protected_deployment_is_done_with_a_note(
        self, service: ShipcrewService, sessions: FakeSessions, site: _Site
    ) -> None:
        site.statuses = [401]
        mission = await self._shipping(service)
        _finish_deploy(sessions, "sess1", f"DEPLOYED: {site.url}")
        await service.ship.tick()
        await service.ship.drain()
        state = await _state(service, mission.id)
        assert state.ship_status == "done"
        assert state.ship_note is not None and "HTTP 401" in state.ship_note
        assert "- **Note:** The deployment answers HTTP 401" in (state.ship_report_md or "")

    async def test_url_that_never_answers_fails(
        self, service: ShipcrewService, sessions: FakeSessions, site: _Site
    ) -> None:
        site.statuses = [404]
        mission = await self._shipping(service)
        _finish_deploy(sessions, "sess1", f"DEPLOYED: {site.url}")
        await service.ship.tick()
        await service.ship.drain()
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert state.ship_error is not None and "last: HTTP 404" in state.ship_error
        assert site.hits >= 2
        assert "- **Status:** Ship failed:" in (state.ship_report_md or "")
        assert state.status != "done"

    async def test_the_agents_fail_line_fails_the_ship(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await self._shipping(service)
        _finish_deploy(
            sessions, "sess1", "Build needs env.\nDecisions: none\nFAIL: missing env vars: API_KEY"
        )
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert state.ship_error == "deploy failed: missing env vars: API_KEY"
        assert state.ship_report_md and "missing env vars: API_KEY" in state.ship_report_md
        assert "sess1" in sessions.stopped

    async def test_no_verdict_or_an_unsafe_url_fails(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await self._shipping(service)
        _finish_deploy(sessions, "sess1", "I deployed it, trust me.")
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert "DEPLOYED: <url>" in (state.ship_error or "")
        service.settings = _replace_settings(service.settings, ship_allow_private_urls=False)
        await service.ship.start(mission.id, None, manual=True)
        _finish_deploy(sessions, "sess2", "DEPLOYED: https://169.254.169.254/latest")
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert "IP address" in (state.ship_error or "")

    async def test_session_failure_and_preflight_failure(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await self._shipping(service)
        sessions.snapshots["sess1"] = SessionSnapshot(status="failed", error="model overloaded")
        await service.ship.tick()
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert state.ship_error == "the deploy session failed: model overloaded"
        service.ship.preflight = lambda: "vercel is not logged in on the server machine"
        await service.ship.start(mission.id, None, manual=True)
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert state.ship_error == "vercel is not logged in on the server machine"
        assert len(sessions.created) == 1  # no agent session for a failed preflight

    async def test_missing_devops_bundle_fails(
        self, service: ShipcrewService, sessions: FakeSessions, agents_dir: Path
    ) -> None:
        (agents_dir / "devops" / "config.yaml").unlink()
        mission = await self._shipping(service)
        state = await _state(service, mission.id)
        assert state.ship_status == "failed"
        assert "no agent bundle for role 'devops'" in (state.ship_error or "")
        assert sessions.created == []


class TestShipRoute:
    async def test_acl_and_readiness(
        self,
        client: httpx.AsyncClient,
        app_and_service: Any,
        service: ShipcrewService,
        sessions: FakeSessions,
    ) -> None:
        mission = await _mission(service)
        task = await _task(service, mission, "A", status="blocked", blocked_reason="QA FAIL")
        url = f"{P}/missions/{mission.id}/ship"
        assert (await client.post(url, headers=BOB)).status_code == 404
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_and_service[0]), base_url="http://test"
        ) as anonymous:
            assert (await anonymous.post(url)).status_code == 401
        r = await client.post(url)
        assert r.status_code == 409
        assert r.json()["error"]["message"] == "not shipping: 1 card needs a human: 'A'"
        await asyncio.to_thread(service.store.update_task, task.id, status="merged")
        r = await client.post(url)
        assert r.status_code == 200, r.text
        assert r.json()["ship"]["status"] == "deploying"
        assert r.json()["ship"]["session_id"] == "sess1"
        again = await client.post(url)
        assert again.status_code == 409
        assert "already shipping" in again.json()["error"]["message"]
        assert len(sessions.created) == 1

    async def test_ship_command(self, client: httpx.AsyncClient, service: ShipcrewService) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        r = await client.post(f"{P}/missions/{mission.id}/command", json={"text": "déploie"})
        assert r.status_code == 200, r.text
        assert r.json()["intent"] == "ship"
        assert r.json()["message"] == "Deploying to Vercel."
        assert r.json()["mission"]["ship"]["status"] == "deploying"

    async def test_manual_reship_after_done(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        await asyncio.to_thread(service.store.update_mission, mission.id, ship_status="done")
        await service.ship.tick()
        assert sessions.created == []  # auto ship never repeats a finished ship
        r = await client.post(f"{P}/missions/{mission.id}/ship")
        assert r.status_code == 200 and r.json()["ship"]["status"] == "deploying"
        assert r.json()["ship"]["url"] is None and r.json()["ship"]["report_md"] is None


class TestRestart:
    def _restarted(self, service: ShipcrewService, sessions: FakeSessions) -> ShipcrewService:
        """A new service over the same database, as after a server restart."""
        assert service.settings.db_url is not None
        fresh = ShipcrewService(
            ShipcrewStore(create_engine(service.settings.db_url)),
            MissionEventBus(),
            sessions,
            service.settings,
        )
        fresh.ship.preflight = lambda: None
        return fresh

    async def test_deploying_resumes_polling(
        self, service: ShipcrewService, sessions: FakeSessions, site: _Site
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        await service.ship.tick()
        fresh = self._restarted(service, sessions)
        await fresh.ship.tick()  # still running
        assert (await _state(fresh, mission.id)).ship_status == "deploying"
        _finish_deploy(sessions, "sess1", f"DEPLOYED: {site.url}")
        await fresh.ship.tick()
        await fresh.ship.drain()
        assert (await _state(fresh, mission.id)).ship_status == "done"
        assert len(sessions.created) == 1

    async def test_verifying_resumes_the_url_check(
        self, service: ShipcrewService, sessions: FakeSessions, site: _Site
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        await asyncio.to_thread(
            service.store.update_mission,
            mission.id,
            ship_status="verifying",
            ship_url=site.url,
            ship_verify_until=time.time() + 5,
        )
        fresh = self._restarted(service, sessions)
        await fresh.ship.tick()
        await fresh.ship.drain()
        assert (await _state(fresh, mission.id)).ship_status == "done"

    async def test_a_start_cut_off_by_a_restart_fails_cleanly(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        await _task(service, mission, "A", status="merged")
        await asyncio.to_thread(service.store.update_mission, mission.id, ship_status="deploying")
        fresh = self._restarted(service, sessions)
        await fresh.ship.tick()
        state = await _state(fresh, mission.id)
        assert state.ship_status == "failed"
        assert "interrupted" in (state.ship_error or "")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class TestWorktree:
    async def test_the_ship_worktree_is_removed_after_the_deploy(
        self, service: ShipcrewService, sessions: FakeSessions, tmp_path: Path, site: _Site
    ) -> None:
        origin = tmp_path / "origin.git"
        repo = tmp_path / "repo"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(tmp_path, "clone", "-q", str(origin), str(repo))
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
        (repo / "README.md").write_text("hi\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, env=env)
        mission = await service.create_mission("Web", str(repo), None, None)
        await _task(service, mission, "A", status="merged")
        await service.ship.tick()
        request = sessions.created[0]
        assert request.base_branch == "origin/main"
        # The host creates the worktree for the session (the fake does not).
        wt = tmp_path / "ship-wt"
        _git(repo, "worktree", "add", "-q", "-b", request.branch, str(wt), "origin/main")
        _finish_deploy(sessions, "sess1", f"DEPLOYED: {site.url}")
        await service.ship.tick()
        assert not wt.exists()
        assert request.branch not in _git(repo, "branch", "--list")
        await service.ship.drain()
        assert (await _state(service, mission.id)).ship_status == "done"


class TestInterventionLog:
    async def test_each_entry_into_intervention_is_recorded(
        self, service: ShipcrewService
    ) -> None:
        mission = await _mission(service)
        task = await _task(service, mission, "A")
        store = service.store
        await asyncio.to_thread(
            store.update_task, task.id, status="intervention", blocked_reason="needs approval: x"
        )
        # Staying in intervention adds nothing.
        await asyncio.to_thread(store.update_task, task.id, status="intervention")
        await asyncio.to_thread(store.update_task, task.id, status="running", blocked_reason=None)
        # A guardrail ask (the session maps to intervention without a reason).
        await asyncio.to_thread(store.update_task, task.id, status="intervention")
        stored = await service.require_task(task.id)
        assert [i["reason"] for i in stored.interventions] == [
            "needs approval: x",
            "the agent asked a human (approval or input)",
        ]
        assert all(isinstance(i["at"], float) for i in stored.interventions)
        assert stored.to_api()["interventions"] == stored.interventions

    async def test_start_records_the_first_start_time(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await _mission(service)
        task = await _task(service, mission, "A")
        started = await service.start_task(task.id, None)
        assert started.started_at is not None
        first = started.started_at
        await asyncio.to_thread(service.store.update_task, task.id, status="ready")
        again = await service.start_task(task.id, None)
        assert again.started_at == first
