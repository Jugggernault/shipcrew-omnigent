"""The main checkout's node_modules is refreshed after a merge, so new worktrees seed.

Real git (a local bare ``origin``) and a fake ``pnpm`` on ``PATH`` that writes
``node_modules/.ok`` and logs every run. A "merge" is a commit pushed to
``origin/main`` from a task worktree; :func:`pr_loop._cleanup_worktree` is the
loop's post-merge step that removes the worktree, fast-forwards main and calls
the refresh.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from omnigent.shipcrew import main_deps, pr_loop, tools
from omnigent.shipcrew.deps_seed import seed_node_modules

FAKE_PNPM = """#!/bin/sh
echo "$@" >> "{calls}"
sleep "${{FAKE_PNPM_SLEEP:-0}}"
mkdir -p node_modules && echo ok > node_modules/.ok
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(tools, "CONFIG", tmp_path / "no-tools.json")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "pnpm-calls.log"
    pnpm = bin_dir / "pnpm"
    pnpm.write_text(FAKE_PNPM.format(calls=calls))
    pnpm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.delenv("SHIPCREW_MAIN_DEPS", raising=False)
    return calls


@pytest.fixture
def repo(tmp_path: Path, env: Path) -> Path:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-B", "main")
    (work / ".gitignore").write_text("node_modules\n")
    (work / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "-u", "origin", "main")
    return work


def calls(env: Path) -> list[str]:
    return env.read_text().splitlines() if env.is_file() else []


def merge_task(
    repo: Path, tmp_path: Path, name: str, *, lock: str | None = None, modules: bool = False
) -> Path:
    """A task worktree commits (optionally a new lockfile) and "merges" into origin/main."""
    wt = tmp_path / "wt" / name
    branch = f"shipcrew/{name}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    (wt / f"{name}.txt").write_text(name)
    if lock is not None:
        (wt / "pnpm-lock.yaml").write_text(lock)
    if modules:
        (wt / "node_modules").mkdir()
        (wt / "node_modules" / "pkg.js").write_text(f"// {name}\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", name)
    git(wt, "push", "-q", "origin", "HEAD:main")
    return wt


def cleanup(repo: Path, wt: Path, name: str) -> None:
    pr_loop._cleanup_worktree(repo, wt, f"shipcrew/{name}", "main")
    assert main_deps.wait_idle(repo, 20)


class TestRefreshAfterMerge:
    def test_installs_once_then_stays_fresh(self, tmp_path: Path, repo: Path, env: Path) -> None:
        cleanup(repo, merge_task(repo, tmp_path, "a"), "a")
        assert (repo / "node_modules" / ".ok").is_file()
        assert calls(env) == ["install --frozen-lockfile --prefer-offline"]
        log = (repo / ".git" / "shipcrew" / main_deps.LOG_FILE).read_text()
        assert "exit 0" in log
        # Lockfile unchanged by the next merge: no reinstall.
        cleanup(repo, merge_task(repo, tmp_path, "b"), "b")
        assert len(calls(env)) == 1
        assert main_deps.refresh_after_merge(repo, "main") == "fresh"

    def test_lockfile_change_reinstalls(self, tmp_path: Path, repo: Path, env: Path) -> None:
        cleanup(repo, merge_task(repo, tmp_path, "a"), "a")
        cleanup(repo, merge_task(repo, tmp_path, "b", lock="lockfileVersion: '9.1'\n"), "b")
        assert len(calls(env)) == 2
        assert (repo / "pnpm-lock.yaml").read_text() == "lockfileVersion: '9.1'\n"

    def test_adopts_the_merged_worktree_modules(
        self, tmp_path: Path, repo: Path, env: Path
    ) -> None:
        lock = "lockfileVersion: '9.2'\n"
        cleanup(repo, merge_task(repo, tmp_path, "a", lock=lock, modules=True), "a")
        assert (repo / "node_modules" / "pkg.js").read_text() == "// a\n"
        assert calls(env) == []  # no install: the worktree's matching modules moved over
        assert not list((repo / ".git" / "shipcrew").glob("node_modules.staged-*"))
        # And a new worktree on the same lockfile now seeds from the main checkout.
        wt = tmp_path / "wt" / "next"
        git(repo, "worktree", "add", "-q", "-b", "shipcrew/next", str(wt), "main")
        assert seed_node_modules(str(repo), str(wt)) in ("reflink", "hardlink")
        assert (wt / "node_modules" / "pkg.js").is_file()

    def test_stale_worktree_modules_are_not_adopted(
        self, tmp_path: Path, repo: Path, env: Path
    ) -> None:
        # The worktree's lockfile is not the merged one (main moved on meanwhile).
        wt = merge_task(repo, tmp_path, "a", modules=True)
        (wt / "pnpm-lock.yaml").write_text("something else\n")
        cleanup(repo, wt, "a")
        assert (repo / "node_modules" / ".ok").is_file()
        assert not (repo / "node_modules" / "pkg.js").exists()
        assert len(calls(env)) == 1

    @pytest.mark.parametrize("case", ["dirty", "off-base", "not-ignored", "disabled"])
    def test_skipped(
        self,
        tmp_path: Path,
        repo: Path,
        env: Path,
        case: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if case == "dirty":
            (repo / "pnpm-lock.yaml").write_text("local edit\n")
        elif case == "off-base":
            git(repo, "checkout", "-q", "-b", "other")
        elif case == "not-ignored":
            (repo / ".gitignore").write_text("")
            git(repo, "commit", "-qam", "unignore")
        else:
            monkeypatch.setenv("SHIPCREW_MAIN_DEPS", "0")
        want = "disabled" if case == "disabled" else "skipped"
        assert main_deps.refresh_after_merge(repo, "main") == want
        assert main_deps.wait_idle(repo, 5)
        assert calls(env) == []
        assert not (repo / "node_modules").exists()

    def test_install_is_bounded_and_runs_in_the_background(
        self, repo: Path, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_PNPM_SLEEP", "5")
        monkeypatch.setenv("SHIPCREW_MAIN_DEPS_TIMEOUT_S", "1")
        t0 = time.monotonic()
        assert main_deps.refresh_after_merge(repo, "main") == "installing"
        assert time.monotonic() - t0 < 1.0  # the caller (a scheduler tick) never waits
        assert main_deps.refresh_after_merge(repo, "main") == "busy"  # one at a time
        assert main_deps.wait_idle(repo, 10)
        assert time.monotonic() - t0 < 4.0
        log = (repo / ".git" / "shipcrew" / main_deps.LOG_FILE).read_text()
        assert "timed out after 1 s" in log
        assert not (repo / ".git" / "shipcrew" / main_deps.STAMP_FILE).exists()
