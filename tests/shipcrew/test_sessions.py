"""``OmnigentSessionService`` against a stub ``/v1/sessions`` app and host tunnel."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.server.routes import _host_worktree
from omnigent.shipcrew.sessions import (
    OmnigentSessionService,
    RootSessionRequest,
    SessionServiceError,
    bundle_agent_dir,
    snapshot_from_payload,
)


class _Registry:
    def __init__(self, host_ids: list[str]) -> None:
        self._conns = {h: object() for h in host_ids}

    def online_host_ids(self) -> list[str]:
        return list(self._conns)

    def get(self, host_id: str) -> object | None:
        return self._conns.get(host_id)


class _StubApp:
    """Records what the service sends to omnigent's session routes."""

    def __init__(self, *, host_ids: list[str]) -> None:
        self.creates: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.session_body: dict[str, Any] | None = {"status": "running"}
        self.latest_items: list[dict[str, Any]] = []
        self.app = FastAPI()
        self.app.state.host_registry = _Registry(host_ids)
        self.app.state.host_store = None

        @self.app.post("/v1/sessions")
        async def create(request: Request) -> dict[str, Any]:
            form = await request.form()
            bundle = form["bundle"]
            assert not isinstance(bundle, str)
            self.creates.append(
                {
                    "metadata": json.loads(str(form["metadata"])),
                    "bundle": await bundle.read(),
                    "origin": request.headers.get("origin"),
                }
            )
            return {"session_id": "conv_1"}

        @self.app.post("/v1/sessions/{session_id}/events")
        async def events(session_id: str, request: Request) -> dict[str, Any]:
            self.events.append((session_id, await request.json()))
            return {"ok": True}

        @self.app.get("/v1/sessions/{session_id}/items")
        async def items(session_id: str, limit: int, order: str) -> dict[str, Any]:
            assert (limit, order) == (1, "desc")
            return {"data": self.latest_items}

        @self.app.get("/v1/sessions/{session_id}", response_model=None)
        async def get(session_id: str) -> dict[str, Any] | JSONResponse:
            if self.session_body is None:
                return JSONResponse(status_code=404, content={"detail": "gone"})
            return self.session_body


class _Worktrees:
    def __init__(self, listed: list[dict[str, Any]], *, fail_new: bool = False) -> None:
        self.listed = listed
        self.fail_new = fail_new
        self.created: list[dict[str, Any]] = []

    async def list(self, **kw: Any) -> list[dict[str, Any]]:
        return self.listed

    async def create(self, **kw: Any) -> _host_worktree.CreatedWorktree:
        self.created.append(kw)
        if self.fail_new and not kw.get("existing_branch"):
            raise _host_worktree.WorktreeProxyError("branch already exists")
        return _host_worktree.CreatedWorktree(
            worktree_path="/wt/task", branch=kw["branch_name"], workspace="/wt/task"
        )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "developer"
    (root / "skills").mkdir(parents=True)
    (root / "config.yaml").write_text("name: developer\n")
    (root / "skills" / "a.md").write_text("skill")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return root


def _request(agent_dir: Path) -> RootSessionRequest:
    return RootSessionRequest(
        task_id="t1",
        title="Add login",
        prompt="# Add login",
        repo_path="/repo",
        branch="task/t1",
        agent_dir=agent_dir,
        base_branch="main",
        labels={"shipcrew.task_id": "t1"},
    )


def _patch_worktrees(monkeypatch: pytest.MonkeyPatch, fake: _Worktrees) -> None:
    monkeypatch.setattr(_host_worktree, "list_worktrees_on_host", fake.list)
    monkeypatch.setattr(_host_worktree, "create_worktree_on_host", fake.create)


def test_bundle_skips_build_dirs(bundle: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(bundle_agent_dir(bundle)), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["config.yaml", "skills/a.md"]


def test_bundle_without_config_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SessionServiceError, match=r"config\.yaml"):
        bundle_agent_dir(tmp_path)


