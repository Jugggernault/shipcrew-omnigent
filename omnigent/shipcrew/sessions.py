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
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

_logger = logging.getLogger(__name__)

# Minted in-process bearer tokens only need to outlive one request.
_TOKEN_TTL_S = 300
_INTERNAL_BASE_URL = "http://127.0.0.1"
_BUNDLE_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
_RUNNER_ONLINE_TIMEOUT_S = 60.0
_RUNNER_POLL_S = 0.25
_TRUST_LOCK = threading.Lock()


class SessionServiceError(RuntimeError):
    """A session operation failed; the message is shown as ``blocked_reason``."""


@dataclass(frozen=True)
class RootSessionRequest:
    """Everything needed to start a task's root session.

    :param branch: Git branch for the task worktree, e.g. ``"task/<id>"``.
    :param acting_user: Identity the session is created for (its owner).
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


@dataclass(frozen=True)
class SessionSnapshot:
    """The slice of an omnigent session snapshot the board cares about.

    :param status: ``"idle"``, ``"running"``, ``"waiting"`` or ``"failed"``.
    :param awaiting_human: A pending approval / input prompt is open.
    :param cost_usd: Subtree cost of the session, when reported.
    :param error: Last task error message, when the session failed.
    :param agent_replied: The latest transcript item is the agent's, not the
        user's prompt; only read while idle, to tell "finished" from "not started".
    """

    status: str
    awaiting_human: bool = False
    cost_usd: float | None = None
    error: str | None = None
    agent_replied: bool = False


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


def bundle_agent_dir(agent_dir: Path) -> bytes:
    """Pack an agent bundle directory as the ``tar.gz`` omnigent accepts."""
    from omnigent.spec import materialize_bundle

    if not (agent_dir / "config.yaml").is_file():
        raise SessionServiceError(f"agent bundle {agent_dir} has no config.yaml")
    buf = io.BytesIO()
    # materialize_bundle dereferences symlinks (the orchestrator's agents/<role>).
    with tempfile.TemporaryDirectory() as tmp:
        root = materialize_bundle(agent_dir, Path(tmp) / "bundle")
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
            create_worktree_on_host,
            list_worktrees_on_host,
        )

        registry = self._app.state.host_registry
        try:
            listed = await list_worktrees_on_host(
                host_registry=registry, host_conn=conn, repo_path=request.repo_path
            )
        except WorktreeProxyError as exc:
            raise SessionServiceError(exc.message) from exc
        for entry in listed:
            if entry.get("branch") == request.branch and entry.get("path"):
                return str(entry["path"])
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
        bundle = await asyncio.to_thread(bundle_agent_dir, request.agent_dir)
        host_id, conn = await self._resolve_host(request.acting_user)
        workspace = await self._task_worktree(conn, request)
        await asyncio.to_thread(_pretrust_claude_workspace, workspace)
        metadata = {
            "title": request.title[:200],
            "host_id": host_id,
            "workspace": workspace,
            "labels": request.labels,
        }
        headers = self._auth_headers(request.acting_user)
        async with self._client() as client:
            created = await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
                headers=headers,
            )
            if created.status_code >= 400:
                raise SessionServiceError(f"session create failed: {_error_detail(created)}")
            session_id = str(created.json()["session_id"])
            try:
                await self._wait_runner_online(client, session_id, headers)
                await self._post_message(client, session_id, request.prompt, headers)
            except BaseException:
                # The caller never learns this id: end the session, or its
                # runner lingers unowned by any card.
                with contextlib.suppress(Exception):
                    await self.stop(session_id, acting_user=request.acting_user)
                raise
        return session_id

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
        bundle = await asyncio.to_thread(bundle_agent_dir, request.agent_dir)
        await asyncio.to_thread(_pretrust_claude_workspace, request.workspace)
        headers = self._auth_headers(request.acting_user)
        metadata: dict[str, Any] = {
            "title": request.title[:200],
            "workspace": request.workspace,
            "parent_session_id": request.parent_session_id,
            "labels": request.labels,
        }
        async with self._client() as client:
            parent = await client.get(
                f"/v1/sessions/{request.parent_session_id}",
                params={"include_items": "false", "include_liveness": "false"},
                headers=headers,
            )
            if parent.status_code >= 400:
                raise SessionServiceError(f"parent session read failed: {_error_detail(parent)}")
            if not parent.json().get("runner_id"):
                # No live parent runner to co-locate on: launch on the host.
                # Only then does the child get a host_id, because stopping a
                # host-bound session also tears down its runner, which for a
                # co-located child would be the parent's.
                metadata["host_id"], _conn = await self._resolve_host(request.acting_user)
            created = await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
                headers=headers,
            )
            if created.status_code >= 400:
                raise SessionServiceError(f"child session create failed: {_error_detail(created)}")
            session_id = str(created.json()["session_id"])
            try:
                await self._wait_runner_online(client, session_id, headers)
                await self._post_message(client, session_id, request.prompt, headers)
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
        return dataclasses.replace(snap, agent_replied=_is_agent_item(latest))


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


def snapshot_from_payload(payload: dict[str, Any]) -> SessionSnapshot:
    """Reduce a ``GET /v1/sessions/{id}`` body to a :class:`SessionSnapshot`."""
    error = payload.get("last_task_error")
    cost = payload.get("total_cost_usd")
    status = str(payload.get("status") or "idle")
    # Claude-native background shells / agents keep the task in flight.
    if status == "idle" and (payload.get("background_task_count") or 0) > 0:
        status = "running"
    return SessionSnapshot(
        status=status,
        awaiting_human=bool(payload.get("pending_elicitations")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        error=str(error.get("message") or error) if isinstance(error, dict) else None,
    )
