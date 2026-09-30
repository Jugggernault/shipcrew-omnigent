"""How shipcrew drives omnigent sessions.

:class:`SessionService` is the seam the orchestrator talks to; tests replace
it. :class:`OmnigentSessionService` is the real implementation: it creates the
task's git worktree through the host tunnel, then goes through omnigent's own
public ``/v1/sessions`` API in-process, so all of omnigent's validation, host
launch and permission logic applies unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import logging
import posixpath
import re
import tarfile
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from omnigent.shipcrew.harness import apply_worker_harness
from omnigent.shipcrew.worktree_prep import prepare_worktree

_logger = logging.getLogger(__name__)

# Minted in-process bearer tokens only need to outlive one request.
_TOKEN_TTL_S = 300
_INTERNAL_BASE_URL = "http://127.0.0.1"
_BUNDLE_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
_RUNNER_ONLINE_TIMEOUT_S = 60.0
_RUNNER_POLL_S = 0.25
# A first turn the runner rejects before any agent output (live: claude-sdk's
# "turn failed (status 204)", code runner_error, a duplicate-delivery race
# right after session create) is re-sent once after this pause.
_FIRST_TURN_RETRY_CODE = "runner_error"
_FIRST_TURN_RETRY_BACKOFF_S = 2.0
# After the re-send, a still-"failed" snapshot is the stale first failure for
# this long (the retried turn has not reached the runner yet).
_FIRST_TURN_RETRY_GRACE_S = 30.0
_TRUST_LOCK = threading.Lock()


class SessionServiceError(RuntimeError):
    """A session operation failed; the message is shown as ``blocked_reason``."""


class ProjectNameTaken(SessionServiceError):
    """``POST``/``PATCH /v1/projects`` answered 409 (the owner has that name)."""


@dataclass(frozen=True)
class ProjectRef:
    """An omnigent project as its owner sees it (``GET /v1/projects``)."""

    id: str
    name: str


@dataclass(frozen=True)
class RootSessionRequest:
    """Everything needed to start a task's root session.

    :param branch: Git branch for the task worktree, e.g. ``"shipcrew/<id8>-<slug>"``.
    :param acting_user: Identity the session is created for (its owner).
    :param workspace: Run in this directory as is (no worktree), e.g. the
        planner in the mission repo; ``branch`` is then ignored.
    :param project_id: The mission's omnigent project the session is filed in.
    """

    task_id: str
    title: str
    prompt: str
    repo_path: str
    branch: str
    agent_dir: Path
    acting_user: str | None = None
    base_branch: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    workspace: str | None = None
    owned_paths: tuple[str, ...] = ()
    project_id: str | None = None
    # The mission's other active tasks at start: ``{"title", "owned_paths"}``
    # each (their files are DENY for this task, see policies.owned_paths).
    other_tasks: tuple[dict[str, Any], ...] = ()
    # "claude-sdk" renders a claude-native bundle headless (see harness.py);
    # None / "claude-native" upload it as authored.
    harness: str | None = None


@dataclass(frozen=True)
class ChildSessionRequest:
    """A loop-started child of a task's root session (reviewer, integrator).

    :param parent_session_id: The task's root session; the child shows in its tree.
    :param workspace: The task worktree the child runs in.
    """

    parent_session_id: str
    title: str
    prompt: str
    workspace: str
    agent_dir: Path
    acting_user: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    # Per-session effort (e.g. "low" for a small review diff); None = the agent default.
    reasoning_effort: str | None = None
    # The mission's omnigent project (same folder as the parent).
    project_id: str | None = None
    # See RootSessionRequest.harness.
    harness: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    """The slice of an omnigent session snapshot the board cares about.

    :param status: ``"idle"``, ``"running"``, ``"waiting"`` or ``"failed"``.
    :param awaiting_human: A pending approval / input prompt is open.
    :param cost_usd: Subtree cost of the session, when reported.
    :param error: Last task error message, when the session failed.
    :param agent_replied: The latest transcript item is the agent's, not the
        user's prompt; only read while idle, to tell "finished" from "not started".
    :param pending_ask: What the first open approval prompt asks: its
        ``policy`` name and a redacted, ~120-char ``preview`` of the command /
        file (see :func:`ask_summary`).
    """

    status: str
    awaiting_human: bool = False
    cost_usd: float | None = None
    error: str | None = None
    agent_replied: bool = False
    # ``{"policy": ..., "preview": ...}`` of the first open approval prompt.
    pending_ask: dict[str, str] | None = None
    # Its ``elicitation_id``: one intervention per prompt, however often it is polled.
    pending_ask_id: str | None = None
    # ``last_task_error.code`` (e.g. ``"runner_error"``), when the session failed.
    error_code: str | None = None


class SessionService(Protocol):
    """Operations the orchestrator needs on omnigent sessions."""

    async def create_root_session(self, request: RootSessionRequest) -> str:
        """Create the worktree + root session, send the prompt, return the id."""
        ...

    async def cancel(self, session_id: str, *, acting_user: str | None) -> None:
        """Interrupt the session's running work (the process stays, e.g. for a human)."""
        ...

    async def stop(self, session_id: str, *, acting_user: str | None) -> None:
        """Terminate the session's agent process and runner; the transcript stays."""
        ...

    async def snapshot(
        self, session_id: str, *, acting_user: str | None
    ) -> SessionSnapshot | None:
        """Current state of the session, or ``None`` when it no longer exists."""
        ...

    async def send_message(self, session_id: str, text: str, *, acting_user: str | None) -> None:
        """Send ``text`` as a new user turn (e.g. CI logs, review feedback)."""
        ...

    async def last_agent_text(self, session_id: str, *, acting_user: str | None) -> str | None:
        """Text of the latest assistant message, or ``None`` when there is none."""
        ...

    async def create_child_session(self, request: ChildSessionRequest) -> str:
        """Create a child session under a root session, send the prompt, return the id."""
        ...


