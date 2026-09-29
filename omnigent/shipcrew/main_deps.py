"""Keep the mission repo's main checkout ``node_modules`` current after merges.

:mod:`omnigent.shipcrew.deps_seed` seeds a new task worktree from the main
checkout's ``node_modules``, but a mission repo is usually a plain clone with
no ``node_modules`` at all, so the seed never fired and every task ran a full
install. After each merge the PR loop fast-forwards a clean local base and then
calls :func:`refresh_after_merge`:

1. **Adopt** (cheap): before the merged task's worktree is removed,
   :func:`stage_worktree_modules` moves its ``node_modules`` aside
   (``os.rename`` into ``.git/shipcrew/``, same filesystem only). When that
   worktree's lockfile is byte-identical to the fast-forwarded main lockfile,
   the staged directory becomes the main checkout's ``node_modules``.
2. **Install** (else): when the main lockfile changed since the last refresh
   (sha256 stamp in ``.git/shipcrew/deps.stamp``) or ``node_modules`` is
   missing, the repo's package manager runs its frozen install ONCE in the main
   checkout, in a background thread (never blocks the scheduler tick), one at a
   time per repo, bounded by ``SHIPCREW_MAIN_DEPS_TIMEOUT_S`` (600 s), output
   in ``.git/shipcrew/deps-install.log``.

Only when the main checkout is on the base branch, has no tracked changes and
ignores ``node_modules``. ``SHIPCREW_MAIN_DEPS=0`` turns it off. Best effort:
any failure leaves things as they were (agents install as before).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from omnigent.shipcrew.deps_seed import LOCKFILES
from omnigent.shipcrew.tools import session_env

_logger = logging.getLogger(__name__)

_FALSEY = {"0", "false", "no", "off"}
_GIT_TIMEOUT_S = 30.0
STAMP_FILE = "deps.stamp"
LOG_FILE = "deps-install.log"
# Frozen installs only: the main checkout's lockfile is never rewritten.
INSTALL_COMMANDS: dict[str, list[str]] = {
    "pnpm-lock.yaml": ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
    "package-lock.json": ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
    "yarn.lock": ["yarn", "install", "--frozen-lockfile"],
    "bun.lock": ["bun", "install", "--frozen-lockfile"],
    "bun.lockb": ["bun", "install", "--frozen-lockfile"],
}

_lock = threading.Lock()
_in_flight: dict[str, threading.Thread] = {}


def enabled() -> bool:
    """``SHIPCREW_MAIN_DEPS`` (default on)."""
    return os.environ.get("SHIPCREW_MAIN_DEPS", "1").strip().lower() not in _FALSEY


def install_timeout_s() -> float:
    """``SHIPCREW_MAIN_DEPS_TIMEOUT_S`` (default 600, at least 1)."""
    try:
        return max(1.0, float(os.environ.get("SHIPCREW_MAIN_DEPS_TIMEOUT_S") or 600.0))
    except ValueError:
        return 600.0


@dataclass(frozen=True)
class Staged:
    """A merged worktree's ``node_modules``, moved aside before the worktree goes.

    :param path: The staged directory under ``.git/shipcrew/``.
    :param lockfile: Lockfile name, e.g. ``"pnpm-lock.yaml"``.
    :param lock_bytes: That worktree's lockfile content.
    """

    path: Path
    lockfile: str
    lock_bytes: bytes


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env={**session_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _state_dir(repo: Path) -> Path | None:
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return None
    state = git_dir / "shipcrew"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _lockfile(root: Path) -> str | None:
    return next((name for name in LOCKFILES if (root / name).is_file()), None)


def _stamp(lockfile: str, data: bytes) -> str:
    return f"{lockfile}:{hashlib.sha256(data).hexdigest()}"


def _discard(path: Path | None) -> None:
    """Remove a (possibly large) directory in the background."""
    if path is None or not path.exists():
        return
    threading.Thread(
        target=shutil.rmtree, args=(path,), kwargs={"ignore_errors": True}, daemon=True
    ).start()


def stage_worktree_modules(repo: Path, worktree: Path | None) -> Staged | None:
    """Move the worktree's ``node_modules`` into ``.git/shipcrew/`` (rename only).

    Called right before the worktree is removed. ``None`` when disabled, the
    worktree has no lockfile or ``node_modules``, or it lives on another
    filesystem (a rename would fail; nothing is copied).
    """
    if not enabled() or worktree is None:
        return None
    try:
        src = worktree / "node_modules"
        lockfile = _lockfile(worktree)
        state = _state_dir(repo)
        if state is None or lockfile is None or not src.is_dir() or src.is_symlink():
            return None
        dest = state / f"node_modules.staged-{os.getpid()}-{time.monotonic_ns()}"
        os.rename(src, dest)
        return Staged(dest, lockfile, (worktree / lockfile).read_bytes())
    except OSError as exc:
        _logger.debug("shipcrew: could not stage node_modules of %s: %s", worktree, exc)
        return None


def _main_ready(repo: Path, base: str) -> bool:
    head = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    status = _git(["status", "--porcelain", "--untracked-files=no"], repo)
    ignored = _git(["check-ignore", "-q", "node_modules"], repo)
    return (
        head is not None
        and head.stdout.strip() == base
        and status is not None
        and status.returncode == 0
        and not status.stdout.strip()
        and ignored is not None
        and ignored.returncode == 0
    )


def _adopt(repo: Path, staged: Staged) -> bool:
    target = repo / "node_modules"
    old: Path | None = None
    try:
        if target.exists() or target.is_symlink():
            old = staged.path.with_name(staged.path.name + ".old")
            os.rename(target, old)
        os.rename(staged.path, target)
    except OSError as exc:
        _logger.debug("shipcrew: could not adopt staged node_modules in %s: %s", repo, exc)
        return False
    for cache in (".cache", ".vite", ".vite-temp", ".vitest"):
        shutil.rmtree(target / cache, ignore_errors=True)
    _discard(old)
    return True


def refresh_after_merge(repo: Path, base: str, staged: Staged | None = None) -> str:
    """Bring ``<repo>/node_modules`` in line with the fast-forwarded lockfile.

    :returns: ``"disabled"``, ``"skipped"`` (not on base / dirty / not
        ignored / no lockfile), ``"fresh"``, ``"adopted"``, ``"installing"``
        (a background install started) or ``"busy"`` (one is running).
    """
    try:
        return _refresh(repo, base, staged)
    except OSError as exc:
        _logger.debug("shipcrew: main deps refresh of %s failed: %s", repo, exc)
        return "skipped"
    finally:
        if staged is not None:
            _discard(staged.path)  # no-op once adopted (moved away)


def _refresh(repo: Path, base: str, staged: Staged | None) -> str:
    if not enabled():
        return "disabled"
    state = _state_dir(repo)
    lockfile = _lockfile(repo)
    if state is None or lockfile is None or not _main_ready(repo, base):
        return "skipped"
    data = (repo / lockfile).read_bytes()
    stamp = _stamp(lockfile, data)
    stamp_path = state / STAMP_FILE
    current = stamp_path.read_text(encoding="utf-8").strip() if stamp_path.is_file() else ""
    if (repo / "node_modules").is_dir() and current == stamp:
        return "fresh"
    if (
        staged is not None
        and staged.lockfile == lockfile
        and staged.lock_bytes == data
        and not _busy(repo)
        and _adopt(repo, staged)
    ):
        stamp_path.write_text(stamp + "\n", encoding="utf-8")
        _logger.info("shipcrew: %s/node_modules adopted from the merged worktree", repo)
        return "adopted"
    return _start_install(repo, lockfile, stamp, state)


def _busy(repo: Path) -> bool:
    with _lock:
        thread = _in_flight.get(str(repo.resolve()))
        return thread is not None and thread.is_alive()


def _start_install(repo: Path, lockfile: str, stamp: str, state: Path) -> str:
    key = str(repo.resolve())
    with _lock:
        running = _in_flight.get(key)
        if running is not None and running.is_alive():
            return "busy"
        thread = threading.Thread(
            target=_install,
            args=(repo, lockfile, stamp, state),
            name=f"shipcrew-main-deps:{repo.name}",
            daemon=True,
        )
        _in_flight[key] = thread
        thread.start()
    return "installing"


def _install(repo: Path, lockfile: str, stamp: str, state: Path) -> None:
    argv = list(INSTALL_COMMANDS[lockfile])
    env = {**session_env(), "CI": "1", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
    exe = shutil.which(argv[0], path=env.get("PATH"))
    log = state / LOG_FILE
    started = time.time()
    with log.open("a", encoding="utf-8") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(argv)} ({stamp})\n")
        out.flush()
        if exe is None:
            out.write(f"{argv[0]} not found on PATH: skipped\n")
            return
        try:
            done = subprocess.run(
                [exe, *argv[1:]],
                cwd=repo,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=install_timeout_s(),
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            out.write(f"timed out after {install_timeout_s():.0f} s\n")
            return
        except OSError as exc:
            out.write(f"failed to run: {exc}\n")
            return
        out.write(f"exit {done.returncode} after {time.time() - started:.1f} s\n")
    if done.returncode == 0:
        (state / STAMP_FILE).write_text(stamp + "\n", encoding="utf-8")
        _logger.info("shipcrew: installed %s/node_modules (%s)", repo, lockfile)


def wait_idle(repo: Path, timeout: float) -> bool:
    """Wait for the repo's background install (tests); ``True`` when none runs."""
    with _lock:
        thread = _in_flight.get(str(repo.resolve()))
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()
