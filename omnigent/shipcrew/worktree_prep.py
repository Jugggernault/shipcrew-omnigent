"""Get a new worktree ready before an agent session starts in it.

Every task, ship and loop-child (reviewer, integrator) worktree goes through
:func:`prepare_worktree`:

1. **Agent notes stay out of commits.** Next.js 16 ``next dev`` / ``next
   build`` write ``AGENTS.md`` and ``CLAUDE.md`` into the project root, and an
   agent once committed them with ``git add -A``. When the repository does not
   track them, ``/AGENTS.md`` and ``/CLAUDE.md`` go into the repository's
   ``info/exclude`` (``git rev-parse --git-path info/exclude``: shared by every
   worktree of the repository, never committed). A repository that tracks
   them keeps them tracked (an exclude never hides a tracked file).
2. **node_modules seeded** (:func:`omnigent.shipcrew.deps_seed.seed_node_modules`)
   from the main checkout (which :mod:`omnigent.shipcrew.main_deps` keeps
   installed; a running background install there is waited for, bounded, so
   a half-written tree is never copied), else from the extra sources (the
   task worktree for a reviewer checkout of the same head).

Best effort: any failure is logged and the session starts anyway.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

from omnigent.shipcrew import main_deps
from omnigent.shipcrew.deps_seed import seed_node_modules

_logger = logging.getLogger(__name__)

AGENT_NOTE_FILES = ("AGENTS.md", "CLAUDE.md")
_EXCLUDE_HEADER = "# shipcrew: tool-written agent notes (next dev / next build), never committed"
_GIT_TIMEOUT_S = 15.0
# How long a start waits for the main checkout's background install to finish.
MAIN_INSTALL_WAIT_S = 90.0


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def exclude_agent_notes(workspace: str) -> list[str]:
    """Add ``/AGENTS.md`` / ``/CLAUDE.md`` to the repo's ``info/exclude`` when untracked.

    :param workspace: A worktree (or checkout) of the repository.
    :returns: The patterns added now (``[]`` when already there, tracked, or
        on any git failure).
    """
    root = Path(workspace)
    where = _git(["rev-parse", "--git-path", "info/exclude"], root)
    if where is None or where.returncode != 0 or not where.stdout.strip():
        return []
    exclude = Path(where.stdout.strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    wanted: list[str] = []
    for name in AGENT_NOTE_FILES:
        tracked = _git(["ls-files", "--error-unmatch", "--", name], root)
        if tracked is None:
            return []
        if tracked.returncode != 0:
            wanted.append(f"/{name}")
    try:
        existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
        added = [p for p in wanted if p not in existing]
        if not added:
            return []
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = list(existing)
        if lines and lines[-1].strip():
            lines.append("")
        if _EXCLUDE_HEADER not in existing:
            lines.append(_EXCLUDE_HEADER)
        lines += added
        exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        _logger.debug("shipcrew: could not write %s: %s", exclude, exc)
        return []
    return added


def prepare_worktree(
    repo_path: str, workspace: str, *, seed_from: Sequence[str] = ()
) -> str | None:
    """Exclude the agent notes, then seed ``node_modules`` (see the module docstring).

    :param repo_path: The mission repo's main checkout, e.g. ``"/work/app"``.
    :param workspace: The new worktree, e.g. ``"/work/app-worktrees/shipcrew-review-.."``.
    :param seed_from: Other checkouts to seed from when the main checkout's
        lockfile differs (e.g. the task worktree for its reviewer).
    :returns: How ``node_modules`` was seeded (``"reflink"`` / ``"hardlink"``),
        or ``None``.
    """
    try:
        exclude_agent_notes(workspace)
    except Exception:  # noqa: BLE001 - never blocks a start
        _logger.debug("shipcrew: agent-notes exclude failed for %s", workspace, exc_info=True)
    try:
        repo = Path(repo_path)
        if repo.is_dir() and not main_deps.wait_idle(repo, MAIN_INSTALL_WAIT_S):
            # Still installing: its node_modules is half written. Seed from the others.
            return seed_node_modules(repo_path, workspace, fallbacks=seed_from, skip_primary=True)
        return seed_node_modules(repo_path, workspace, fallbacks=seed_from)
    except Exception:  # noqa: BLE001 - never blocks a start
        _logger.debug("shipcrew: node_modules seed failed for %s", workspace, exc_info=True)
        return None
