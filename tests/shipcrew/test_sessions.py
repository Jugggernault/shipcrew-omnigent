"""``OmnigentSessionService`` against a stub ``/v1/sessions`` app and host tunnel."""

from __future__ import annotations

import dataclasses
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
    ChildSessionRequest,
    OmnigentSessionService,
    RootSessionRequest,
    SessionServiceError,
    bundle_agent_dir,
    inject_task_contract,
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
        self.session_body: dict[str, Any] | None = {"status": "running", "runner_id": "run_1"}
        self.runner_polls = 0
        self.latest_items: list[dict[str, Any]] = []
        self.children: list[dict[str, Any]] = []
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

        @self.app.get("/v1/runners/{runner_id}/status")
        async def runner_status(runner_id: str) -> dict[str, Any]:
            # Offline on the first poll, online after: the prompt must wait.
            self.runner_polls += 1
            return {"online": self.runner_polls > 1}

        @self.app.get("/v1/sessions/{session_id}/child_sessions")
        async def child_sessions(session_id: str) -> dict[str, Any]:
            return {"object": "list", "data": self.children}

        @self.app.get("/v1/sessions/{session_id}/items")
        async def items(session_id: str, limit: int, order: str) -> dict[str, Any]:
            assert order == "desc"
            return {"data": self.latest_items[:limit]}

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


def test_bundle_dereferences_symlinked_sub_agents(bundle: Path, tmp_path: Path) -> None:
    parent = tmp_path / "orchestrator"
    (parent / "agents").mkdir(parents=True)
    (parent / "config.yaml").write_text("name: orchestrator\n")
    (parent / "agents" / "worker").symlink_to(bundle, target_is_directory=True)
    with tarfile.open(fileobj=io.BytesIO(bundle_agent_dir(parent)), mode="r:gz") as tar:
        assert "agents/worker/config.yaml" in tar.getnames()


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
    assert stub.runner_polls == 2
    (sent,) = stub.events
    assert sent[0] == "conv_1"
    assert sent[1]["data"]["content"] == [{"type": "input_text", "text": "# Add login"}]


_OWNED_CONFIG = """name: developer
guardrails:
  policies:
    shipcrew_owned_paths:
      type: function
      on: [tool_call]
      function:
        path: omnigent.shipcrew.policies.owned_paths
        arguments:
          owned_paths: []  # @task.owned_paths
          root: ""  # @task.root
"""


def test_inject_task_contract_fills_the_slots() -> None:
    import yaml

    text = inject_task_contract(_OWNED_CONFIG, owned_paths=["app/**", 'we"ird'], root="/wt/t")
    args = yaml.safe_load(text)["guardrails"]["policies"]["shipcrew_owned_paths"]["function"][
        "arguments"
    ]
    assert args == {"owned_paths": ["app/**", 'we"ird'], "root": "/wt/t"}


def test_inject_task_contract_leaves_configs_without_slots() -> None:
    assert inject_task_contract("name: qa\n", owned_paths=["a/**"], root="/wt") == "name: qa\n"
    assert inject_task_contract(_OWNED_CONFIG, owned_paths=[], root="/wt") == _OWNED_CONFIG


