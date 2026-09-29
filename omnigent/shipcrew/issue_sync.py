"""Two-way sync between board tasks and GitHub issues / PRs, through ``gh.py``.

Board -> GitHub: every task without an issue gets one (title, acceptance
checklist, labels ``shipcrew`` + ``role:<role>``).

GitHub -> board, per tick:

- the task's PR was merged outside shipcrew -> ``merged``;
- its issue was closed (and the work was not merged) -> ``blocked`` with
  ``"issue closed on GitHub"``;
- its issue is assigned to someone, or labelled ``shipcrew:human`` -> the card
  goes to that human (the agent is interrupted through the board's usual path).

Sync only runs for missions with a ``repo_url`` while ``gh`` is logged in;
otherwise it is a no-op whose :class:`SyncReport` says why. Every ``gh`` call
has a short timeout, and one tick creates at most :data:`MAX_ISSUES_PER_TICK`
issues, so a tick stays bounded.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.shipcrew import gh
from omnigent.shipcrew.branches import task_branch
from omnigent.shipcrew.store import Mission, Task

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)

BASE_LABEL = "shipcrew"
HUMAN_LABEL = "shipcrew:human"
ROLE_LABEL_PREFIX = "role:"
CLOSED_REASON = "issue closed on GitHub"
MAX_ISSUES_PER_TICK = 20
# Human stand-in when an issue is labelled for a human but assigned to nobody.
UNNAMED_HUMAN = "github"


@dataclass(frozen=True)
class SyncReport:
    """Outcome of one mission sync.

    :param ok: The sync ran against GitHub.
    :param reason: Why it did not run (or failed), else ``None``.
    :param created: Issues opened in this sync.
    :param updated: Cards changed from GitHub state in this sync.
    """

    mission_id: str
    ok: bool
    reason: str | None = None
    created: int = 0
    updated: int = 0
    synced_at: int = 0

    def to_api(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "created": self.created,
            "updated": self.updated,
            "synced_at": self.synced_at,
        }


def issue_body(task: Task) -> str:
    """Issue text: the task body, then the acceptance checklist."""
    parts = []
    if task.body.strip():
        parts += [task.body.strip(), ""]
    parts.append("## Acceptance criteria")
    parts += [f"- [ ] {a}" for a in task.acceptance] or ["- [ ] (none given)"]
    parts += ["", f"<!-- shipcrew task {task.id} -->"]
    return "\n".join(parts)


def issue_labels(task: Task) -> list[str]:
    return [BASE_LABEL, f"{ROLE_LABEL_PREFIX}{task.role}"]


def _human_of(issue: dict[str, Any]) -> str | None:
    """Login of the human who took the issue, or ``None`` when agents keep it."""
    for assignee in issue.get("assignees") or []:
        if isinstance(assignee, dict) and assignee.get("login"):
            return str(assignee["login"])
    labels = {label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)}
    return UNNAMED_HUMAN if HUMAN_LABEL in labels else None


def _task_branches(task: Task) -> set[str]:
    """Branch names a task's PR may use: the stored branch, else the canonical scheme."""
    return {task.branch or task_branch(task.id, task.title)}


