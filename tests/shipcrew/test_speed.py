"""Speed hacks: parallel starts, the worktree lock, node_modules seeding,
cheaper reviews and the CI template."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.server.routes import _host_worktree
from omnigent.shipcrew import deps_seed, pr_loop
from omnigent.shipcrew.deps_seed import seed_node_modules
from omnigent.shipcrew.review_policy import (
    changed_lines,
    is_doc_path,
    review_effort,
    review_skip_reason,
)
from omnigent.shipcrew.scheduler import ShipcrewScheduler
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import OmnigentSessionService, RootSessionRequest

from .conftest import FakeSessions

P = "/v1/shipcrew"
_MISSION = {"title": "M", "repo_path": "/r"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)


# ── parallel starts ─────────────────────────────────────────────


class _SlowSessions(FakeSessions):
    """Each start takes ``delay`` seconds; records the peak of concurrent starts."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def create_root_session(self, request: RootSessionRequest) -> str:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().create_root_session(request)
        finally:
            self.active -= 1


@pytest.fixture
def sessions() -> _SlowSessions:
    return _SlowSessions(delay=0.3)


async def _ready(client: httpx.AsyncClient, mission_id: str, **kw: Any) -> str:
    r = await client.post(f"{P}/missions/{mission_id}/tasks", json={"title": "t", **kw})
    await client.patch(f"{P}/tasks/{r.json()['id']}", json={"status": "ready"})
    return str(r.json()["id"])


class TestParallelStarts:
    async def test_ready_cards_start_concurrently_within_capacity(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: _SlowSessions
    ) -> None:
        mission = (await client.post(f"{P}/missions", json=_MISSION)).json()
        ids = [await _ready(client, mission["id"], owned_paths=[f"p{i}/**"]) for i in range(3)]
        began = time.monotonic()
        started = await ShipcrewScheduler(lambda: service, 60).tick()
        elapsed = time.monotonic() - began
        # max_parallel=2 (test settings): two starts, side by side, not one after the other.
        assert started == ids[:2]
        assert sessions.peak == 2
        assert elapsed < 0.3 * 2
        third = await service.require_task(ids[2])
        assert (third.status, third.blocked_reason) == ("ready", "capacity: 2/2 agents running")

    async def test_a_card_picked_this_tick_holds_its_paths(
        self, client: httpx.AsyncClient, service: ShipcrewService, sessions: _SlowSessions
    ) -> None:
        mission = (await client.post(f"{P}/missions", json=_MISSION)).json()
        first = await _ready(client, mission["id"], owned_paths=["src/**"])
        second = await _ready(client, mission["id"], owned_paths=["src/app.ts"])
        assert await ShipcrewScheduler(lambda: service, 60).tick() == [first]
        assert "overlap" in ((await service.require_task(second)).blocked_reason or "")
        assert sessions.peak == 1


# ── worktree lock ───────────────────────────────────────────────


class _Registry:
    def online_host_ids(self) -> list[str]:
        return ["host_a"]

    def get(self, host_id: str) -> object:
        return object()


def _session_service() -> OmnigentSessionService:
    from fastapi import FastAPI

    app = FastAPI()
    app.state.host_registry = _Registry()
    app.state.host_store = None
    return OmnigentSessionService(app, auth_provider=None)


def _request(repo: Path, n: int) -> RootSessionRequest:
    return RootSessionRequest(
        task_id=f"t{n}",
        title=f"t{n}",
        prompt="x",
        repo_path=str(repo),
        branch=f"shipcrew/0000000{n}-t",
        agent_dir=repo,
        base_branch="main",
    )


