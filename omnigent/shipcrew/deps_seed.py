"""Seed a fresh task worktree's ``node_modules`` from the mission repo's main checkout.

A new worktree has no ``node_modules``, so every agent starts with a full
install (tens of seconds to minutes). When the worktree's lockfile is
byte-identical to the main checkout's, the main checkout's ``node_modules`` is
exactly what that install would produce, so it is copied instead:

- a reflink copy (``cp -a --reflink=always``) where the filesystem supports it
  (btrfs, XFS, APFS-like): independent files at no copy cost;
- else a hardlink copy (``cp -al``): directories are new, files are shared.
  Symlinks are copied as symlinks (``-a`` implies ``--no-dereference``), so
  pnpm's layout (relative links into ``node_modules/.pnpm``) stays valid.
  Package managers replace package files rather than editing them in place,
  and the few metadata files they do rewrite in place (``.package-lock.json``,
  ``.modules.yaml``, ...) get their own copy, so a later install in the
  worktree never writes through into the main checkout. Build caches under
  ``node_modules/.cache`` / ``.vite`` are dropped, not shared.

Best effort and silent: any mismatch or failure leaves the worktree as it was
(no ``node_modules``) and the agent installs as usual.
"""

from __future__ import annotations

import filecmp
import logging
import shutil
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)

LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb")
# Rewritten in place by the package managers: never shared through a hardlink.
_PRIVATE_FILES = (
    ".package-lock.json",
    ".modules.yaml",
    ".pnpm/lock.yaml",
    ".yarn-integrity",
    ".yarn-state.yml",
)
# Tool caches written in place (babel/eslint/next, vite, vitest): dropped.
_CACHE_DIRS = (".cache", ".vite", ".vite-temp", ".vitest")
_COPY_TIMEOUT_S = 300.0


def _run(args: list[str], cwd: Path) -> bool:
    try:
        done = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=_COPY_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _gitignored(workspace: Path, name: str) -> bool:
    """Whether ``name`` is ignored in the worktree, so a seeded copy is never committed."""
    return _run(["git", "check-ignore", "-q", name], workspace)


def _matching_lockfile(repo: Path, workspace: Path) -> str | None:
    for name in LOCKFILES:
        theirs, ours = repo / name, workspace / name
        if ours.is_file():
            if theirs.is_file() and filecmp.cmp(theirs, ours, shallow=False):
                return name
            return None  # the worktree's own lockfile differs: its deps differ
    return None


def _unshare(dest: Path) -> None:
    for rel in _PRIVATE_FILES:
        path = dest / rel
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)
    for rel in _CACHE_DIRS:
        path = dest / rel
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def seed_node_modules(repo_path: str, workspace: str) -> str | None:
    """Copy ``<repo>/node_modules`` into a new worktree when the lockfiles match.

    :param repo_path: The mission repo's main checkout, e.g. ``"/work/app"``.
    :param workspace: The task worktree, e.g. ``"/work/.worktrees/shipcrew-1a2b"``.
    :returns: ``"reflink"`` or ``"hardlink"`` when seeded, ``None`` when
        skipped (no lockfile match, not gitignored, already present) or failed.
    """
    repo, dest_root = Path(repo_path), Path(workspace)
    src, dest = repo / "node_modules", dest_root / "node_modules"
    try:
        if not src.is_dir() or src.is_symlink() or dest.exists() or dest.is_symlink():
            return None
        if repo.resolve() == dest_root.resolve():
            return None
        lockfile = _matching_lockfile(repo, dest_root)
        if lockfile is None or not _gitignored(dest_root, "node_modules"):
            return None
        for method, flags in (("reflink", ["-a", "--reflink=always"]), ("hardlink", ["-al"])):
            if _run(["cp", *flags, str(src), str(dest)], dest_root):
                _unshare(dest)
                _logger.info(
                    "shipcrew: seeded %s/node_modules (%s copy, %s matches)",
                    workspace,
                    method,
                    lockfile,
                )
                return method
            shutil.rmtree(dest, ignore_errors=True)
    except OSError as exc:
        _logger.debug("shipcrew: node_modules seed of %s skipped: %s", workspace, exc)
        shutil.rmtree(dest, ignore_errors=True)
    return None
