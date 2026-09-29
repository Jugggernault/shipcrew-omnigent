"""Verify tasks (qa, security): test-only writes, and failures turned into fix tasks.

A verify task checks merged work. It never implements features: it may only
write test files (:data:`TEST_GLOBS`, inside its owned paths, enforced by the
bundles' guardrails and again on the PR diff) and its report file.

When its root agent finishes, :meth:`VerifyLoop.after_turn` decides what the
PR loop does with the card instead of the builder path:

- ``PASS`` with no blocker/major finding and no commits: nothing to merge, the
  card is done (``merged``, worktree removed).
- ``PASS`` with commits (tests it wrote, which pass): the normal PR loop runs
  (push, PR, CI, review, merge).
- ``FAIL`` (or ``PASS`` with blocker/major findings): no block. ONE developer
  task ``Fix: <title>`` is created in the same mission (body = the findings
  with ``file:line`` and repro, "add a regression test"; owned paths = the
  files the findings name, else the verify task's own), ``ready``. Tests the
  verify task wrote are kept on a local branch named in the fix body (they
  fail until the fix lands). The verify card goes back to ``ready`` with the
  fix task added to its ``depends_on``, so it re-verifies from the merged fix.
  After :data:`MAX_FIX_CYCLES` fix tasks the card is held in ``intervention``
  with the reason instead.

The fix count is derived from the mission's tasks (plan key
``fix:<verify task id>:<n>``), so a restart never loses it.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.shipcrew.policies import DEFAULT_TEST_GLOBS, is_test_path
from omnigent.shipcrew.store import Task

if TYPE_CHECKING:
    from omnigent.shipcrew.pr_loop import _Ctx
    from omnigent.shipcrew.service import ShipcrewService

VERIFY_ROLES = frozenset({"qa", "security"})
MAX_FIX_CYCLES = 2
FIX_KEY_PREFIX = "fix:"
VERIFY_HOLD_PREFIX = "verification still failing"
TESTS_BRANCH_PREFIX = "shipcrew-tests/"
TEST_GLOBS = DEFAULT_TEST_GLOBS
# Report files a verify role writes next to its tests.
REPORT_FILES = {"qa": ".shipcrew/qa.json", "security": ".shipcrew/security.md"}
_SERIOUS = ("blocker", "major")
_REPORT_MAX_CHARS = 6000
_MAX_FINDINGS_SHOWN = 30


def verify_writable(role: str, path: str) -> bool:
    """A test file, or the role's own report file."""
    return is_test_path(path) or path == REPORT_FILES.get(role)


def is_verify_hold(task: Task) -> bool:
    """A verify card parked after its last fix cycle (no PR; a human decides)."""
    return (
        task.status == "intervention"
        and task.role in VERIFY_ROLES
        and task.pr_number is None
        and (task.blocked_reason or "").startswith(VERIFY_HOLD_PREFIX)
    )


def fix_key(task_id: str, n: int) -> str:
    return f"{FIX_KEY_PREFIX}{task_id}:{n}"


def fix_cycles(tasks: Iterable[Task], verify_task_id: str) -> int:
    """Fix tasks already created for this verify task."""
    prefix = f"{FIX_KEY_PREFIX}{verify_task_id}:"
    return sum(1 for t in tasks if (t.plan_key or "").startswith(prefix))


def serious(findings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if f.get("severity") in _SERIOUS]


def _clean_repo_path(raw: str) -> str | None:
    """A findings ``file`` as a repo-relative path, or ``None`` when unusable."""
    path = raw.strip().strip("`'\"").replace("\\", "/")
    path = re.sub(r":\d+(?::\d+)?$", "", path)  # "src/a.ts:12" -> "src/a.ts"
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith(("/", "~")) or any(ord(c) < 32 for c in path):
        return None
    norm = posixpath.normpath(path)
    if norm == "." or norm.startswith("..") or "*" in norm:
        return None
    return norm


