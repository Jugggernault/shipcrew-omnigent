"""shipcrew fork: a host-bound child session gets its own runner.

The PR loop starts reviewer / integrator children with ``parent_session_id``
(board tree) AND an explicit ``host_id``. Co-located on the parent's runner,
the child row carried the parent's ``runner_id``, so the loop's
``stop_session`` on the child sent SIGTERM to the developer's runner. With an
explicit ``host_id`` the bundled create must not inherit the runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from tests.server.helpers import build_agent_bundle, register_test_runner

pytestmark = pytest.mark.asyncio

PARENT_RUNNER = "runner_shipcrew_parent"


async def _create(client: httpx.AsyncClient, metadata: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="sc"), "application/gzip")},
    )


@pytest_asyncio.fixture
async def parent_on_runner(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    async def _no_runner_client(session_id: str, runner_router: object) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_runner_client)
    register_test_runner(app, PARENT_RUNNER)


async def _snapshot(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/sessions/{session_id}", params={"include_items": "false"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _parent(client: httpx.AsyncClient) -> str:
    resp = await _create(client, {})
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["session_id"]
    bound = await client.patch(f"/v1/sessions/{parent_id}", json={"runner_id": PARENT_RUNNER})
    assert bound.status_code == 200, bound.text
    return parent_id


async def test_child_without_host_id_still_colocates(
    app: FastAPI, client: httpx.AsyncClient, parent_on_runner: None
) -> None:
    parent_id = await _parent(client)
    resp = await _create(client, {"parent_session_id": parent_id})
    assert resp.status_code == 201, resp.text
    child = await _snapshot(client, resp.json()["session_id"])
    assert child["runner_id"] == PARENT_RUNNER


async def test_host_bound_child_does_not_inherit_the_parent_runner(
    app: FastAPI,
    client: httpx.AsyncClient,
    parent_on_runner: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.server.routes import _session_create_validation

    async def _workspace_ok(**kwargs: Any) -> str:
        return str(tmp_path)

    monkeypatch.setattr(
        _session_create_validation, "validate_uploaded_bundle_host_workspace", _workspace_ok
    )
    parent_id = await _parent(client)
    # (This test app wires no host store, so the host launch itself is skipped.)
    resp = await _create(
        client,
        {"parent_session_id": parent_id, "host_id": "ab" * 16, "workspace": str(tmp_path)},
    )
    assert resp.status_code == 201, resp.text
    child = await _snapshot(client, resp.json()["session_id"])
    # The child never carries the parent's runner, so a stop_session on it
    # cannot reach the developer's runner.
    assert child["runner_id"] != PARENT_RUNNER