class TestWorktreeLock:
    async def test_worktree_adds_on_one_repo_never_overlap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_env: None
    ) -> None:
        # The host's `git worktree add`, run for real in a thread (as the host
        # tunnel does it out of process): 8 parallel starts on one repo.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        state = {"active": 0, "peak": 0}

        async def listed(**kw: Any) -> list[dict[str, Any]]:
            return []

        async def create(**kw: Any) -> _host_worktree.CreatedWorktree:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                path = tmp_path / "wt" / kw["branch_name"].replace("/", "-")
                await asyncio.to_thread(
                    _git, repo, "worktree", "add", "-q", "-b", kw["branch_name"], str(path), "main"
                )
                return _host_worktree.CreatedWorktree(
                    worktree_path=str(path), branch=kw["branch_name"], workspace=str(path)
                )
            finally:
                state["active"] -= 1

        monkeypatch.setattr(_host_worktree, "list_worktrees_on_host", listed)
        monkeypatch.setattr(_host_worktree, "create_worktree_on_host", create)
        service = _session_service()
        paths = await asyncio.gather(
            *(service._task_worktree(object(), _request(repo, n)) for n in range(8))
        )
        assert state["peak"] == 1
        assert len(set(paths)) == 8 and all(Path(p).is_dir() for p in paths)
        assert _git(repo, "worktree", "list").count("\n") == 8

    async def test_different_repos_do_not_wait_for_each_other(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = {"active": 0, "peak": 0}

        async def listed(**kw: Any) -> list[dict[str, Any]]:
            return []

        async def create(**kw: Any) -> _host_worktree.CreatedWorktree:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1
            return _host_worktree.CreatedWorktree(
                worktree_path="/wt", branch=kw["branch_name"], workspace="/wt"
            )

        monkeypatch.setattr(_host_worktree, "list_worktrees_on_host", listed)
        monkeypatch.setattr(_host_worktree, "create_worktree_on_host", create)
        service = _session_service()
        await asyncio.gather(
            *(service._task_worktree(object(), _request(tmp_path / f"r{n}", n)) for n in range(3))
        )
        assert state["peak"] == 3


# ── node_modules seeding ────────────────────────────────────────


def _checkout(tmp_path: Path, *, lock: str = "pnpm-lock.yaml") -> tuple[Path, Path]:
    """A main checkout with an installed pnpm-style node_modules, and a fresh worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("node_modules\n")
    (repo / lock).write_text("lockfileVersion: '9.0'\n")
    (repo / "package.json").write_text('{"name": "app"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    store = repo / "node_modules" / ".pnpm" / "left-pad@1.3.0" / "node_modules" / "left-pad"
    store.mkdir(parents=True)
    (store / "index.js").write_text("module.exports = 1\n")
    (repo / "node_modules" / "left-pad").symlink_to(".pnpm/left-pad@1.3.0/node_modules/left-pad")
    (repo / "node_modules" / ".bin").mkdir()
    (repo / "node_modules" / ".bin" / "left-pad").symlink_to("../left-pad/index.js")
    (repo / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n")
    (repo / "node_modules" / ".cache").mkdir()
    (repo / "node_modules" / ".cache" / "eslint").write_text("cache\n")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "shipcrew/00000001-t", str(wt), "main")
    return repo, wt


class TestSeedNodeModules:
    def test_hardlink_copy_keeps_pnpm_symlinks(self, tmp_path: Path, git_env: None) -> None:
        repo, wt = _checkout(tmp_path)
        assert seed_node_modules(str(repo), str(wt)) in ("reflink", "hardlink")
        nm = wt / "node_modules"
        # Relative symlinks stay symlinks and resolve inside the worktree's copy.
        assert (nm / "left-pad").is_symlink()
        assert (nm / "left-pad" / "index.js").read_text() == "module.exports = 1\n"
        assert (nm / "left-pad").resolve().is_relative_to(nm.resolve())
        assert (nm / ".bin" / "left-pad").is_symlink()
        # Metadata a package manager rewrites in place is private, caches are dropped.
        assert (nm / ".modules.yaml").read_text() == "layoutVersion: 5\n"
        mod_src, mod_dst = repo / "node_modules" / ".modules.yaml", nm / ".modules.yaml"
        assert mod_src.stat().st_ino != mod_dst.stat().st_ino
        (mod_dst).write_text("changed\n")
        assert mod_src.read_text() == "layoutVersion: 5\n"
        assert not (nm / ".cache").exists()
        assert (repo / "node_modules" / ".cache" / "eslint").exists()
        # Still gitignored: nothing to commit.
        assert _git(wt, "status", "--porcelain") == ""

    def test_hardlinks_when_reflinks_are_unsupported(
        self, tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, wt = _checkout(tmp_path)
        real = deps_seed._run

        def no_reflink(args: list[str], cwd: Path) -> bool:
            if "--reflink=always" in args:
                (Path(args[-1]) / "partial").mkdir(parents=True)  # a failed copy's leftovers
                return False
            return real(args, cwd)

        monkeypatch.setattr(deps_seed, "_run", no_reflink)
        assert seed_node_modules(str(repo), str(wt)) == "hardlink"
        src = repo / "node_modules" / ".pnpm" / "left-pad@1.3.0" / "node_modules" / "left-pad"
        dst = wt / "node_modules" / ".pnpm" / "left-pad@1.3.0" / "node_modules" / "left-pad"
        assert (src / "index.js").stat().st_ino == (dst / "index.js").stat().st_ino
        assert not (wt / "node_modules" / "partial").exists()

    @pytest.mark.parametrize("case", ["lock differs", "no lockfile", "not ignored", "present"])
    def test_skipped_silently(self, tmp_path: Path, git_env: None, case: str) -> None:
        repo, wt = _checkout(tmp_path)
        if case == "lock differs":
            (wt / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nother: 1\n")
        elif case == "no lockfile":
            (wt / "pnpm-lock.yaml").unlink()
        elif case == "not ignored":
            (wt / ".gitignore").write_text("")
        else:
            (wt / "node_modules").mkdir()
        assert seed_node_modules(str(repo), str(wt)) is None
        if case != "present":
            assert not (wt / "node_modules").exists()

    def test_failures_leave_nothing_behind(
        self, tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, wt = _checkout(tmp_path)
        real = deps_seed._run

        def cp_fails(args: list[str], cwd: Path) -> bool:
            if args[0] == "cp":
                (Path(args[-1]) / "partial").mkdir(parents=True, exist_ok=True)
                return False
            return real(args, cwd)

        monkeypatch.setattr(deps_seed, "_run", cp_fails)
        assert seed_node_modules(str(repo), str(wt)) is None
        assert not (wt / "node_modules").exists()
        assert seed_node_modules("/nonexistent", str(wt)) is None

    async def test_root_session_start_seeds_the_new_worktree(
        self, tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnigent.shipcrew import sessions as sessions_mod

        repo, wt = _checkout(tmp_path)
        seeded: list[tuple[str, str]] = []
        monkeypatch.setattr(sessions_mod, "prepare_worktree", lambda r, w: seeded.append((r, w)))

        async def fake_worktree(conn: object, request: RootSessionRequest) -> str:
            return str(wt)

        service = _session_service()
        monkeypatch.setattr(service, "_task_worktree", fake_worktree)
        monkeypatch.setattr(sessions_mod, "bundle_agent_dir", lambda *a, **k: b"")
        monkeypatch.setattr(sessions_mod, "_pretrust_claude_workspace", lambda w: None)
        with pytest.raises(Exception):  # noqa: B017 - the stub app has no /v1/sessions
            await service.create_root_session(_request(repo, 1))
        assert seeded == [(str(repo), str(wt))]


# ── reviewer policy ─────────────────────────────────────────────


class TestReviewPolicy:
    @pytest.mark.parametrize(
        ("changed", "reason"),
        [
            (["tests/a.test.ts", "e2e/cart.spec.ts"], "tests-only diff with CI green"),
            (["README.md", "docs/setup.md"], "docs-only diff with CI green"),
            (["README.md", "src/a.ts"], None),
            (["tests/a.test.ts", "README.md"], None),  # mixed: reviewed
            (["AGENTS.md"], None),  # agent instructions are not docs
            (["DESIGN.md"], None),
            ([".shipcrew/prd.md"], None),
            (["content/post.mdx"], None),
            (["requirements.txt"], None),
            ([], None),
        ],
    )
    def test_skip_reason(self, changed: list[str], reason: str | None) -> None:
        assert review_skip_reason(changed) == reason

    def test_skip_can_be_turned_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHIPCREW_REVIEW_SKIP", "0")
        assert review_skip_reason(["tests/a.test.ts"]) is None

    def test_changed_lines_and_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert changed_lines("10\t2\tsrc/a.ts\n-\t-\tlogo.png\n3\t0\tb.ts\n") == 15
        assert review_effort(15) == "low"
        assert review_effort(120) == "medium"
        assert review_effort(150) is None
        monkeypatch.setenv("SHIPCREW_REVIEW_EFFORT_TINY", "default")
        assert review_effort(5) is None

    def test_doc_paths(self) -> None:
        assert is_doc_path("docs/guide.md") and not is_doc_path(".claude/skills/x/SKILL.md")


# ── CI template ─────────────────────────────────────────────────


class TestCiTemplate:
    def _workflow(self) -> dict[str, Any]:
        return yaml.safe_load((pr_loop.TEMPLATES_DIR / "ci.yml").read_text())

    def test_cancels_superseded_runs_and_caches_installs(self) -> None:
        wf = self._workflow()
        assert wf["concurrency"]["cancel-in-progress"] is True
        steps = wf["jobs"]["ci"]["steps"]
        setup_node = next(s for s in steps if s.get("uses", "").startswith("actions/setup-node"))
        assert setup_node["with"]["cache"] == "${{ steps.pm.outputs.cache }}"
        pm = next(s for s in steps if s.get("id") == "pm")["run"]
        assert "pnpm install --frozen-lockfile --prefer-offline" in pm
        assert "npm ci --prefer-offline --no-audit --no-fund" in pm
        assert "yarn install --frozen-lockfile" in pm
        assert any(s.get("uses", "").startswith("pnpm/action-setup") for s in steps)
        env = wf["jobs"]["ci"]["env"]
        assert env["CHROMIUM_PATH"] == "/usr/bin/google-chrome"
        assert "playwright install" not in yaml.safe_dump(wf)

    @pytest.mark.parametrize("where", ["path", "cache", "none"])
    def test_prefers_the_headless_shell_when_present(self, tmp_path: Path, where: str) -> None:
        steps = self._workflow()["jobs"]["ci"]["steps"]
        script = next(s for s in steps if s.get("name") == "headless browser")["run"]
        bindir, cache = tmp_path / "bin", tmp_path / "ms-playwright"
        bindir.mkdir()
        shell = None
        if where == "path":
            shell = bindir / "chrome-headless-shell"
        elif where == "cache":
            (cache / "chromium_headless_shell-1200/chrome-headless-shell-linux64").mkdir(
                parents=True
            )
            shell = cache / "chromium_headless_shell-1234/chrome-headless-shell-linux64"
            shell.mkdir(parents=True)
            shell = shell / "chrome-headless-shell"
            (cache / "chromium_headless_shell-1200/chrome-headless-shell-linux64"
             / "chrome-headless-shell").write_text("#!/bin/sh\n")  # fmt: skip
        if shell is not None:
            shell.write_text("#!/bin/sh\n")
            shell.chmod(0o755)
        out = tmp_path / "env"
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "GITHUB_ENV": str(out),
            "PLAYWRIGHT_BROWSERS_PATH": str(cache),
            "CHROMIUM_PATH": "/usr/bin/google-chrome",
        }
        subprocess.run(["bash", "-e", "-c", script], env=env, check=True, timeout=30)
        written = out.read_text() if out.exists() else ""
        assert written == (f"CHROMIUM_PATH={shell}\n" if shell else "")

    @pytest.mark.parametrize(
        ("files", "outputs"),
        [
            ({}, {"name": ""}),  # before the Foundation task: nothing to run
            ({"package.json": "{}", "pnpm-lock.yaml": ""}, {"name": "pnpm", "pnpm": "10"}),
            (
                {"package.json": '{"packageManager": "pnpm@9.1.0"}', "pnpm-lock.yaml": ""},
                {"name": "pnpm", "pnpm": ""},
            ),
            ({"package.json": "{}", "yarn.lock": ""}, {"name": "yarn", "cache": "yarn"}),
            ({"package.json": "{}", "package-lock.json": "{}"}, {"name": "npm", "cache": "npm"}),
            ({"package.json": "{}"}, {"name": "npm", "cache": ""}),
        ],
    )
    def test_package_manager_from_the_lockfile(
        self, tmp_path: Path, files: dict[str, str], outputs: dict[str, str]
    ) -> None:
        steps = self._workflow()["jobs"]["ci"]["steps"]
        script = next(s for s in steps if s.get("id") == "pm")["run"]
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        out = tmp_path / "out"
        env = {**os.environ, "GITHUB_OUTPUT": str(out)}
        subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env, check=True, timeout=30)
        got = dict(line.split("=", 1) for line in out.read_text().splitlines())
        assert {k: got[k] for k in outputs} == outputs

    def test_parallel_checks_step_runs_all_three_and_fails_on_any(self, tmp_path: Path) -> None:
        steps = self._workflow()["jobs"]["ci"]["steps"]
        script = next(s for s in steps if s.get("name", "").startswith("lint + typecheck"))["run"]
        fake_pm = tmp_path / "pm"
        # `pm run <script>`: every script sleeps 0.5 s; typecheck fails.
        fake_pm.write_text('#!/bin/sh\nsleep 0.5\necho "ran $2"\n[ "$2" != typecheck ]\n')
        fake_pm.chmod(0o755)
        (tmp_path / "package.json").write_text(
            '{"scripts": {"lint": "x", "typecheck": "x", "test": "x"}}'
        )
        env = {**os.environ, "PM": str(fake_pm), "RUNNER_TEMP": str(tmp_path)}
        began = time.monotonic()
        done = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=30,
        )  # fmt: skip
        elapsed = time.monotonic() - began
        assert done.returncode == 1
        assert "::error::typecheck failed" in done.stdout
        assert all(f"ran {s}" in done.stdout for s in ("lint", "typecheck", "test"))
        assert elapsed < 1.4  # three 0.5 s scripts side by side, not 1.5 s in a row

    def test_missing_scripts_are_skipped(self, tmp_path: Path) -> None:
        steps = self._workflow()["jobs"]["ci"]["steps"]
        script = next(s for s in steps if s.get("name", "").startswith("lint + typecheck"))["run"]
        fake_pm = tmp_path / "pm"
        fake_pm.write_text('#!/bin/sh\necho "ran $2"\n')
        fake_pm.chmod(0o755)
        (tmp_path / "package.json").write_text('{"scripts": {"test": "x"}}')
        env = {**os.environ, "PM": str(fake_pm), "RUNNER_TEMP": str(tmp_path)}
        done = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=30,
        )  # fmt: skip
        assert done.returncode == 0
        assert "ran test" in done.stdout and "ran lint" not in done.stdout
        assert 'no "lint" script: skipped' in done.stdout
