"""The shipcrew board API is mounted on the real omnigent app and shares its DB."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy import inspect

from omnigent.db.utils import get_or_create_engine


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