async def test_create_makes_worktree_session_and_sends_prompt(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    stub = _StubApp(host_ids=["host_a"])
    worktrees = _Worktrees(listed=[{"path": "/repo", "branch": "main", "is_main": True}])
    _patch_worktrees(monkeypatch, worktrees)
    service = OmnigentSessionService(stub.app, auth_provider=None)

    assert await service.create_root_session(_request(bundle)) == "conv_1"

    assert worktrees.created[0]["branch_name"] == "task/t1"
    assert worktrees.created[0]["base_branch"] == "main"
    (create,) = stub.creates
    assert create["metadata"] == {
        "title": "Add login",
        "host_id": "host_a",
        "workspace": "/wt/task",
        "labels": {"shipcrew.task_id": "t1"},
    }
    assert create["origin"] == "omnigent://internal"
    (sent,) = stub.events
    assert sent[0] == "conv_1"
    assert sent[1]["data"]["content"] == [{"type": "input_text", "text": "# Add login"}]


async def test_restart_reuses_existing_worktree(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    stub = _StubApp(host_ids=["host_a"])
    worktrees = _Worktrees(listed=[{"path": "/wt/old", "branch": "task/t1"}])
    _patch_worktrees(monkeypatch, worktrees)
    await OmnigentSessionService(stub.app, None).create_root_session(_request(bundle))
    assert worktrees.created == []
    assert stub.creates[0]["metadata"]["workspace"] == "/wt/old"


async def test_existing_branch_without_worktree_is_checked_out(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    stub = _StubApp(host_ids=["host_a"])
    worktrees = _Worktrees(listed=[], fail_new=True)
    _patch_worktrees(monkeypatch, worktrees)
    await OmnigentSessionService(stub.app, None).create_root_session(_request(bundle))
    assert [c.get("existing_branch", False) for c in worktrees.created] == [False, True]
    assert worktrees.created[1]["base_branch"] is None


async def test_no_online_host(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    stub = _StubApp(host_ids=[])
    _patch_worktrees(monkeypatch, _Worktrees(listed=[]))
    with pytest.raises(SessionServiceError, match="no online host"):
        await OmnigentSessionService(stub.app, None).create_root_session(_request(bundle))


async def test_cancel_sends_interrupt() -> None:
    stub = _StubApp(host_ids=[])
    await OmnigentSessionService(stub.app, None).cancel("conv_9", acting_user=None)
    assert stub.events == [("conv_9", {"type": "interrupt"})]


async def test_snapshot_reads_session() -> None:
    stub = _StubApp(host_ids=[])
    service = OmnigentSessionService(stub.app, None)
    stub.session_body = {
        "status": "running",
        "total_cost_usd": 1.5,
        "pending_elicitations": [{"id": "e1"}],
    }
    snap = await service.snapshot("conv_1", acting_user=None)
    assert snap is not None
    assert (snap.status, snap.awaiting_human, snap.cost_usd) == ("running", True, 1.5)
    stub.session_body = None
    assert await service.snapshot("conv_1", acting_user=None) is None


@pytest.mark.parametrize(
    ("latest", "replied"),
    [
        ([], False),
        ([{"type": "message", "role": "user"}], False),
        ([{"type": "message", "role": "assistant"}], True),
        ([{"type": "function_call_output"}], True),
    ],
)
async def test_idle_snapshot_reads_latest_item(
    latest: list[dict[str, Any]], replied: bool
) -> None:
    stub = _StubApp(host_ids=[])
    stub.session_body = {"status": "idle"}
    stub.latest_items = latest
    snap = await OmnigentSessionService(stub.app, None).snapshot("c", acting_user=None)
    assert snap is not None
    assert snap.agent_replied is replied


def test_snapshot_from_payload_failed() -> None:
    snap = snapshot_from_payload(
        {"status": "failed", "last_task_error": {"message": "rate limited"}}
    )
    assert (snap.status, snap.error, snap.cost_usd) == ("failed", "rate limited", None)
