"""The shipcrew board API is mounted on the real omnigent app and shares its DB."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import inspect

from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


async def test_shipcrew_routes_on_real_app(app: FastAPI, db_uri: str) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/shipcrew/missions", json={"title": "Ship", "repo_path": "/repo"}
        )
        assert created.status_code == 200, created.text
        mission = created.json()
        task = await client.post(
            f"/v1/shipcrew/missions/{mission['id']}/tasks", json={"title": "T"}
        )
        assert task.status_code == 200, task.text
        listed = await client.get(f"/v1/shipcrew/missions/{mission['id']}/tasks")
        assert [t["title"] for t in listed.json()["tasks"]] == ["T"]
        missing = await client.get("/v1/shipcrew/missions/nope/tasks")
        assert missing.status_code == 404
    tables = set(inspect(get_or_create_engine(db_uri)).get_table_names())
    assert {"shipcrew_missions", "shipcrew_tasks"} <= tables
    assert hasattr(app.state, "shipcrew_scheduler")


@pytest.fixture
def app_with_projects(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The conftest ``app`` plus a project store (``/v1/projects`` mounted)."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        project_store=SqlAlchemyProjectStore(db_uri),
    )


async def test_mission_gets_an_omnigent_project(app_with_projects: FastAPI) -> None:
    """The real in-process ``/v1/projects`` call: one folder per mission, renamed with it."""
    transport = httpx.ASGITransport(app=app_with_projects)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/shipcrew/missions", json={"title": "Project mission", "repo_path": "/repo"}
        )
        assert created.status_code == 200, created.text
        mission = created.json()
        assert mission["project_id"]
        projects = (await client.get("/v1/projects")).json()["data"]
        assert {"id": mission["project_id"], "name": "Project mission"}.items() <= next(
            p for p in projects if p["id"] == mission["project_id"]
        ).items()
        renamed = await client.patch(
            f"/v1/shipcrew/missions/{mission['id']}", json={"title": "Renamed mission"}
        )
        assert renamed.status_code == 200, renamed.text
        project = (await client.get(f"/v1/projects/{mission['project_id']}")).json()
        assert project["name"] == "Renamed mission"
        links = (await client.get("/v1/shipcrew/project-links")).json()["links"]
        assert {"project_id": mission["project_id"], "mission_id": mission["id"]}.items() <= next(
            link for link in links if link["mission_id"] == mission["id"]
        ).items()
