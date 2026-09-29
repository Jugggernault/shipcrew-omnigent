"""The server installs the shipcrew CI workflow on main before the first task branch.

Real git with a local bare ``origin`` (the PR-loop fixtures, fake gh included);
nothing reaches GitHub.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from omnigent.shipcrew import ci_install
from omnigent.shipcrew.ci_install import (
    CI_COMMIT_MESSAGE,
    CI_TEMPLATE,
    CI_WORKFLOW_PATH,
    ensure_ci_workflow,
)
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.settings import ShipcrewSettings

from .test_pr_loop import (  # noqa: F401 - pytest fixtures
    OWNER,
    LoopSessions,
    agents_dir,
    gh_state,
    git,
    repo,
    sessions,
)


@pytest.fixture
def settings(tmp_path: Path, agents_dir: Path) -> ShipcrewSettings:  # noqa: F811
    return ShipcrewSettings(
        agents_dir=agents_dir,
        max_parallel=4,
        max_usd=None,
        scheduler_enabled=False,
        pr_loop_enabled=True,
        install_ci=True,
        db_url=f"sqlite:///{tmp_path / 'shipcrew.db'}",
    )


def _origin(repo: Path) -> Path:  # noqa: F811
    return repo.parent / "origin.git"


def _origin_log(repo: Path) -> list[str]:  # noqa: F811
    return git(_origin(repo), "log", "--format=%s", "main").splitlines()


class TestEnsureCiWorkflow:
    def test_installs_once_and_is_idempotent(self, repo: Path) -> None:  # noqa: F811
        before = git(repo, "rev-parse", "HEAD")
        first = ensure_ci_workflow(repo, "main")
        assert first.status == "installed" and first.commit
        assert _origin_log(repo)[0] == CI_COMMIT_MESSAGE
        shown = git(_origin(repo), "show", f"main:{CI_WORKFLOW_PATH}")
        assert shown == CI_TEMPLATE.read_text().rstrip("\n")
        # Only the workflow was added, on top of the old main.
        assert git(repo, "diff", "--name-only", before, first.commit) == CI_WORKFLOW_PATH
        # The clean local main was fast-forwarded; its index was never touched.
        assert git(repo, "rev-parse", "HEAD") == first.commit
        assert git(repo, "status", "--porcelain") == ""
        second = ensure_ci_workflow(repo, "main")
        assert second.status == "present"
        assert _origin_log(repo).count(CI_COMMIT_MESSAGE) == 1

    def test_skipped_when_the_repo_has_its_own_ci(self, repo: Path) -> None:  # noqa: F811
        wf = repo / CI_WORKFLOW_PATH
        wf.parent.mkdir(parents=True)
        wf.write_text("name: mine\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "own ci")
        git(repo, "push", "-q", "origin", "main")
        assert ensure_ci_workflow(repo, "main").status == "present"
        assert git(_origin(repo), "show", f"main:{CI_WORKFLOW_PATH}") == "name: mine"

    def test_dirty_local_main_is_left_alone(self, repo: Path) -> None:  # noqa: F811
        (repo / "shared.txt").write_text("local edit\n")
        head = git(repo, "rev-parse", "HEAD")
        result = ensure_ci_workflow(repo, "main")
        assert result.status == "installed"
        assert git(repo, "rev-parse", "HEAD") == head  # not fast-forwarded
        assert (repo / "shared.txt").read_text() == "local edit\n"
        assert git(repo, "rev-parse", "origin/main") == result.commit

    def test_builds_on_the_latest_main(self, repo: Path, tmp_path: Path) -> None:  # noqa: F811
        other = tmp_path / "other"
        git(tmp_path, "clone", "-q", str(_origin(repo)), str(other))
        (other / "x.txt").write_text("x\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "someone else")
        git(other, "push", "-q", "origin", "main")
        result = ensure_ci_workflow(repo, "main")  # our origin/main was stale
        assert result.status == "installed"
        assert _origin_log(repo)[:2] == [CI_COMMIT_MESSAGE, "someone else"]

    def test_a_refused_push_is_retried_once(self, repo: Path, tmp_path: Path) -> None:  # noqa: F811
        marker = tmp_path / "refused-once"
        hook = _origin(repo) / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\n[ -f {marker} ] && exit 0\ntouch {marker}\nexit 1\n")
        hook.chmod(0o755)
        assert ensure_ci_workflow(repo, "main").status == "installed"
        assert marker.exists()
        assert _origin_log(repo).count(CI_COMMIT_MESSAGE) == 1

    def test_skips_without_origin_or_base(self, tmp_path: Path) -> None:
        assert ensure_ci_workflow(tmp_path / "nope").status == "skipped"
        lone = tmp_path / "lone"
        subprocess.run(["git", "init", "-q", str(lone)], check=True)
        assert ensure_ci_workflow(lone).status == "skipped"
        bare = tmp_path / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(lone), "remote", "add", "origin", str(bare)], check=True)
        result = ensure_ci_workflow(lone)
        assert result.status == "skipped" and "no main" in result.detail

    def test_refused_push_fails_softly(self, repo: Path) -> None:  # noqa: F811
        hook = _origin(repo) / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n")
        hook.chmod(0o755)
        result = ensure_ci_workflow(repo, "main")
        assert result.status == "failed" and "protected branch" in result.detail
        assert CI_COMMIT_MESSAGE not in _origin_log(repo)


class TestFirstTaskStart:
    async def test_first_start_installs_before_the_branch_is_cut(
        self,
        service: ShipcrewService,
        sessions: LoopSessions,  # noqa: F811
        repo: Path,  # noqa: F811
    ) -> None:
        mission = await service.create_mission("M", str(repo), None, OWNER)
        tasks = [
            await service.create_task(mission.id, title=f"T{i}", acceptance=["ok"])
            for i in range(2)
        ]
        started = await asyncio.gather(*(service.start_task(t.id, OWNER) for t in tasks))
        assert _origin_log(repo).count(CI_COMMIT_MESSAGE) == 1
        for task in started:
            assert task.root_session_id is not None
            wt = sessions.worktrees[task.root_session_id]
            # Both parallel first starts forked from the commit with CI.
            assert (wt / CI_WORKFLOW_PATH).is_file()
        # A later start does not touch git again for CI.
        third = await service.create_task(mission.id, title="T3", acceptance=["ok"])
        calls: list[object] = []
        original = ci_install.ensure_ci_workflow

        def _spy(*args: object, **kwargs: object) -> object:
            calls.append(args)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        import omnigent.shipcrew.service as service_mod

        service_mod.ensure_ci_workflow = _spy
        try:
            await service.start_task(third.id, OWNER)
        finally:
            service_mod.ensure_ci_workflow = original
        assert calls == []

    async def test_disabled_setting_installs_nothing(
        self,
        service: ShipcrewService,
        sessions: LoopSessions,  # noqa: F811
        repo: Path,  # noqa: F811
    ) -> None:
        import dataclasses

        service.settings = dataclasses.replace(service.settings, install_ci=False)
        mission = await service.create_mission("M", str(repo), None, OWNER)
        task = await service.create_task(mission.id, title="T", acceptance=["ok"])
        await service.start_task(task.id, OWNER)
        assert CI_COMMIT_MESSAGE not in _origin_log(repo)
