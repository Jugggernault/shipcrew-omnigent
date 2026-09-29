"""Verify tasks (qa, security): done without a PR, a PR of their tests, or a fix task.

Same harness as ``test_pr_loop.py``: real git worktrees and a local bare
remote, the fake ``gh``, scripted agents.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from omnigent.shipcrew.scheduler import ShipcrewScheduler
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import RootSessionRequest, SessionSnapshot
from omnigent.shipcrew.store import Task
from omnigent.shipcrew.verify import (
    MAX_FIX_CYCLES,
    fix_cycles,
    fix_key,
    fix_owned_paths,
    fix_task_body,
    is_verify_hold,
    verify_writable,
)

from . import test_pr_loop as _loop
from .test_pr_loop import OWNER, LoopSessions, commit, gh_state_of, git, run_until, status_is

# The PR-loop harness fixtures (real git + fake gh), shared as is.
repo = _loop.repo
gh_state = _loop.gh_state
scheduler = _loop.scheduler
settings = _loop.settings

FINDINGS_FAIL = """Checked the demo script: the cart total is wrong.

```json
{"findings": [{"file": "src/cart.js", "line": 12, "severity": "blocker",
  "message": "total ignores quantity; repro: `npm test -- cart`"}]}
```
FAIL: 1 failures"""

Agent = Callable[[Path], str]


@dataclass
class RoleSessions(LoopSessions):
    """``LoopSessions`` whose root agents act at start, per role (``agent_dir.name``)."""

    agents: dict[str, list[Agent]] = field(default_factory=dict)
    roles_started: list[str] = field(default_factory=list)

    async def create_root_session(self, request: RootSessionRequest) -> str:
        session_id = await super().create_root_session(request)
        role = request.agent_dir.name
        self.roles_started.append(role)
        queue = self.agents.get(role) or []
        if queue:
            self.agent_texts[session_id] = queue.pop(0)(self.worktrees[session_id])
        return session_id


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    for role in ("developer", "reviewer", "integrator", "qa", "security"):
        (root / role).mkdir(parents=True)
        (root / role / "config.yaml").write_text(f"name: {role}\n")
    return root


@pytest.fixture
def sessions(tmp_path: Path, repo: Path, gh_state: Path) -> RoleSessions:
    # CI of the harness is green once `ok` exists: put it on main so a
    # tests-only PR can go green.
    commit(repo, "ok", "", "ci ok")
    git(repo, "push", "-q", "origin", "main")
    return RoleSessions(repo=repo, wt_root=tmp_path / "wt")


async def _verify_task(
    service: ShipcrewService, repo: Path, *, role: str = "qa", owned: list[str] | None = None
) -> Task:
    mission = await service.create_mission("M", str(repo), None, OWNER)
    task = await service.create_task(
        mission.id,
        title="Verify the cart",
        acceptance=["demo script passes"],
        role=role,
        owned_paths=owned if owned is not None else ["tests/**", "e2e/**"],
    )
    return await service.patch_task(task.id, {"status": "ready"}, OWNER)


def _says(text: str, files: dict[str, str] | None = None) -> Agent:
    def act(wt: Path) -> str:
        for path, content in (files or {}).items():
            commit(wt, path, content, f"add {path}")
        return text

    return act


async def _tasks(service: ShipcrewService, mission_id: str) -> list[Task]:
    return await service.list_tasks(mission_id)


# ── pure helpers ────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize(
        ("path", "ok"),
        [
            ("tests/cart.test.ts", True),
            ("test/sum.js", True),
            ("e2e/cart.spec.ts", True),
            ("src/__tests__/cart.ts", True),
            ("app/cart/page.test.tsx", True),
            ("src/components/__snapshots__/a.snap", True),
            (".shipcrew/qa.json", True),
            (".shipcrew/security.md", False),
            ("src/cart.ts", False),
            ("src/tests/helper.ts", False),  # a deep tests/ folder may be app code
            ("package.json", False),
            ("tests/../src/cart.ts", False),
        ],
    )
    def test_qa_writable(self, path: str, ok: bool) -> None:
        assert verify_writable("qa", path) is ok

    def test_fix_owned_paths_from_findings_with_fallback(self) -> None:
        findings = [
            {"file": "./src/cart.js:12", "severity": "blocker"},
            {"file": "/etc/passwd", "severity": "major"},
            {"file": "../x.js", "severity": "major"},
            {"file": "src/cart.js", "severity": "major"},
        ]
        assert fix_owned_paths(findings, ["tests/cart.test.js"], ["tests/**"]) == [
            "src/cart.js",
            "tests/cart.test.js",
        ]
        assert fix_owned_paths([{"file": ""}], [], ["tests/**", "app/**"]) == [
            "tests/**",
            "app/**",
        ]

    def test_fix_body_names_findings_and_the_tests_branch(self) -> None:
        task = Task(id="a" * 32, mission_id="m", title="Verify", role="qa")
        body = fix_task_body(
            task,
            reason="1 failures",
            findings=[{"file": "src/a.js", "line": 3, "severity": "blocker", "message": "boom"}],
            cycle=1,
            report='{"pass": false}',
            tests_branch="shipcrew-tests/aaaaaaaa-1",
            test_files=["tests/a.test.js"],
        )
        assert "fix cycle 1 of 2" in body
        assert "- [blocker] `src/a.js:3` — boom" in body
        assert "git checkout shipcrew-tests/aaaaaaaa-1 -- tests/a.test.js" in body
        assert "regression test" in body
        assert '{"pass": false}' in body

    def test_fix_cycles_counts_plan_keys(self) -> None:
        tasks = [
            Task(id="f1", mission_id="m", title="Fix", plan_key=fix_key("q", 1)),
            Task(id="f2", mission_id="m", title="Fix", plan_key=fix_key("q", 2)),
            Task(id="o", mission_id="m", title="Other", plan_key=fix_key("other", 1)),
            Task(id="p", mission_id="m", title="Plan", plan_key="T01"),
        ]
        assert fix_cycles(tasks, "q") == 2


# ── the loop ────────────────────────────────────────────────────


class TestVerifyLoop:
    async def test_pass_without_commits_is_done_without_a_pr(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
        gh_state: Path,
    ) -> None:
        sessions.agents["qa"] = [_says("Demo script walked, 0 console errors.\nPASS")]
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"))
        assert done.pr_number is None
        assert done.review is not None and "nothing to merge" in done.review["summary"]
        assert gh_state_of(gh_state)["prs"] == {}
        assert done.root_session_id in sessions.stopped
        assert git(repo, "worktree", "list").count("\n") == 0  # only the main checkout
        assert len(await _tasks(service, task.mission_id)) == 1

    async def test_verify_decisions_reach_the_task(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        # The ship report lists per-task decisions: verify roles write them too.
        sessions.agents["qa"] = [
            _says("Walked the demo.\n\nDecisions:\n- skipped the e2e, unit tests cover it\nPASS")
        ]
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"))
        assert done.decisions == ["skipped the e2e, unit tests cover it"]

    async def test_passing_tests_it_wrote_go_through_the_pr_loop_unreviewed(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
        gh_state: Path,
    ) -> None:
        sessions.agents["qa"] = [
            _says("Added a regression e2e.\nPASS", {"e2e/cart.spec.ts": "test('x', ()=>{})\n"})
        ]
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"))
        assert done.pr_number is not None
        # Tests-only diff with CI green: no reviewer session.
        assert sessions.roles() == []
        assert done.review is not None
        assert done.review["summary"] == "review skipped: tests-only diff with CI green"
        assert (repo / "e2e" / "cart.spec.ts").exists()

    async def test_fix_task_owns_the_minor_findings_too(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        # Live run 2: a missing favicon (minor) survived the fix cycle because
        # the fix task did not own it. Cheap minor fixes ride along now.
        reply = FINDINGS_FAIL.replace(
            "}]}",
            '}, {"file": "app/favicon.ico", "line": null, "severity": "minor", '
            '"message": "GET /favicon.ico is 404"}]}',
        )
        sessions.agents["qa"] = [_says(reply)]
        task = await _verify_task(service, repo)
        await run_until(scheduler, service, task.id, lambda t: len(t.depends_on) == 1)
        (fix,) = [t for t in await _tasks(service, task.mission_id) if t.id != task.id]
        assert fix.owned_paths[:2] == ["src/cart.js", "app/favicon.ico"]
        assert "[minor] `app/favicon.ico`" in fix.body
        assert "Fix the minor findings too when that is cheap" in fix.body

    async def test_fail_creates_one_fix_task_and_requeues_the_verify_card(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        sessions.agents["qa"] = [_says(FINDINGS_FAIL)]
        task = await _verify_task(service, repo)
        await run_until(scheduler, service, task.id, lambda t: len(t.depends_on) == 1)
        verify = await service.require_task(task.id)
        tasks = await _tasks(service, task.mission_id)
        (fix,) = [t for t in tasks if t.id != task.id]
        assert (fix.title, fix.role, fix.plan_key) == (
            "Fix: Verify the cart",
            "developer",
            fix_key(task.id, 1),
        )
        assert fix.status in ("ready", "running")  # the same tick may start it
        assert fix.owned_paths[:1] == ["src/cart.js"]
        assert "src/cart.test.*" in fix.owned_paths  # its regression test is owned
        assert "`src/cart.js:12`" in fix.body and "npm test -- cart" in fix.body
        assert any("regression test" in a for a in fix.acceptance)
        # The verify card waits for the fix, with a fresh worktree next time.
        assert verify.status == "ready" and verify.depends_on == [fix.id]
        assert verify.root_session_id in sessions.stopped
        assert not (sessions.wt_root / task.id).exists()  # type: ignore[operator]
        assert (verify.blocked_reason or "").startswith("waiting on dependencies")

    async def test_failing_tests_it_wrote_are_handed_to_the_fix_task(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
        gh_state: Path,
    ) -> None:
        sessions.agents["qa"] = [
            _says(FINDINGS_FAIL, {"tests/cart.test.js": "assert(false)\n"}),
        ]
        task = await _verify_task(service, repo)
        await run_until(scheduler, service, task.id, lambda t: len(t.depends_on) == 1)
        (fix,) = [t for t in await _tasks(service, task.mission_id) if t.id != task.id]
        branch = f"shipcrew-tests/{task.id[:8]}-1"
        assert f"git checkout {branch} -- tests/cart.test.js" in fix.body
        assert fix.owned_paths[:2] == ["src/cart.js", "tests/cart.test.js"]
        # Kept locally (the fix reads it), never pushed, and no PR for the failing tests.
        assert git(repo, "branch", "--list", branch).strip() == branch
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}") == ""
        assert gh_state_of(gh_state)["prs"] == {}

    async def test_pass_with_a_major_finding_still_gets_a_fix(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        text = (
            '```json\n{"findings": [{"file": "app/api/cart/route.ts", "line": 4, '
            '"severity": "major", "message": "IDOR on cart id"}]}\n```\nPASS'
        )
        sessions.agents["security"] = [_says(text)]
        task = await _verify_task(service, repo, role="security")
        await run_until(scheduler, service, task.id, lambda t: len(t.depends_on) == 1)
        (fix,) = [t for t in await _tasks(service, task.mission_id) if t.id != task.id]
        assert fix.owned_paths[:1] == ["app/api/cart/route.ts"]
        assert "1 blocker/major finding(s)" in fix.body

    async def test_fix_merges_then_the_card_reverifies_and_passes(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        sessions.agents["qa"] = [
            _says(FINDINGS_FAIL, {"tests/cart.test.js": "assert(total == 6)\n"}),
            _says("Re-verified.\nPASS"),
        ]

        def developer(wt: Path) -> str:
            # Follow the fix body: take the verify task's tests, then fix the code.
            tests_branch = next(
                line.split("`")[1]
                for line in sessions.created[-1].prompt.splitlines()
                if line.startswith("`git checkout ")
            ).split()[2]
            subprocess.run(
                ["git", "checkout", tests_branch, "--", "tests/cart.test.js"], cwd=wt, check=True
            )
            commit(wt, "src/cart.js", "total = sum(qty * price)\n", "fix total")
            return "Fixed the total, the handed-over test passes.\nPASS"

        sessions.agents["developer"] = [developer]
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"), 30)
        assert sessions.roles_started == ["qa", "developer", "qa"]
        (fix,) = [t for t in await _tasks(service, task.mission_id) if t.id != task.id]
        assert fix.status == "merged"
        assert (repo / "src" / "cart.js").exists() and (repo / "tests" / "cart.test.js").exists()
        assert done.pr_number is None  # the re-run passed with nothing new to merge
        # The handed-over tests branch is gone once the card passed.
        assert git(repo, "branch", "--list", "shipcrew-tests/*") == ""

    async def test_after_the_last_fix_cycle_the_card_is_held(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        sessions.agents["qa"] = [_says(FINDINGS_FAIL)]
        task = await _verify_task(service, repo)
        for n in range(1, MAX_FIX_CYCLES + 1):
            old = await service.create_task(task.mission_id, title=f"old fix {n}")
            await service.update_fields(old.id, plan_key=fix_key(task.id, n), status="merged")
        held = await run_until(scheduler, service, task.id, status_is("intervention"))
        assert is_verify_hold(held)
        assert held.blocked_reason == ("verification still failing after 2 fix cycles: 1 failures")
        assert len(await _tasks(service, task.mission_id)) == 1 + MAX_FIX_CYCLES
        # The idle session does not flip the hold back to review...
        for _ in range(3):
            await scheduler.tick()
        assert (await service.require_task(task.id)).status == "intervention"
        # ... until a human talks to the agent in its session.
        assert held.root_session_id is not None
        sessions.snapshots[held.root_session_id] = SessionSnapshot(status="running")
        await service.sync_active()
        assert (await service.require_task(task.id)).status == "running"

    async def test_a_verify_pr_with_non_test_files_needs_approval(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        # Code a test ran (or a policy gap) changed app code: the server backstop.
        sessions.agents["qa"] = [
            _says("PASS", {"tests/a.test.js": "ok\n", "src/app.js": "patched\n"}),
        ]
        task = await _verify_task(service, repo, owned=["tests/**", "src/**"])
        held = await run_until(scheduler, service, task.id, status_is("intervention"))
        assert held.needs_human_approval
        reasons = held.approval_reasons
        assert any("qa task changed non-test files: src/app.js" in r for r in reasons)
        assert sessions.roles() == ["reviewer"]  # not tests-only: reviewed
        await asyncio.sleep(0)

    async def test_its_report_file_in_the_pr_needs_no_approval(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        sessions.agents["qa"] = [
            _says("PASS", {"tests/a.test.js": "ok\n", ".shipcrew/qa.json": '{"pass": true}\n'}),
        ]
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"))
        assert (done.needs_human_approval, done.approval_reasons) == (False, [])


class TestVerifyNudge:
    async def test_verify_turn_without_verdict_is_nudged_once(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        from omnigent.shipcrew import pr_loop

        # The first turn ends without a verdict; after the nudge it passes.
        sessions.agents["qa"] = [_says("The ask was declined.")]
        sessions.developer = lambda wt, message: "Walked the demo.\nPASS"
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("merged"))
        assert [m[1] for m in sessions.messages] == [pr_loop.VERDICT_NUDGE]
        assert done.pr_number is None and done.verdict_nudges == 0

    async def test_verify_second_missing_verdict_blocks(
        self,
        service: ShipcrewService,
        sessions: RoleSessions,
        repo: Path,
        scheduler: ShipcrewScheduler,
    ) -> None:
        from omnigent.shipcrew import pr_loop

        sessions.agents["qa"] = [_says("The ask was declined.")]
        sessions.developer = lambda wt, message: "Still no verdict."
        task = await _verify_task(service, repo)
        done = await run_until(scheduler, service, task.id, status_is("blocked"))
        assert "without a PASS/FAIL line" in (done.blocked_reason or "")
        assert [m[1] for m in sessions.messages] == [pr_loop.VERDICT_NUDGE]