class GitHubSync:
    """Periodic, bounded issue/PR sync for every mission with a ``repo_url``."""

    def __init__(self, service: ShipcrewService) -> None:
        self._service = service
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.reports: dict[str, SyncReport] = {}

    def _report(self, mission: Mission, ok: bool, reason: str | None, **counts: int) -> SyncReport:
        report = SyncReport(mission.id, ok, reason, synced_at=int(time.time()), **counts)
        self.reports[mission.id] = report
        if reason is not None:
            _logger.info("shipcrew: GitHub sync of mission %s: %s", mission.id, reason)
        return report

    async def tick(self, now: float | None = None) -> list[SyncReport]:
        """Sync each mission whose interval has elapsed. Missions without a repo are skipped."""
        now = time.monotonic() if now is None else now
        interval = self._service.settings.sync_interval_s
        reports = []
        for mission in await self._service.list_missions():
            if not mission.repo_url:
                continue
            last = self._last.get(mission.id)
            if last is not None and now - last < interval:
                continue
            reports.append(await self.sync_mission(mission.id))
            self._last[mission.id] = now
        return reports

    async def sync_mission(self, mission_id: str) -> SyncReport:
        """One full sync of a mission now (``POST /missions/{id}/sync``)."""
        lock = self._locks.setdefault(mission_id, asyncio.Lock())
        async with lock:
            mission = await self._service.require_mission(mission_id)
            # A forced sync also restarts the mission's periodic interval.
            self._last[mission.id] = time.monotonic()
            if not mission.repo_url:
                return self._report(mission, False, "mission has no repo_url: GitHub sync is off")
            cwd = Path(mission.repo_path) if Path(mission.repo_path).is_dir() else None
            logged_in, why = await asyncio.to_thread(gh.auth_status, cwd)
            if not logged_in:
                return self._report(mission, False, why)
            try:
                created = await self._create_missing_issues(mission, cwd)
                updated = await self._pull_state(mission, cwd)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                _logger.warning("shipcrew: GitHub sync of mission %s failed", mission.id)
                return self._report(mission, False, f"GitHub sync failed: {exc}")
            return self._report(mission, True, None, created=created, updated=updated)

    async def _create_missing_issues(self, mission: Mission, cwd: Path | None) -> int:
        repo = mission.repo_url or ""
        tasks = await asyncio.to_thread(self._service.store.list_tasks, mission.id)
        todo = [t for t in tasks if t.issue_number is None and t.status != "merged"]
        todo = todo[:MAX_ISSUES_PER_TICK]
        if not todo:
            return 0
        labels = {BASE_LABEL, HUMAN_LABEL, *(label for t in todo for label in issue_labels(t))}
        await asyncio.to_thread(gh.ensure_repo_labels, repo, sorted(labels), cwd)
        for task in todo:
            number, url = await asyncio.to_thread(
                gh.create_issue, repo, task.title, issue_body(task), issue_labels(task), cwd
            )
            await self._service.update_fields(task.id, issue_number=number, issue_url=url)
        return len(todo)

    async def _pull_state(self, mission: Mission, cwd: Path | None) -> int:
        repo = mission.repo_url or ""
        issues = await asyncio.to_thread(gh.list_issues, repo, BASE_LABEL, cwd)
        prs = await asyncio.to_thread(gh.list_pull_requests, repo, cwd)
        issue_by_number = {i.get("number"): i for i in issues}
        pr_by_number = {p.get("number"): p for p in prs}
        pr_by_head = {p.get("headRefName"): p for p in prs if p.get("headRefName")}
        owner = mission.owner_user_id
        updated = 0
        for task in await asyncio.to_thread(self._service.store.list_tasks, mission.id):
            if task.status == "merged":
                continue
            pr = pr_by_number.get(task.pr_number) if task.pr_number is not None else None
            if pr is None:
                pr = next((pr_by_head[b] for b in _task_branches(task) if b in pr_by_head), None)
            if pr is not None and str(pr.get("state", "")).upper() == "MERGED":
                await self._service.patch_task(task.id, {"status": "merged"}, owner)
                await self._service.update_fields(
                    task.id, pr_number=pr.get("number"), pr_url=pr.get("url") or task.pr_url
                )
                updated += 1
                continue
            issue = issue_by_number.get(task.issue_number) if task.issue_number else None
            if issue is None:
                continue
            if str(issue.get("state", "")).upper() == "CLOSED":
                if task.status != "blocked" or task.blocked_reason != CLOSED_REASON:
                    await self._service.block_task(task.id, CLOSED_REASON, owner)
                    updated += 1
                continue
            human = _human_of(issue)
            if human is not None and not task.human_assigned:
                await self._service.patch_task(
                    task.id, {"assignee": {"kind": "human", "id": human}}, owner
                )
                updated += 1
        return updated