# Placeholder lines of the bundles' owned-paths policy (agents/_shared/policies/
# owned_paths.yaml): the task contract is written over them at start.
_OWNED_PATHS_SLOT = re.compile(
    r"^(?P<indent>[ \t]*)owned_paths:[^\n]*# @task\.owned_paths[ \t]*$", re.M
)
_ROOT_SLOT = re.compile(r"^(?P<indent>[ \t]*)root:[^\n]*# @task\.root[ \t]*$", re.M)
_OTHER_TASKS_SLOT = re.compile(
    r"^(?P<indent>[ \t]*)other_tasks:[^\n]*# @task\.other_tasks[ \t]*$", re.M
)


def inject_task_contract(
    config_text: str,
    *,
    owned_paths: Sequence[str],
    root: str,
    other_tasks: Sequence[dict[str, Any]] = (),
) -> str:
    """Fill the owned-paths policy slots of a bundle ``config.yaml``.

    The values are written as JSON, which YAML reads as flow scalars. A config
    without the slots (a role with no owned-paths policy) is returned as is.

    :param other_tasks: The mission's other active tasks (``{"title",
        "owned_paths"}``), a start-time snapshot: writes to their files are
        refused with a hint (the ``# @task.other_tasks`` slot; a bundle
        without it keeps asking for them).
    """
    if not owned_paths or _OWNED_PATHS_SLOT.search(config_text) is None:
        return config_text
    text = _OWNED_PATHS_SLOT.sub(
        lambda m: f"{m['indent']}owned_paths: {json.dumps(list(owned_paths))}", config_text
    )
    others = [
        {"title": str(o.get("title") or ""), "owned_paths": [str(p) for p in o["owned_paths"]]}
        for o in other_tasks
        if o.get("owned_paths")
    ]
    text = _OTHER_TASKS_SLOT.sub(lambda m: f"{m['indent']}other_tasks: {json.dumps(others)}", text)
    return _ROOT_SLOT.sub(lambda m: f"{m['indent']}root: {json.dumps(root)}", text)


