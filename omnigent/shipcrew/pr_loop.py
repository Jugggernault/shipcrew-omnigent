"""PR loop interface: push -> PR -> CI fix x3 -> Claude review -> policy -> merge.

Stub for the next round. The scheduler moves a card to ``review`` when its
root agent finishes; this loop takes it from there to ``merged``. Every
function below documents its contract; none is implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnigent.shipcrew.store import Task

MAX_CI_FIX_ATTEMPTS = 3
TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class ReviewVerdict:
    """Outcome of the reviewer session.

    :param decision: ``"approve"`` or ``"changes"`` (the reviewer's last word).
    :param summary: The reviewer's summary comment.
    :param session_id: The reviewer child session (shows in the task's tree).
    """

    decision: Literal["approve", "changes"]
    summary: str
    session_id: str


@dataclass(frozen=True)
class MergePolicy:
    """What ``APPROVALS.md`` requires before a merge.

    :param human_approval: A human must approve, whatever the reviewer said.
    :param protected_paths: Globs whose change always needs a human.
    """

    human_approval: bool = False
    protected_paths: tuple[str, ...] = ()


async def open_pull_request(task: Task, worktree: Path) -> tuple[int, str]:
    """Push ``task/<id>`` and open a draft PR linked to the task's issue.

    TODO: ``git push -u origin task/<id>``, then ``gh pr create --draft`` with a
    body built from the task (acceptance criteria, ``Closes #issue``). Store
    ``pr_number`` / ``pr_url`` / ``ci="pending"`` on the task.

    :returns: ``(pr_number, pr_url)``.
    """
    raise NotImplementedError


async def run_ci_fix_loop(task: Task, worktree: Path) -> bool:
    """Wait for CI; on red, ask the root agent to fix, up to :data:`MAX_CI_FIX_ATTEMPTS`.

    TODO: poll :func:`omnigent.shipcrew.gh.pr_checks`; on failure send the
    failing-check summary as a new message to ``task.root_session_id`` and wait
    for the session to go idle. Update ``ci`` to ``green`` / ``red``. After the
    last failed attempt, move the card to ``intervention``.

    :returns: ``True`` when CI ended green.
    """
    raise NotImplementedError


async def request_review(task: Task, worktree: Path) -> ReviewVerdict:
    """Start a Claude reviewer as a child session of the task's root session.

    TODO: create a ``reviewer`` role session with ``parent_session_id`` set to
    ``task.root_session_id`` (so it appears in the sub-agent tree), prompt it
    with the PR diff and acceptance criteria, and parse APPROVE / CHANGES from
    its final message. The reviewer runs on Claude (no second vendor).
    """
    raise NotImplementedError


def load_merge_policy(repo_path: Path) -> MergePolicy:
    """Parse ``APPROVALS.md`` at the repo root (missing file = default policy).

    TODO: define the file format (front matter or a simple list of rules).
    """
    raise NotImplementedError


async def merge_serialized(task: Task, repo_path: Path) -> bool:
    """Merge one PR at a time per repository.

    TODO: take a per-repo lock, ``gh pr update-branch`` (conflict -> integrator
    agent), re-check CI, then :func:`omnigent.shipcrew.gh.pr_merge`. On success
    set the card to ``merged`` so dependants unblock.

    :returns: ``True`` when merged.
    """
    raise NotImplementedError


async def advance(task: Task) -> Task:
    """Drive one ``review`` card one step through the loop above.

    TODO: called by the scheduler for cards in ``review``; idempotent and
    resumable from the task's stored ``pr_number`` / ``ci`` fields.
    """
    raise NotImplementedError
