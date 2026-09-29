"""The ``Decisions:`` list: parsing, and where the loop stores it (task, plan)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.shipcrew.decisions import (
    MAX_DECISION_CHARS,
    MAX_DECISIONS,
    merge_decisions,
    parse_decisions,
)
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import SessionSnapshot

from .conftest import FakeSessions

P = "/v1/shipcrew"


class TestParse:
    def test_plain_list_before_the_verdict(self) -> None:
        text = (
            "Done, tests pass.\n\nDecisions:\n- Stored the cart in memory.\n"
            "- Used zod for validation.\nPASS"
        )
        assert parse_decisions(text) == ["Stored the cart in memory.", "Used zod for validation."]

    @pytest.mark.parametrize(
        "header",
        ["**Decisions:**", "## Decisions", "Decisions", "**Decisions**:", "decisions made:"],
    )
    def test_header_spellings(self, header: str) -> None:
        assert parse_decisions(f"{header}\n* one\n* two\nPASS") == ["one", "two"]

    def test_numbered_and_blank_line_after_header(self) -> None:
        assert parse_decisions("Decisions:\n\n1. a\n2) b\n\nPASS") == ["a", "b"]

    @pytest.mark.parametrize("text", ["Decisions: none\nPASS", "Decisions:\n- none\nPASS"])
    def test_none(self, text: str) -> None:
        assert parse_decisions(text) == []

    def test_inline_single_decision(self) -> None:
        assert parse_decisions("Decisions: kept the CLI flags as they are\nPASS") == [
            "kept the CLI flags as they are"
        ]

    @pytest.mark.parametrize("text", [None, "", "PASS", "I made decisions about the API.\nPASS"])
    def test_absent_is_empty(self, text: str | None) -> None:
        assert parse_decisions(text) == []

    def test_last_list_wins_and_fences_are_skipped(self) -> None:
        text = (
            "Decisions:\n- old turn\n\nThen more work.\n```md\nDecisions:\n- in code\n```\n"
            "Decisions:\n- final\nFAIL: tests red"
        )
        assert parse_decisions(text) == ["final"]

    def test_markdown_is_stripped_and_items_capped(self) -> None:
        many = "\n".join(f"- **d{i}**" for i in range(MAX_DECISIONS + 5))
        items = parse_decisions(f"Decisions:\n{many}\nPASS")
        assert len(items) == MAX_DECISIONS
        assert items[0] == "d0"
        long = parse_decisions("Decisions:\n- " + "x" * 1000)
        assert len(long[0]) == MAX_DECISION_CHARS

    def test_merge_dedupes_case_insensitively(self) -> None:
        assert merge_decisions(["A", "b"], ["a", "c", "B", "c"]) == ["A", "b", "c"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class TestStored:
    async def test_pr_loop_stores_the_developer_decisions(
        self, service: ShipcrewService, sessions: FakeSessions, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        mission = await service.create_mission("M", str(repo), None, None)
        task = await service.create_task(mission.id, title="T")
        await asyncio.to_thread(
            service.store.update_task, task.id, status="review", root_session_id="sess9"
        )
        sessions.agent_texts["sess9"] = "Decisions:\n- chose X\n- chose Y\nFAIL: no tests"
        current = await service.require_task(task.id)
        await service.pr_loop.advance(current)
        stored = await service.require_task(task.id)
        assert stored.status == "blocked"
        assert stored.decisions == ["chose X", "chose Y"]
        # A later turn adds its own, without duplicates.
        await service.pr_loop.record_decisions(stored, "Decisions:\n- chose Y\n- chose Z\nPASS")
        assert (await service.require_task(task.id)).decisions == ["chose X", "chose Y", "chose Z"]

    async def test_planner_decisions_go_to_the_mission(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        agents_dir: Path,
        tmp_path: Path,
    ) -> None:
        (agents_dir / "planner").mkdir()
        (agents_dir / "planner" / "config.yaml").write_text("name: planner\n")
        repo = tmp_path / "repo"
        (repo / ".shipcrew").mkdir(parents=True)
        r = await client.post(f"{P}/missions", json={"title": "M", "repo_path": str(repo)})
        mission_id = r.json()["id"]
        await client.post(f"{P}/missions/{mission_id}/plan", json={"prd": "Build it."})
        (repo / ".shipcrew" / "plan.json").write_text(
            json.dumps({"tasks": [{"key": "T01", "title": "One"}]})
        )
        sessions.snapshots["sess1"] = SessionSnapshot(status="idle", agent_replied=True)
        sessions.agent_texts["sess1"] = "Plan written.\nDecisions:\n- Two tasks only\nPASS"
        await service.planner.sync()
        missions = (await client.get(f"{P}/missions")).json()["missions"]
        assert missions[0]["plan"]["status"] == "imported"
        assert missions[0]["plan_decisions"] == ["Two tasks only"]
