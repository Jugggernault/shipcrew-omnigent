"""The server installs the mission's CI workflow before any task branch is cut.

Agents may not change ``.github/workflows/**`` without a human (the
``workflows_guard`` policy asks), so the Foundation task used to stop on an
approval card when it wrote ``ci.yml`` itself. Instead, when the first task of
a mission starts, :func:`ensure_ci_workflow` commits
``omnigent/shipcrew/templates/ci.yml`` to ``origin/<base>`` as one
``chore: shipcrew CI`` commit, unless the base already has
``.github/workflows/ci.yml``. The template is package-manager agnostic (pnpm,
npm or yarn from the lockfile at run time), so the scaffolder only has to make
the ``package.json`` scripts match it.

The commit is built with git plumbing on a temporary index (``read-tree`` of
``origin/<base>``, the template blob added, ``write-tree``, ``commit-tree``)
and pushed with a plain fast-forward ``git push origin <sha>:refs/heads/<base>``:
the main checkout's files and index are never touched, and a push that races
another one is retried once on the new base. Afterwards the local base is
fast-forwarded when it is checked out and clean (like the PR loop's cleanup),
so task worktrees fork from the commit that has CI.

git runs through :func:`omnigent.shipcrew.tools.session_env` (the same seam as
the PR loop), with bounded timeouts. Blocking: async callers use a thread.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnigent.shipcrew.tools import session_env

_logger = logging.getLogger(__name__)

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_COMMIT_MESSAGE = "chore: shipcrew CI"
CI_TEMPLATE = Path(__file__).parent / "templates" / "ci.yml"
_GIT_TIMEOUT_S = 60.0
_PUSH_TIMEOUT_S = 120.0
# Used only when the repository has no committer identity of its own.
_FALLBACK_NAME = "shipcrew"
_FALLBACK_EMAIL = "shipcrew@users.noreply.github.com"

Status = Literal["installed", "present", "skipped", "failed"]


@dataclass(frozen=True)
class CiInstall:
    """Outcome of :func:`ensure_ci_workflow`.

    :param status: ``installed`` (pushed now), ``present`` (the base already
        has the workflow), ``skipped`` (no git checkout, no ``origin``, no
        base branch on it yet) or ``failed`` (fetch or push refused).
    :param detail: Why, for logs and the board.
    :param commit: The pushed commit SHA when installed.
    """

    status: Status
    detail: str = ""
    commit: str | None = None


def _git(
    args: Sequence[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**session_env(), "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", f"git {args[0]}: {exc}")


def _out(r: subprocess.CompletedProcess[str]) -> str:
    return (r.stderr or r.stdout).strip()[-400:]


def _identity_env(repo: Path) -> dict[str, str]:
    """Committer identity: the repo's own, else a fixed shipcrew one."""
    env: dict[str, str] = {}
    if not _git(["config", "user.name"], repo).stdout.strip():
        env.update(GIT_AUTHOR_NAME=_FALLBACK_NAME, GIT_COMMITTER_NAME=_FALLBACK_NAME)
    if not _git(["config", "user.email"], repo).stdout.strip():
        env.update(GIT_AUTHOR_EMAIL=_FALLBACK_EMAIL, GIT_COMMITTER_EMAIL=_FALLBACK_EMAIL)
    return env


def _commit_with_workflow(repo: Path, parent: str, content: str) -> str | None:
    """A commit on top of *parent* that adds the workflow file (index untouched)."""
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=content)
    if blob.returncode:
        return None
    with tempfile.TemporaryDirectory(prefix="shipcrew-ci-") as tmp:
        index = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        steps = [
            ["read-tree", parent],
            ["update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.strip()},"
             f"{CI_WORKFLOW_PATH}"],
        ]  # fmt: skip
        for step in steps:
            if _git(step, repo, env=index).returncode:
                return None
        tree = _git(["write-tree"], repo, env=index)
    if tree.returncode:
        return None
    commit = _git(
        ["commit-tree", tree.stdout.strip(), "-p", parent, "-m", CI_COMMIT_MESSAGE],
        repo,
        env=_identity_env(repo),
    )
    return (commit.stdout.strip() or None) if commit.returncode == 0 else None


def _fast_forward_local_base(repo: Path, base: str) -> None:
    current = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    clean = _git(["status", "--porcelain", "--untracked-files=no"], repo)
    if current.stdout.strip() == base and clean.returncode == 0 and not clean.stdout.strip():
        _git(["merge", "--ff-only", "--quiet", f"origin/{base}"], repo)


def ensure_ci_workflow(
    repo_path: str | Path, base: str = "main", *, template: Path = CI_TEMPLATE
) -> CiInstall:
    """Commit the shipcrew CI workflow to ``origin/<base>`` unless it is there.

    Idempotent: a base that already has ``.github/workflows/ci.yml`` (ours or
    the repo's own) is left alone.

    :param repo_path: The mission's main checkout (with an ``origin`` remote).
    :param base: The branch PRs target (``SHIPCREW_PR_BASE``).
    :param template: The workflow to install.
    :returns: What happened, see :class:`CiInstall`.
    """
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return CiInstall("skipped", f"{repo} is not a git checkout")
    if _git(["remote", "get-url", "origin"], repo).returncode:
        return CiInstall("skipped", "the repository has no origin remote")
    content = template.read_text(encoding="utf-8")
    last = ""
    for _attempt in range(2):
        listed = _git(
            ["ls-remote", "--exit-code", "--heads", "origin", base], repo, timeout=_PUSH_TIMEOUT_S
        )
        if listed.returncode == 2:  # reachable, but no such branch (an empty repo)
            return CiInstall("skipped", f"origin has no {base} branch yet")
        fetched = _git(["fetch", "--quiet", "origin", base], repo, timeout=_PUSH_TIMEOUT_S)
        if fetched.returncode:
            return CiInstall("failed", f"git fetch origin {base}: {_out(fetched)}")
        head = _git(["rev-parse", "--verify", "--quiet", f"origin/{base}^{{commit}}"], repo)
        parent = head.stdout.strip()
        if head.returncode or not parent:
            return CiInstall("skipped", f"origin has no {base} branch yet")
        if _git(["cat-file", "-e", f"{parent}:{CI_WORKFLOW_PATH}"], repo).returncode == 0:
            return CiInstall("present", f"origin/{base} already has {CI_WORKFLOW_PATH}")
        commit = _commit_with_workflow(repo, parent, content)
        if commit is None:
            return CiInstall("failed", "could not build the CI commit")
        pushed = _git(
            ["push", "--quiet", "origin", f"{commit}:refs/heads/{base}"],
            repo,
            timeout=_PUSH_TIMEOUT_S,
        )
        if pushed.returncode == 0:
            _git(["fetch", "--quiet", "origin", base], repo, timeout=_PUSH_TIMEOUT_S)
            _fast_forward_local_base(repo, base)
            _logger.info("shipcrew: installed %s on %s (%s)", CI_WORKFLOW_PATH, base, commit)
            return CiInstall("installed", f"pushed {CI_COMMIT_MESSAGE} to {base}", commit)
        last = _out(pushed)  # e.g. the base moved meanwhile: fetch and try once more
    return CiInstall("failed", f"git push origin {base}: {last}")