def bundle_agent_dir(
    agent_dir: Path,
    *,
    owned_paths: Sequence[str] = (),
    workspace: str | None = None,
    other_tasks: Sequence[dict[str, Any]] = (),
    harness: str | None = None,
) -> bytes:
    """Pack an agent bundle directory as the ``tar.gz`` omnigent accepts.

    :param owned_paths: The task's owned globs; with *workspace* they are
        injected into the bundle's owned-paths guardrail (see
        :func:`inject_task_contract`).
    :param workspace: Absolute path of the task worktree.
    :param other_tasks: The mission's other active tasks (see
        :func:`inject_task_contract`).
    :param harness: ``"claude-sdk"`` renders a claude-native worker bundle for
        the headless harness (:func:`omnigent.shipcrew.harness.apply_worker_harness`).
    """
    from omnigent.spec import materialize_bundle

    if not (agent_dir / "config.yaml").is_file():
        raise SessionServiceError(f"agent bundle {agent_dir} has no config.yaml")
    buf = io.BytesIO()
    # materialize_bundle dereferences symlinks (the orchestrator's agents/<role>).
    with tempfile.TemporaryDirectory() as tmp:
        root = materialize_bundle(agent_dir, Path(tmp) / "bundle")
        config = root / "config.yaml"
        text = original = config.read_text(encoding="utf-8")
        if owned_paths and workspace:
            text = inject_task_contract(
                text, owned_paths=owned_paths, root=workspace, other_tasks=other_tasks
            )
        text = apply_worker_harness(text, harness)
        if text != original:
            config.write_text(text, encoding="utf-8")
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root)
                if path.is_file() and not _BUNDLE_SKIP_DIRS.intersection(rel.parts):
                    tar.add(str(path), arcname=rel.as_posix())
    return buf.getvalue()


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("detail"):
            return str(payload["detail"])[:500]
    return str(payload)[:500]


