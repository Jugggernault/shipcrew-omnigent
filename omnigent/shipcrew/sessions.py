"""How shipcrew drives omnigent sessions.

:class:`SessionService` is the seam the orchestrator talks to; tests replace
it. :class:`OmnigentSessionService` is the real implementation: it creates the
task's git worktree through the host tunnel, then goes through omnigent's own
public ``/v1/sessions`` API in-process, so all of omnigent's validation, host
launch and permission logic applies unchanged.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

_logger = logging.getLogger(__name__)

# Minted in-process bearer tokens only need to outlive one request.
_TOKEN_TTL_S = 300
_INTERNAL_BASE_URL = "http://127.0.0.1"
_BUNDLE_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


class SessionServiceError(RuntimeError):
    """A session operation failed; the message is shown as ``blocked_reason``."""


@dataclass(frozen=True)
class RootSessionRequest:
    """Everything needed to start a task's root session.

    :param branch: Git branch for the task worktree, e.g. ``"task/<id>"``.
    :param existing_branch: Check out ``branch`` instead of creating it (a
        restart after an earlier attempt already created the branch).
    :param acting_user: Identity the session is created for (its owner).
    """

    task_id: str
    title: str
    prompt: str
    repo_path: str
    branch: str
    agent_dir: Path
    acting_user: str | None = None
    existing_branch: bool = False
    base_branch: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSnapshot:
    """The slice of an omnigent session snapshot the board cares about.

    :param status: ``"idle"``, ``"running"``, ``"waiting"`` or ``"failed"``.
    :param awaiting_human: A pending approval / input prompt is open.
    :param cost_usd: Subtree cost of the session, when reported.
    :param error: Last task error message, when the session failed.
    """

    status: str
    awaiting_human: bool = False
    cost_usd: float | None = None
    error: str | None = None


class SessionService(Protocol):
    """Operations the orchestrator needs on omnigent sessions."""

    async def create_root_session(self, request: RootSessionRequest) -> str:
        """Create the worktree + root session, send the prompt, return the id."""
        ...

    async def cancel(self, session_id: str, *, acting_user: str | None) -> None:
        """Interrupt the session's running work."""
        ...

    async def snapshot(
        self, session_id: str, *, acting_user: str | None
    ) -> SessionSnapshot | None:
        """Current state of the session, or ``None`` when it no longer exists."""
        ...


def bundle_agent_dir(agent_dir: Path) -> bytes:
    """Pack an agent bundle directory as the ``tar.gz`` omnigent accepts."""
    if not (agent_dir / "config.yaml").is_file():
        raise SessionServiceError(f"agent bundle {agent_dir} has no config.yaml")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(agent_dir.rglob("*")):
            rel = path.relative_to(agent_dir)
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
        from omnigent.server.auth import (
            RESERVED_USER_LOCAL,
            resolve_auth_header,
            resolve_auth_header_strip_prefix,
        )

        provider = self._auth_provider
        if provider is None or user_id is None or user_id == RESERVED_USER_LOCAL:
            return {}
        token = provider.mint_runner_token(user_id, _TOKEN_TTL_S)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {resolve_auth_header(): resolve_auth_header_strip_prefix() + user_id}

    async def _resolve_host(self, acting_user: str | None) -> tuple[str, Any]:
        from omnigent.server.auth import RESERVED_USER_LOCAL

        registry = getattr(self._app.state, "host_registry", None)
        if registry is None:
            raise SessionServiceError("no host registry on this server")
        candidates: list[str] = []
        if self._host_id is not None:
            candidates.append(self._host_id)
        else:
            host_store = getattr(self._app.state, "host_store", None)
            if host_store is not None:
                hosts = await asyncio.to_thread(
                    host_store.list_hosts, acting_user or RESERVED_USER_LOCAL
                )
                candidates.extend(
                    str(h.host_id) for h in hosts if getattr(h, "sandbox_provider", None) is None
                )
            candidates.extend(registry.online_host_ids())
        for host_id in candidates:
            conn = registry.get(host_id)
            if conn is not None:
                return host_id, conn
        raise SessionServiceError(
            "no online host: run `omnigent host` on the machine that holds the repo"
        )

    async def create_root_session(self, request: RootSessionRequest) -> str:
        from omnigent.server.routes._host_worktree import (
            WorktreeProxyError,
            create_worktree_on_host,
        )

        bundle = await asyncio.to_thread(bundle_agent_dir, request.agent_dir)
        host_id, conn = await self._resolve_host(request.acting_user)
        try:
            worktree = await create_worktree_on_host(
                host_registry=self._app.state.host_registry,
                host_conn=conn,
                repo_path=request.repo_path,
                branch_name=request.branch,
                base_branch=None if request.existing_branch else request.base_branch,
                existing_branch=request.existing_branch,
            )
        except WorktreeProxyError as exc:
            raise SessionServiceError(exc.message) from exc
        metadata = {
            "title": request.title[:200],
            "host_id": host_id,
            "workspace": worktree.workspace or worktree.worktree_path,
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
            sent = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": request.prompt}],
                    },
                },
                headers=headers,
            )
            if sent.status_code >= 400:
                raise SessionServiceError(f"prompt dispatch failed: {_error_detail(sent)}")
        return session_id

    async def cancel(self, session_id: str, *, acting_user: str | None) -> None:
        async with self._client() as client:
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "interrupt"},
                headers=self._auth_headers(acting_user),
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise SessionServiceError(f"interrupt failed: {_error_detail(response)}")

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
        return snapshot_from_payload(response.json())


def snapshot_from_payload(payload: dict[str, Any]) -> SessionSnapshot:
    """Reduce a ``GET /v1/sessions/{id}`` body to a :class:`SessionSnapshot`."""
    error = payload.get("last_task_error")
    cost = payload.get("total_cost_usd")
    return SessionSnapshot(
        status=str(payload.get("status") or "idle"),
        awaiting_human=bool(payload.get("pending_elicitations")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        error=str(error.get("message") or error) if isinstance(error, dict) else None,
    )