async def test_create_injects_owned_paths_into_the_bundle(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    (bundle / "config.yaml").write_text(_OWNED_CONFIG)
    stub = _StubApp(host_ids=["host_a"])
    _patch_worktrees(monkeypatch, _Worktrees(listed=[]))
    request = dataclasses.replace(_request(bundle), owned_paths=("app/cart/**",))
    await OmnigentSessionService(stub.app, None).create_root_session(request)
    with tarfile.open(fileobj=io.BytesIO(stub.creates[0]["bundle"]), mode="r:gz") as tar:
        member = tar.extractfile("config.yaml")
        assert member is not None
        config = member.read().decode()
    assert 'owned_paths: ["app/cart/**"]' in config
    assert 'root: "/wt/task"' in config
    # the source bundle is untouched
    assert (bundle / "config.yaml").read_text() == _OWNED_CONFIG


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
        ([{"type": "resource_event", "status": "completed"}], False),
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


async def test_idle_root_with_busy_child_is_running() -> None:
    stub = _StubApp(host_ids=[])
    stub.session_body = {"status": "idle"}
    stub.latest_items = [{"type": "message", "role": "assistant"}]
    stub.children = [{"id": "child", "busy": True}]
    snap = await OmnigentSessionService(stub.app, None).snapshot("c", acting_user=None)
    assert snap is not None and snap.status == "running"


def test_idle_payload_with_background_tasks_is_running() -> None:
    assert snapshot_from_payload({"status": "idle", "background_task_count": 1}).status == (
        "running"
    )
    assert snapshot_from_payload({"status": "idle", "background_task_count": 0}).status == "idle"


def test_snapshot_from_payload_failed() -> None:
    snap = snapshot_from_payload(
        {"status": "failed", "last_task_error": {"message": "rate limited"}}
    )
    assert (snap.status, snap.error, snap.cost_usd) == ("failed", "rate limited", None)


async def test_stop_sends_stop_session() -> None:
    stub = _StubApp(host_ids=[])
    await OmnigentSessionService(stub.app, None).stop("conv_9", acting_user=None)
    assert stub.events == [("conv_9", {"type": "stop_session", "data": {}})]


async def test_failed_prompt_ends_the_created_session(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    stub = _StubApp(host_ids=["host_a"])
    _patch_worktrees(monkeypatch, _Worktrees(listed=[]))

    @stub.app.middleware("http")
    async def _reject_prompt(request: Request, call_next: Any) -> Any:
        if request.url.path.endswith("/events"):
            body = await request.body()
            if b'"message"' in body:
                return JSONResponse(status_code=500, content={"detail": "boom"})
        return await call_next(request)

    with pytest.raises(SessionServiceError, match="prompt dispatch failed"):
        await OmnigentSessionService(stub.app, None).create_root_session(_request(bundle))
    assert stub.events == [("conv_1", {"type": "stop_session", "data": {}})]


class _Host:
    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.sandbox_provider = None


class _HostStore:
    def __init__(self, owned: dict[str, list[str]]) -> None:
        self._owned = owned

    def list_hosts(self, user_id: str) -> list[_Host]:
        return [_Host(h) for h in self._owned.get(user_id, [])]


async def test_real_users_only_launch_on_their_own_hosts(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    # alice's host is online, bob owns none: bob's task must not borrow it.
    stub = _StubApp(host_ids=["host_alice"])
    stub.app.state.host_store = _HostStore({"alice@example.com": ["host_alice"]})
    worktrees = _Worktrees(listed=[])
    _patch_worktrees(monkeypatch, worktrees)
    request = dataclasses.replace(_request(bundle), acting_user="bob@example.com")
    with pytest.raises(SessionServiceError, match="no online host"):
        await OmnigentSessionService(stub.app, None).create_root_session(request)
    pinned = OmnigentSessionService(stub.app, None, host_id="host_alice")
    with pytest.raises(SessionServiceError, match="no online host"):
        await pinned.create_root_session(request)
    assert worktrees.created == []
    owner_request = dataclasses.replace(request, acting_user="alice@example.com")
    await OmnigentSessionService(stub.app, None).create_root_session(owner_request)
    assert stub.creates[0]["metadata"]["host_id"] == "host_alice"


async def test_send_message_posts_a_user_turn() -> None:
    stub = _StubApp(host_ids=[])
    await OmnigentSessionService(stub.app, None).send_message("conv_3", "fix CI", acting_user=None)
    (sent,) = stub.events
    assert sent[0] == "conv_3"
    assert sent[1]["type"] == "message"
    assert sent[1]["data"]["content"] == [{"type": "input_text", "text": "fix CI"}]


async def test_last_agent_text_is_the_latest_assistant_message() -> None:
    stub = _StubApp(host_ids=[])
    stub.latest_items = [
        {"type": "function_call"},
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "done\n"},
                {"type": "text", "text": "PASS"},
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "x"}],
        },
    ]
    service = OmnigentSessionService(stub.app, None)
    assert await service.last_agent_text("c", acting_user=None) == "done\nPASS"
    stub.latest_items = [{"type": "message", "role": "user", "content": "hi"}]
    assert await service.last_agent_text("c", acting_user=None) is None


@pytest.mark.parametrize(("parent_runner", "host_bound"), [("run_1", False), (None, True)])
async def test_child_session_co_locates_on_a_live_parent(
    bundle: Path, parent_runner: str | None, host_bound: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.shipcrew import sessions as sessions_mod

    # The stub serves one body for every id, so a runner-less parent means a
    # runner-less child too: do not wait the full runner timeout.
    monkeypatch.setattr(sessions_mod, "_RUNNER_ONLINE_TIMEOUT_S", 0.05)
    stub = _StubApp(host_ids=["host_a"])
    stub.session_body = {"status": "idle", "runner_id": parent_runner}
    request = ChildSessionRequest(
        parent_session_id="conv_root",
        title="Review: Add login",
        prompt="review this",
        workspace="/wt/task",
        agent_dir=bundle,
        labels={"shipcrew.role": "reviewer"},
    )
    assert await OmnigentSessionService(stub.app, None).create_child_session(request) == "conv_1"
    (create,) = stub.creates
    metadata = create["metadata"]
    assert metadata["parent_session_id"] == "conv_root"
    assert metadata["workspace"] == "/wt/task"
    # A co-located child must not be host-bound: stopping it would tear down
    # the parent's runner.
    assert ("host_id" in metadata) is host_bound
    assert stub.events[-1][1]["data"]["content"][0]["text"] == "review this"