class OmnigentSessionService:
    """Real :class:`SessionService` backed by the running omnigent app.

    :param app: The omnigent FastAPI app (requests are served in-process).
    :param auth_provider: The app's auth provider, used to act as the task
        owner on background calls. ``None`` when auth is disabled.
    :param host_id: Pinned host; ``None`` picks the owner's online host.
    """

    def __init__(self, app: Any, auth_provider: Any | None, *, host_id: str | None = None):
        self._app = app
        self._auth_provider = auth_provider
        self._host_id = host_id
        # One lock per repository around `git worktree add`: parallel starts
        # on the same repo race on its .git (index/config/ref locks).
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        # First prompt of each session this service created, until the agent
        # answers: re-sent once if the runner rejects that first turn.
        # ponytail: in-memory, a restart loses the retry (the failure surfaces).
        self._first_prompts: dict[str, _FirstPrompt] = {}

    def _worktree_lock(self, repo_path: str) -> asyncio.Lock:
        key = posixpath.normpath(repo_path)
        return self._worktree_locks.setdefault(key, asyncio.Lock())

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url=_INTERNAL_BASE_URL,
            timeout=httpx.Timeout(180.0),
        )

    def _auth_headers(self, user_id: str | None) -> dict[str, str]:
        from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
        from omnigent.server.auth import (
            RESERVED_USER_LOCAL,
            resolve_auth_header,
            resolve_auth_header_strip_prefix,
        )

        # First-party Origin satisfies the CSRF guard on multipart session create.
        headers = {"Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
        provider = self._auth_provider
        if provider is None or user_id is None or user_id == RESERVED_USER_LOCAL:
            return headers
        token = provider.mint_runner_token(user_id, _TOKEN_TTL_S)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers[resolve_auth_header()] = resolve_auth_header_strip_prefix() + user_id
        return headers

    async def _resolve_host(self, acting_user: str | None) -> tuple[str, Any]:
        from omnigent.server.auth import RESERVED_USER_LOCAL

        registry = getattr(self._app.state, "host_registry", None)
        if registry is None:
            raise SessionServiceError("no host registry on this server")
        # The worktree call below goes straight to the host tunnel, past the
        # owner check of /v1/hosts: with real users, only the acting user's
        # own hosts are candidates (single-user mode may use any online host).
        multi_user = acting_user is not None and acting_user != RESERVED_USER_LOCAL
        host_store = getattr(self._app.state, "host_store", None)
        owned: list[str] = []
        if host_store is not None:
            hosts = await asyncio.to_thread(
                host_store.list_hosts, acting_user or RESERVED_USER_LOCAL
            )
            owned = [str(h.host_id) for h in hosts if getattr(h, "sandbox_provider", None) is None]
        candidates: list[str] = []
        if self._host_id is not None:
            candidates.append(self._host_id)
        else:
            candidates.extend(owned)
            if not multi_user:
                candidates.extend(registry.online_host_ids())
        if multi_user:
            candidates = [h for h in candidates if h in owned]
        for host_id in candidates:
            conn = registry.get(host_id)
            if conn is not None:
                return host_id, conn
        raise SessionServiceError(
            "no online host: run `omnigent host` on the machine that holds the repo"
        )

    async def _task_worktree(self, conn: Any, request: RootSessionRequest) -> str:
        """Workspace of the task's worktree, reusing one left by an earlier start.

        Idempotent across retries: an existing worktree on the branch is reused,
        an existing branch without a worktree is checked out afresh.
        """
        from omnigent.server.routes._host_worktree import (
            WorktreeProxyError,
            list_worktrees_on_host,
        )

        registry = self._app.state.host_registry
        async with self._worktree_lock(request.repo_path):
            try:
                listed = await list_worktrees_on_host(
                    host_registry=registry, host_conn=conn, repo_path=request.repo_path
                )
            except WorktreeProxyError as exc:
                raise SessionServiceError(exc.message) from exc
            for entry in listed:
                if entry.get("branch") == request.branch and entry.get("path"):
                    return str(entry["path"])
            return await self._add_worktree(conn, request)

    async def _add_worktree(self, conn: Any, request: RootSessionRequest) -> str:
        from omnigent.server.routes._host_worktree import (
            WorktreeProxyError,
            create_worktree_on_host,
        )

        registry = self._app.state.host_registry
        try:
            created = await create_worktree_on_host(
                host_registry=registry,
                host_conn=conn,
                repo_path=request.repo_path,
                branch_name=request.branch,
                base_branch=request.base_branch,
            )
        except WorktreeProxyError as first:
            try:
                created = await create_worktree_on_host(
                    host_registry=registry,
                    host_conn=conn,
                    repo_path=request.repo_path,
                    branch_name=request.branch,
                    base_branch=None,
                    existing_branch=True,
                )
            except WorktreeProxyError:
                raise SessionServiceError(first.message) from first
        return created.workspace or created.worktree_path

    async def create_root_session(self, request: RootSessionRequest) -> str:
        host_id, conn = await self._resolve_host(request.acting_user)
        workspace = request.workspace or await self._task_worktree(conn, request)
        if request.workspace is None:
            # Keep tool-written AGENTS.md / CLAUDE.md out of commits, and skip
            # the agent's dependency install when the main checkout's
            # node_modules matches the worktree's lockfile (best effort).
            await asyncio.to_thread(prepare_worktree, request.repo_path, workspace)
        bundle = await asyncio.to_thread(
            bundle_agent_dir,
            request.agent_dir,
            owned_paths=request.owned_paths,
            workspace=workspace,
            other_tasks=request.other_tasks,
            harness=request.harness,
        )
        await asyncio.to_thread(_pretrust_claude_workspace, workspace)
        metadata = {
            "title": request.title[:200],
            "host_id": host_id,
            "workspace": workspace,
            "labels": request.labels,
        }
        headers = self._auth_headers(request.acting_user)
        async with self._client() as client:
            created = await self._post_session(
                client, metadata, bundle, headers, request.project_id
            )
            if created.status_code >= 400:
                raise SessionServiceError(f"session create failed: {_error_detail(created)}")
            session_id = str(created.json()["session_id"])
            try:
                await self._wait_runner_online(client, session_id, headers)
                await self._post_message(client, session_id, request.prompt, headers)
                self._first_prompts[session_id] = _FirstPrompt(request.prompt)
            except BaseException:
                # The caller never learns this id: end the session, or its
                # runner lingers unowned by any card.
                with contextlib.suppress(Exception):
                    await self.stop(session_id, acting_user=request.acting_user)
                raise
        return session_id

    async def _post_session(
        self,
        client: httpx.AsyncClient,
        metadata: dict[str, Any],
        bundle: bytes,
        headers: dict[str, str],
        project_id: str | None,
    ) -> httpx.Response:
        """``POST /v1/sessions`` (multipart), filed into ``project_id`` when set.

        A project deleted in the meantime (404) must not block the session:
        it is created unfiled instead.
        """

        async def post(meta: dict[str, Any]) -> httpx.Response:
            return await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(meta)},
                files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
                headers=headers,
            )

        if project_id is None:
            return await post(metadata)
        created = await post({**metadata, "project_id": project_id})
        if created.status_code == 404 and "project" in _error_detail(created).lower():
            _logger.warning("shipcrew: project %s is gone; session created unfiled", project_id)
            return await post(metadata)
        return created

    # ── Projects (omnigent/shipcrew/projects.py) ──

    async def list_projects(self, *, acting_user: str | None) -> list[ProjectRef]:
        async with self._client() as client:
            response = await client.get("/v1/projects", headers=self._auth_headers(acting_user))
        if response.status_code >= 400:
            raise SessionServiceError(f"project list failed: {_error_detail(response)}")
        return [
            ProjectRef(id=str(p["id"]), name=str(p.get("name") or ""))
            for p in response.json().get("data") or []
            if isinstance(p, dict) and p.get("id")
        ]

    async def create_project(self, name: str, *, acting_user: str | None) -> ProjectRef:
        async with self._client() as client:
            response = await client.post(
                "/v1/projects", json={"name": name}, headers=self._auth_headers(acting_user)
            )
        if response.status_code == 409:
            raise ProjectNameTaken(_error_detail(response))
        if response.status_code >= 400:
            raise SessionServiceError(f"project create failed: {_error_detail(response)}")
        body = response.json()
        return ProjectRef(id=str(body["id"]), name=str(body.get("name") or name))

    async def rename_project(
        self, project_id: str, name: str, *, acting_user: str | None
    ) -> ProjectRef | None:
        async with self._client() as client:
            response = await client.patch(
                f"/v1/projects/{project_id}",
                json={"name": name},
                headers=self._auth_headers(acting_user),
            )
        if response.status_code == 404:
            return None
        if response.status_code == 409:
            raise ProjectNameTaken(_error_detail(response))
        if response.status_code >= 400:
            raise SessionServiceError(f"project rename failed: {_error_detail(response)}")
        body = response.json()
        return ProjectRef(id=str(body["id"]), name=str(body.get("name") or name))

    async def _post_message(
        self, client: httpx.AsyncClient, session_id: str, text: str, headers: dict[str, str]
    ) -> None:
        sent = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
            headers=headers,
        )
        if sent.status_code >= 400:
            raise SessionServiceError(f"prompt dispatch failed: {_error_detail(sent)}")

    async def send_message(self, session_id: str, text: str, *, acting_user: str | None) -> None:
        self._first_prompts.pop(session_id, None)  # a later turn: not a first-turn failure
        async with self._client() as client:
            await self._post_message(client, session_id, text, self._auth_headers(acting_user))

    async def last_agent_text(self, session_id: str, *, acting_user: str | None) -> str | None:
        async with self._client() as client:
            response = await client.get(
                f"/v1/sessions/{session_id}/items",
                params={"limit": 50, "order": "desc"},
                headers=self._auth_headers(acting_user),
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SessionServiceError(f"session items read failed: {_error_detail(response)}")
        for item in response.json().get("data") or []:
            text = assistant_text(item)
            if text is not None:
                return text
        return None

    async def create_child_session(self, request: ChildSessionRequest) -> str:
        bundle = await asyncio.to_thread(
            bundle_agent_dir, request.agent_dir, harness=request.harness
        )
        await asyncio.to_thread(_pretrust_claude_workspace, request.workspace)
        headers = self._auth_headers(request.acting_user)
        metadata: dict[str, Any] = {
            "title": request.title[:200],
            "workspace": request.workspace,
            "parent_session_id": request.parent_session_id,
            "labels": request.labels,
        }
        if request.reasoning_effort is not None:
            metadata["reasoning_effort"] = request.reasoning_effort
        async with self._client() as client:
            parent = await client.get(
                f"/v1/sessions/{request.parent_session_id}",
                params={"include_items": "false", "include_liveness": "false"},
                headers=headers,
            )
            if parent.status_code >= 400:
                raise SessionServiceError(f"parent session read failed: {_error_detail(parent)}")
            # Always give the child its own host runner. Co-locating it on the
            # parent's runner raced with that runner stopping (the developer
            # goes idle, or is stopped, right before the reviewer starts), and
            # stopping the co-located child sent SIGTERM to the parent's
            # runner (the stop targets the runner bound to the child's row).
            # An explicit host_id makes the server skip the runner
            # inheritance (routes_core, shipcrew fork), so stopping the child
            # tears down only its own runner; parent_session_id still puts it
            # in the parent's tree.
            metadata["host_id"], _conn = await self._resolve_host(request.acting_user)
            created = await self._post_session(
                client, metadata, bundle, headers, request.project_id
            )
            if created.status_code >= 400:
                raise SessionServiceError(f"child session create failed: {_error_detail(created)}")
            session_id = str(created.json()["session_id"])
            try:
                await self._wait_runner_online(client, session_id, headers)
                await self._post_message(client, session_id, request.prompt, headers)
                self._first_prompts[session_id] = _FirstPrompt(request.prompt)
            except BaseException:
                with contextlib.suppress(Exception):
                    await self.stop(session_id, acting_user=request.acting_user)
                raise
        return session_id

    async def _wait_runner_online(
        self, client: httpx.AsyncClient, session_id: str, headers: dict[str, str]
    ) -> None:
        """Wait for the host-launched runner before the first prompt.

        A prompt posted earlier costs the server's fixed connect grace plus a
        runner relaunch. Best effort: on timeout the prompt is sent anyway.
        """
        deadline = asyncio.get_running_loop().time() + _RUNNER_ONLINE_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            snap = await client.get(f"/v1/sessions/{session_id}", headers=headers)
            runner_id = snap.json().get("runner_id") if snap.status_code < 400 else None
            if runner_id:
                status = await client.get(f"/v1/runners/{runner_id}/status", headers=headers)
                if status.status_code < 400 and status.json().get("online"):
                    return
            await asyncio.sleep(_RUNNER_POLL_S)
        _logger.warning("shipcrew: runner of session %s not online; prompting anyway", session_id)

    async def cancel(self, session_id: str, *, acting_user: str | None) -> None:
        async with self._client() as client:
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "interrupt"},
                headers=self._auth_headers(acting_user),
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise SessionServiceError(f"interrupt failed: {_error_detail(response)}")

    async def stop(self, session_id: str, *, acting_user: str | None) -> None:
        # stop_session kills the harness process (claude's tmux pane) and the
        # host runner launched for it; an interrupt alone leaves both alive.
        self._first_prompts.pop(session_id, None)
        async with self._client() as client:
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "stop_session", "data": {}},
                headers=self._auth_headers(acting_user),
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise SessionServiceError(f"stop failed: {_error_detail(response)}")

    async def snapshot(
        self, session_id: str, *, acting_user: str | None
    ) -> SessionSnapshot | None:
        async with self._client() as client:
            response = await client.get(
                f"/v1/sessions/{session_id}",
                params={"include_items": "false", "include_liveness": "false"},
                headers=self._auth_headers(acting_user),
            )
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise SessionServiceError(f"session read failed: {_error_detail(response)}")
            snap = snapshot_from_payload(response.json())
            if snap.status == "failed" and session_id in self._first_prompts:
                headers = self._auth_headers(acting_user)
                return await self._retry_first_turn(client, session_id, snap, headers)
            if snap.status != "idle":
                return snap
            # An idle root may still be waiting on background agents / shells.
            children = await client.get(
                f"/v1/sessions/{session_id}/child_sessions",
                headers=self._auth_headers(acting_user),
            )
            if children.status_code < 400 and any(
                isinstance(c, dict) and c.get("busy") for c in children.json().get("data") or []
            ):
                return dataclasses.replace(snap, status="running")
            items = await client.get(
                f"/v1/sessions/{session_id}/items",
                params={"limit": 1, "order": "desc"},
                headers=self._auth_headers(acting_user),
            )
        if items.status_code >= 400:
            raise SessionServiceError(f"session items read failed: {_error_detail(items)}")
        latest = (items.json().get("data") or [None])[0]
        replied = _is_agent_item(latest)
        if replied:
            self._first_prompts.pop(session_id, None)
        return dataclasses.replace(snap, agent_replied=replied)

    async def _retry_first_turn(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        snap: SessionSnapshot,
        headers: dict[str, str],
    ) -> SessionSnapshot:
        """Re-send a first prompt the runner rejected before any agent output, once.

        The session then reads as ``running`` (a plan stays ``running``, a card
        is not failed); a failure after the retry is reported as is.
        """
        first = self._first_prompts[session_id]
        now = asyncio.get_running_loop().time()
        if first.retried_at is not None:
            if now - first.retried_at < _FIRST_TURN_RETRY_GRACE_S and not await self._has_output(
                client, session_id, headers
            ):
                return dataclasses.replace(snap, status="running", error=None, error_code=None)
            self._first_prompts.pop(session_id, None)
            return snap
        if snap.error_code != _FIRST_TURN_RETRY_CODE or await self._has_output(
            client, session_id, headers
        ):
            self._first_prompts.pop(session_id, None)
            return snap
        _logger.warning(
            "shipcrew: first turn of session %s failed (%s); re-sending it once",
            session_id,
            snap.error,
        )
        await asyncio.sleep(_FIRST_TURN_RETRY_BACKOFF_S)
        try:
            await self._post_message(client, session_id, first.text, headers)
        except SessionServiceError as exc:
            _logger.warning("shipcrew: first-turn retry of %s failed: %s", session_id, exc)
            self._first_prompts.pop(session_id, None)
            return snap
        first.retried_at = asyncio.get_running_loop().time()
        return dataclasses.replace(snap, status="running", error=None, error_code=None)

    async def _has_output(
        self, client: httpx.AsyncClient, session_id: str, headers: dict[str, str]
    ) -> bool:
        """Whether the agent produced any transcript item yet (unknown counts as yes)."""
        items = await client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 50, "order": "desc"},
            headers=headers,
        )
        if items.status_code >= 400:
            return True
        return any(_is_agent_item(item) for item in items.json().get("data") or [])