def fix_owned_paths(
    findings: Sequence[dict[str, Any]], test_files: Sequence[str], fallback: Sequence[str]
) -> list[str]:
    """Files the findings name plus the handed-over tests; else the verify task's paths."""
    named = [p for f in findings if (p := _clean_repo_path(str(f.get("file") or "")))]
    owned = list(dict.fromkeys([*named, *test_files]))
    return owned if named else list(dict.fromkeys([*fallback, *test_files]))


def fix_task_body(
    task: Task,
    *,
    reason: str,
    findings: Sequence[dict[str, Any]],
    cycle: int,
    report: str | None,
    tests_branch: str | None,
    test_files: Sequence[str],
) -> str:
    lines = [
        f"The `{task.role}` task **{task.title}** failed verification "
        f"(fix cycle {cycle} of {MAX_FIX_CYCLES}): {reason}",
        "",
    ]
    if findings:
        lines.append("## Findings")
        for f in list(findings)[:_MAX_FINDINGS_SHOWN]:
            where = (f.get("file") or "?") + (f":{f['line']}" if f.get("line") else "")
            lines.append(f"- [{f.get('severity', 'major')}] `{where}` — {f.get('message', '')}")
        if len(findings) > _MAX_FINDINGS_SHOWN:
            lines.append(f"- ... and {len(findings) - _MAX_FINDINGS_SHOWN} more")
        lines.append("")
    if report:
        name = REPORT_FILES.get(task.role, "report")
        lines += [f"## Verify report (`{name}`)", "```", report[:_REPORT_MAX_CHARS], "```", ""]
    if tests_branch and test_files:
        files = " ".join(test_files)
        lines += [
            "## Failing tests from the verify task",
            f"The verify task wrote these tests on local branch `{tests_branch}` (not merged): "
            + ", ".join(f"`{p}`" for p in test_files)
            + ". Bring them into your branch first, watch them fail, then fix the code:",
            f"`git checkout {tests_branch} -- {files}`",
            "",
        ]
    lines += [
        "## What to do",
        "Reproduce each finding (the repro command is in the finding or the report), fix the "
        "cause, and add a regression test for each one (a unit test that calls the route "
        "handler or lib function directly, e2e only when a browser is required). Run the whole "
        "suite in one command before you finish.",
    ]
    return "\n".join(lines)


def fix_acceptance(has_tests: bool) -> list[str]:
    acceptance = [
        "every blocker and major finding listed above is fixed",
        "a regression test covers each fixed finding and passes",
        "the full test suite and every CI command pass",
    ]
    if has_tests:
        acceptance.insert(2, "the verify task's tests named above are in the branch and pass")
    return acceptance


def _read_report(worktree: Path | None, role: str) -> str | None:
    name = REPORT_FILES.get(role)
    if worktree is None or name is None:
        return None
    path = worktree / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if name.endswith(".json"):
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), indent=2)
    return text.strip() or None


def tests_branch_name(task_id: str, cycle: int) -> str:
    """Local branch that keeps a verify task's failing tests for its fix task."""
    return f"{TESTS_BRANCH_PREFIX}{task_id[:8]}-{cycle}"


def drop_tests_branches(repo: Path, task_id: str) -> None:
    """Delete the verify task's ``shipcrew-tests/<id8>-<n>`` branches (it passed)."""
    from omnigent.shipcrew import pr_loop as pl

    pattern = f"refs/heads/{TESTS_BRANCH_PREFIX}{task_id[:8]}-*"
    listed = pl._git(["for-each-ref", "--format=%(refname:short)", pattern], repo, check=False)
    refs = listed.stdout.split()
    for ref in refs:
        pl._git(["branch", "-D", ref], repo, check=False)


