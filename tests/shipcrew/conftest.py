"""Fixtures for the shipcrew orchestrator tests: a bare app, a fake session seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.shipcrew.router import mount_shipcrew
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import (
    RootSessionRequest,
    SessionServiceError,
    SessionSnapshot,
)
from omnigent.shipcrew.settings import ShipcrewSettings

USER_HEADER = "X-Test-User"


@dataclass
class FakeSessions:
    """Records calls; snapshots and failures are set per test."""

    created: list[RootSessionRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    snapshots: dict[str, SessionSnapshot | None] = field(default_factory=dict)
    fail_create: str | None = None

    async def create_root_session(self, request: RootSessionRequest) -> str:
        if self.fail_create is not None:
            raise SessionServiceError(self.fail_create)
        self.created.append(request)
        return f"sess{len(self.created)}"

    async def cancel(self, session_id: str, *, acting_user: str | None) -> None:
        self.cancelled.append(session_id)

    async def snapshot(
        self, session_id: str, *, acting_user: str | None
    ) -> SessionSnapshot | None:
        return self.snapshots.get(session_id, SessionSnapshot(status="running"))


class HeaderAuth:
    """Minimal auth provider: identity from ``X-Test-User``, else unauthenticated."""

    def get_user_id(self, request: Any) -> str | None:
        return request.headers.get(USER_HEADER) or None

    def mint_runner_token(self, user_id: str, ttl_seconds: int) -> str | None:
        return None


@dataclass
class _Store:
    storage_location: str


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    for role in ("developer", "reviewer"):
        (root / role).mkdir(parents=True)
        (root / role / "config.yaml").write_text(f"name: {role}\n")
    return root


@pytest.fixture
def settings(tmp_path: Path, agents_dir: Path) -> ShipcrewSettings:
    return ShipcrewSettings(
        agents_dir=agents_dir,
        max_parallel=2,
        max_usd=10.0,
        scheduler_enabled=False,
        db_url=f"sqlite:///{tmp_path / 'shipcrew.db'}",
    )


@pytest.fixture
def sessions() -> FakeSessions:
    return FakeSessions()


@pytest.fixture
def app_and_service(
    settings: ShipcrewSettings, sessions: FakeSessions
) -> tuple[FastAPI, ShipcrewService]:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": str(exc.code), "message": exc.message}},
        )

    get_service = mount_shipcrew(
        app,
        conversation_store=_Store(storage_location="sqlite://"),
        auth_provider=HeaderAuth(),
        settings=settings,
        session_service=sessions,
    )
    return app, get_service()


@pytest.fixture
def service(app_and_service: tuple[FastAPI, ShipcrewService]) -> ShipcrewService:
    return app_and_service[1]


@pytest_asyncio.fixture
async def client(
    app_and_service: tuple[FastAPI, ShipcrewService],
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_and_service[0])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={USER_HEADER: "alice@example.com"}
    ) as c:
        yield c
