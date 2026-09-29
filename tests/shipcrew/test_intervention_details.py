"""Interventions record WHAT a human was asked: policy name + redacted preview."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from omnigent.shipcrew.service import map_session_state
from omnigent.shipcrew.sessions import (
    SessionSnapshot,
    ask_summary,
    redact_preview,
    snapshot_from_payload,
)
from omnigent.shipcrew.store import ShipcrewStore, Task


def _store(tmp_path: Path) -> ShipcrewStore:
    return ShipcrewStore(create_engine(f"sqlite:///{tmp_path / 's.db'}"))


ASK = {
    "type": "response.elicitation_request",
    "elicitation_id": "e1",
    "params": {
        "policy_name": "shipcrew_shell_allowlist",
        "content_preview": "npm install -D vitest @playwright/test 2>&1 | head -30",
        "message": "`npm install -D ...` is not on the builder shell allowlist.",
    },
}


def test_snapshot_reads_the_pending_ask() -> None:
    snap = snapshot_from_payload({"status": "waiting", "pending_elicitations": [ASK]})
    assert snap.awaiting_human
    assert snap.pending_ask == {
        "policy": "shipcrew_shell_allowlist",
        "preview": "npm install -D vitest @playwright/test 2>&1 | head -30",
    }
    assert snapshot_from_payload({"status": "idle"}).pending_ask is None


def test_message_is_the_fallback_preview() -> None:
    event = {"params": {"policy_name": "p", "message": "Write .github/workflows/ci.yml"}}
    assert ask_summary(event) == {"policy": "p", "preview": "Write .github/workflows/ci.yml"}
    assert ask_summary({"params": {}}) is None


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("API_KEY=abc123 npm test", "abc123"),
        ("curl -H 'Authorization: Bearer tok_123456789' x", "tok_123456789"),
        ("git clone https://user:hunter2@github.com/x", "hunter2"),
        ("echo ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
        ("DATABASE_URL=postgres://u:p@h/db pnpm dev", "postgres://u:p@h/db"),
        ("--token sk-ant-abcdefghijk", "sk-ant-abcdefghijk"),
    ],
)
def test_preview_never_carries_secrets(text: str, hidden: str) -> None:
    out = redact_preview(text)
    assert hidden not in out and "***" in out


def test_preview_is_one_short_line() -> None:
    out = redact_preview("a\nb " + "x" * 300)
    assert "\n" not in out and len(out) == 120 and out.endswith("…")


def test_entering_intervention_logs_policy_and_preview(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mission = store.create_mission("M", str(tmp_path))
    task = store.create_task(mission.id, title="T")
    task = store.update_task(task.id, status="running")
    assert task is not None
    snap = SessionSnapshot(status="waiting", awaiting_human=True, pending_ask={
        "policy": "shipcrew_shell_allowlist", "preview": "sed -i s/a/b/ x"})  # fmt: skip
    changes = map_session_state(task, snap)
    assert changes["status"] == "intervention"
    assert changes["blocked_reason"] == "Needs approval: shipcrew_shell_allowlist: sed -i s/a/b/ x"
    updated = store.update_task(task.id, **changes)
    assert updated is not None
    [entry] = updated.interventions
    assert entry["policy"] == "shipcrew_shell_allowlist"
    assert entry["preview"] == "sed -i s/a/b/ x"
    assert entry["reason"] == "shipcrew_shell_allowlist: sed -i s/a/b/ x"
    # Answered: back to running, the reason badge clears.
    resumed = map_session_state(updated, SessionSnapshot(status="running"))
    assert resumed["status"] == "running" and resumed["blocked_reason"] is None


def test_a_plain_task_update_still_works(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mission = store.create_mission("M", str(tmp_path))
    task: Task = store.create_task(mission.id, title="T")
    updated = store.update_task(task.id, status="intervention")
    assert updated is not None
    assert updated.interventions[0]["reason"] == "the agent asked a human (approval or input)"