@dataclass
class _FirstPrompt:
    """A session's first prompt, kept for one automatic re-send."""

    text: str
    retried_at: float | None = None


def _pretrust_claude_workspace(workspace: str) -> None:
    """Seed Claude's folder trust for a local worktree before its session starts.

    Concurrent ``claude`` launches race on ``~/.claude.json`` and can drop each
    other's trust entry; pre-seeding makes the launch-time write a no-op. Only
    applies when the host shares this machine's filesystem.
    """
    path = Path(workspace)
    if not path.is_dir():
        return
    from omnigent.harnesses.claude_native.bridge import ensure_claude_workspace_trusted

    with _TRUST_LOCK:
        try:
            ensure_claude_workspace_trusted(path)
        except (OSError, ValueError) as exc:
            _logger.warning("shipcrew: could not pre-trust %s: %s", workspace, exc)


_AGENT_ITEM_TYPES = {"function_call", "function_call_output", "reasoning"}


def _is_agent_item(item: Any) -> bool:
    """Whether a transcript item was produced by the agent's turn.

    Bookkeeping items (e.g. ``resource_event``) are written before the prompt
    item lands, so they must not count as a reply.
    """
    if not isinstance(item, dict):
        return False
    if item.get("type") == "message":
        return item.get("role") == "assistant"
    return item.get("type") in _AGENT_ITEM_TYPES