class VerifyLoop:
    """The verify-role branch of the PR loop's first step (see the module docstring).

    :param service: The board service.
    """

    def __init__(self, service: ShipcrewService) -> None:
        self._svc = service

    async def after_turn(self, loop: Any, ctx: _Ctx) -> Task | None:
        """Handle a verify task whose agent finished; ``None`` = continue as a builder.

        :param loop: The :class:`~omnigent.shipcrew.pr_loop.PrLoop` (git and
            session helpers).
        """
        from omnigent.shipcrew import pr_loop as pl

        task = ctx.task
        assert task.root_session_id is not None
        text = await self._svc.sessions.last_agent_text(
            task.root_session_id, acting_user=ctx.owner
        )
        await loop.record_decisions(task, text)
        verdict = pl.parse_verdict(text)
        if verdict is None or ctx.worktree is None:
            return None  # the builder path blocks with its usual reason
        findings = pl._findings(text or "")
        bad = serious(findings)
        await loop._io(pl._fetch, ctx.worktree, ctx.base)
        base = ctx.base_ref if await loop._io(pl._rev, ctx.worktree, ctx.base_ref) else ctx.base
        count = await loop._io(pl._git, ["rev-list", "--count", f"{base}..HEAD"], ctx.worktree)
        commits = int(count.stdout.strip() or 0)
        if verdict[0] == "pass" and not bad:
            if commits:
                return None  # its tests pass: they go through the normal PR loop
            return await self._verified(loop, ctx, findings)
        reason = verdict[1] if verdict[0] == "fail" else f"{len(bad)} blocker/major finding(s)"
        cycles = fix_cycles(await self._svc.list_tasks(task.mission_id), task.id)
        if cycles >= MAX_FIX_CYCLES:
            return await loop._hold(
                ctx, f"{VERIFY_HOLD_PREFIX} after {MAX_FIX_CYCLES} fix cycles: {reason}"
            )
        return await self._hand_to_fix(loop, ctx, reason, findings, commits, base, cycles + 1)

    async def _verified(self, loop: Any, ctx: _Ctx, findings: list[dict[str, Any]]) -> Task:
        """Nothing to merge: stop the session, drop the worktree, card done."""
        from omnigent.shipcrew import pr_loop as pl

        await loop._stop(ctx, ctx.task.root_session_id)
        await loop._io(pl._cleanup_worktree, ctx.repo, ctx.worktree, ctx.branch, ctx.base)
        await loop._io(drop_tests_branches, ctx.repo, ctx.task.id)
        summary = f"{ctx.task.role}: PASS, nothing to merge"
        return await loop._update(
            ctx.task,
            status="merged",
            blocked_reason=None,
            review={"verdict": "approve", "summary": summary, "findings": findings},
        )

    async def _hand_to_fix(
        self,
        loop: Any,
        ctx: _Ctx,
        reason: str,
        findings: list[dict[str, Any]],
        commits: int,
        base: str,
        cycle: int,
    ) -> Task:
        from omnigent.shipcrew import pr_loop as pl

        task = ctx.task
        assert ctx.worktree is not None
        tests_branch: str | None = None
        test_files: list[str] = []
        if commits:
            diff = ["diff", "-z", "--no-renames", "--name-only", f"{base}...HEAD"]
            names = await loop._io(pl._git, diff, ctx.worktree)
            test_files = [n for n in names.stdout.split("\0") if n and is_test_path(n)]
            if test_files:
                tests_branch = tests_branch_name(task.id, cycle)
                await loop._io(pl._git, ["branch", "-f", tests_branch, "HEAD"], ctx.worktree)
        report = _read_report(ctx.worktree, task.role)
        fix = await self._svc.create_task(
            task.mission_id,
            title=f"Fix: {task.title}"[:512],
            body=fix_task_body(
                task,
                reason=reason,
                findings=findings,
                cycle=cycle,
                report=report,
                tests_branch=tests_branch,
                test_files=test_files,
            ),
            acceptance=fix_acceptance(bool(tests_branch)),
            role="developer",
            owned_paths=fix_owned_paths(
                serious(findings) or findings, test_files, task.owned_paths
            ),
        )
        await self._svc.update_fields(fix.id, plan_key=fix_key(task.id, cycle), status="ready")
        # The verify card re-runs from scratch once the fix is merged.
        await loop._stop(ctx, task.root_session_id)
        await loop._io(pl._cleanup_worktree, ctx.repo, ctx.worktree, ctx.branch, ctx.base)
        return await loop._update(
            task,
            status="ready",
            depends_on=[*task.depends_on, fix.id],
            blocked_reason=None,
            session_seen_active=False,
        )
