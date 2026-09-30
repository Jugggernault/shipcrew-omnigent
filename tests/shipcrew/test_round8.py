"""Round 8: the last live-run-4 interventions.

* a read-only grep chain with an ANSI-C string (``grep -c $'\\u00a0' f``) is
  ALLOW for read-only roles (it asked "substitutions, heredocs ...");
* a first turn the runner rejects (``runner_error``) before any agent output
  is re-sent once, and the session keeps reading as ``running``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.shipcrew import sessions as sessions_mod
from omnigent.shipcrew.policies import parse_command, shell_allowlist
from omnigent.shipcrew.sessions import OmnigentSessionService, RootSessionRequest

from .test_sessions import _patch_worktrees, _request, _StubApp, _Worktrees


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


# ── 5. ANSI-C strings in read-only chains ─────────────────────────

LIVE_REVIEWER_CHAIN = (
    'cat components/ui/progress.tsx; grep -n "result-\\|--radius" app/globals.css; '
    'grep -n "className=" components/ui/card.tsx | head -3; '
    "grep -c $' ' lib/results.ts; "
    'grep -rn "\\.skip" components/poll e2e/vote* lib/*.test.ts'
)


def _reader() -> Any:
    allow = ["cat", "head", "grep"]
    return shell_allowlist(allow=allow, read_only=allow, shell_writes=False, role="reviewer")


class TestAnsiC:
    @pytest.mark.parametrize(
        "command",
        [
            LIVE_REVIEWER_CHAIN,
            "grep -c $'\\u00a0' lib/results.ts",
            "grep -n $'\\t' app/page.tsx",
            "grep -c $'\\xc2\\xa0' a.ts",
        ],
    )
    def test_read_only_chain_is_allowed(self, command: str) -> None:
        assert _reader()(_bash(command), {})["result"] == "ALLOW", command

    def test_decoded_value_is_what_the_allowlist_sees(self) -> None:
        segments = parse_command("grep -c $'\\u00a0' lib/x.ts", params=True)
        assert segments is not None and segments[0].argv == ["grep", "-c", "\xa0", "lib/x.ts"]
        # a refused option spelled as an ANSI-C string is still refused
        refused = parse_command("git diff $'--output=/tmp/x'", params=True)
        assert refused is not None and refused[0].argv == ["git", "diff", "--output=/tmp/x"]

    @pytest.mark.parametrize(
        "command", ["echo $'\\cA'", "echo $'it\\'s'", "echo $'\\0'", "echo $'open"]
    )
    def test_undecodable_ansi_c_still_asks(self, command: str) -> None:
        assert parse_command(command, params=True) is None
        assert _reader()(_bash(command), {})["result"] == "ASK"


# ── 4. first-turn retry ───────────────────────────────────────────

RUNNER_204 = {
    "status": "failed",
    "runner_id": "run_1",
    "last_task_error": {"code": "runner_error", "message": "turn failed (status 204)"},
}
USER_ITEM = {"type": "message", "role": "user"}
Started = tuple[_StubApp, OmnigentSessionService, RootSessionRequest]


@pytest.fixture
def started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_StubApp, OmnigentSessionService, RootSessionRequest]:
    monkeypatch.setattr(sessions_mod, "_FIRST_TURN_RETRY_BACKOFF_S", 0.0)
    bundle = tmp_path / "planner"
    bundle.mkdir()
    (bundle / "config.yaml").write_text("name: planner\n")
    stub = _StubApp(host_ids=["host_a"])
    _patch_worktrees(
        monkeypatch, _Worktrees(listed=[{"path": "/repo", "branch": "main", "is_main": True}])
    )
    return stub, OmnigentSessionService(stub.app, None), _request(bundle)


def _prompts(stub: _StubApp) -> list[str]:
    return [e["data"]["content"][0]["text"] for _, e in stub.events if e.get("type") == "message"]


class TestFirstTurnRetry:
    async def test_runner_error_before_output_is_resent_once(
        self, started: Started, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub, service, request = started
        session_id = await service.create_root_session(request)
        stub.session_body, stub.latest_items = dict(RUNNER_204), [USER_ITEM]
        snap = await service.snapshot(session_id, acting_user=None)
        assert snap is not None and (snap.status, snap.error) == ("running", None)
        assert _prompts(stub) == ["# Add login", "# Add login"]
        # the stale failure right after the re-send still reads as running
        snap = await service.snapshot(session_id, acting_user=None)
        assert snap is not None and snap.status == "running"
        assert len(_prompts(stub)) == 2
        # a failure that outlives the grace is real: reported, not re-sent again
        monkeypatch.setattr(sessions_mod, "_FIRST_TURN_RETRY_GRACE_S", 0.0)
        snap = await service.snapshot(session_id, acting_user=None)
        assert snap is not None and (snap.status, snap.error_code) == ("failed", "runner_error")
        assert len(_prompts(stub)) == 2

    @pytest.mark.parametrize(
        ("error", "items"),
        [
            # the agent already answered: a mid-turn failure, not a delivery race
            (RUNNER_204["last_task_error"], [{"type": "message", "role": "assistant"}]),
            ({"code": "rate_limited", "message": "429"}, [USER_ITEM]),
        ],
    )
    async def test_other_failures_are_reported(
        self,
        started: Started,
        error: dict[str, str],
        items: list[dict[str, Any]],
    ) -> None:
        stub, service, request = started
        session_id = await service.create_root_session(request)
        stub.session_body = {"status": "failed", "last_task_error": error}
        stub.latest_items = items
        snap = await service.snapshot(session_id, acting_user=None)
        assert snap is not None and snap.status == "failed"
        assert len(_prompts(stub)) == 1

    async def test_a_later_turn_is_not_retried(self, started: Started) -> None:
        stub, service, request = started
        session_id = await service.create_root_session(request)
        await service.send_message(session_id, "fix CI", acting_user=None)
        stub.session_body, stub.latest_items = dict(RUNNER_204), [USER_ITEM]
        snap = await service.snapshot(session_id, acting_user=None)
        assert snap is not None and snap.status == "failed"
        assert _prompts(stub) == ["# Add login", "fix CI"]

    async def test_unknown_sessions_are_untouched(self, started: Started) -> None:
        stub, service, _ = started
        stub.session_body, stub.latest_items = dict(RUNNER_204), [USER_ITEM]
        snap = await service.snapshot("conv_other", acting_user=None)
        assert snap is not None and snap.status == "failed" and _prompts(stub) == []