def assistant_text(item: Any) -> str | None:
    """Concatenated text of an assistant ``message`` item, else ``None``."""
    if not isinstance(item, dict) or item.get("type") != "message":
        return None
    if item.get("role") != "assistant":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = [
        str(part.get("text") or "")
        for part in content or []
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    ]
    return "".join(parts) if parts else None


_PREVIEW_MAX = 120
# Values that must never reach the report: KEY=value / --token value / known token shapes.
_SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|AUTH|CREDENTIAL|COOKIE|"
    r"DATABASE_URL|DSN)[A-Z0-9_]*)(\s*[=:]\s*|\s+)(\"[^\"]*\"|'[^']*'|\S+)"
)
_SECRET_SHAPES = re.compile(
    r"(?i)(bearer\s+)\S+|\b(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"xox[abp]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]+)"
    r"|(://[^/\s:@]+:)[^@\s]+@"
)


def redact_preview(text: str, limit: int = _PREVIEW_MAX) -> str:
    """One line, secrets masked (``KEY=***``, tokens, URL passwords), cut to *limit*."""
    flat = " ".join(str(text).split())
    flat = _SECRET_SHAPES.sub(
        lambda m: (m.group(1) or "") + "***" if m.group(1) or m.group(2) else f"{m.group(3)}***@",
        flat,
    )
    flat = _SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", flat)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def ask_summary(event: object) -> dict[str, str] | None:
    """``{"policy", "preview"}`` of a ``response.elicitation_request`` event dict."""
    if not isinstance(event, dict):
        return None
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    policy = params.get("policy_name") or event.get("policy_name") or ""
    preview = (
        params.get("content_preview")
        or event.get("content_preview")
        or params.get("message")
        or event.get("message")
        or ""
    )
    if not policy and not preview:
        return None
    return {"policy": str(policy)[:80], "preview": redact_preview(str(preview))}


def snapshot_from_payload(payload: dict[str, Any]) -> SessionSnapshot:
    """Reduce a ``GET /v1/sessions/{id}`` body to a :class:`SessionSnapshot`."""
    error = payload.get("last_task_error")
    cost = payload.get("total_cost_usd")
    status = str(payload.get("status") or "idle")
    pending = payload.get("pending_elicitations") or []
    pending = pending if isinstance(pending, list) else []
    # Claude-native background shells / agents keep the task in flight.
    if status == "idle" and (payload.get("background_task_count") or 0) > 0:
        status = "running"
    first = next((e for e in pending if ask_summary(e)), None)
    ask_id = first.get("elicitation_id") if isinstance(first, dict) else None
    return SessionSnapshot(
        status=status,
        awaiting_human=bool(pending),
        pending_ask=ask_summary(first) if first is not None else None,
        pending_ask_id=str(ask_id) if ask_id else None,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        error=str(error.get("message") or error) if isinstance(error, dict) else None,
        error_code=str(error["code"]) if isinstance(error, dict) and error.get("code") else None,
    )
