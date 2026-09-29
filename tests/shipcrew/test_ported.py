"""Ported v0.2 pieces (doctor, gh), shipped templates, settings and the PR-loop stub."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnigent.shipcrew import gh, pr_loop, tools
from omnigent.shipcrew.settings import DEFAULT_AGENTS_DIR, ShipcrewSettings
from omnigent.shipcrew.store import Task


@pytest.fixture
def tool_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "shipcrew" / "tools.json"
    monkeypatch.setattr(tools, "CONFIG", config)
    return config


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho ok\n")
    path.chmod(0o755)
    return path


class TestDoctor:
    def test_env_override_wins(
        self, tmp_path: Path, tool_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _exe(tmp_path / "bin" / "my-gh")
        monkeypatch.setenv("SHIPCREW_GH", str(fake))
        gh_tool = next(t for t in tools.registry() if t.key == "GH")
        assert tools.resolve(gh_tool) == str(fake)

    def test_saved_dirs_lead_path(
        self, tmp_path: Path, tool_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chromium = _exe(tmp_path / "opt" / "chromium")
        tools.save("CHROMIUM", str(chromium))
        monkeypatch.delenv("CHROMIUM_PATH", raising=False)
        env = tools.session_env()
        assert env["PATH"].split(":")[0] == str(chromium.parent)
        assert env["CHROMIUM_PATH"] == str(chromium)

    def test_check_reports_missing_scopes(self, tmp_path: Path, tool_config: Path) -> None:
        fake = tmp_path / "gh"
        fake.write_text("#!/bin/sh\necho \"Token scopes: 'repo'\"\n")
        fake.chmod(0o755)
        gh_tool = next(t for t in tools.registry() if t.key == "GH")
        ok, why = tools.check(gh_tool, str(fake))
        assert not ok
        assert "workflow" in why


class TestGh:
    def test_pr_checks_without_ci_is_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", "no checks reported on branch")

        monkeypatch.setattr(gh.subprocess, "run", fake_run)
        assert gh.pr_checks(Path("."), 7) == (True, "no CI configured")


def test_templates_are_shipped() -> None:
    names = {p.name for p in pr_loop.TEMPLATES_DIR.iterdir()}
    assert {"ci.yml", "review.yml"} <= names


async def test_pr_loop_is_a_stub() -> None:
    with pytest.raises(NotImplementedError):
        await pr_loop.advance(Task(id="t", mission_id="m", title="t"))


class TestSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("SHIPCREW_AGENTS_DIR", "SHIPCREW_MAX_PARALLEL", "SHIPCREW_MAX_USD"):
            monkeypatch.delenv(name, raising=False)
        cfg = ShipcrewSettings.from_env()
        assert cfg.agents_dir == Path(DEFAULT_AGENTS_DIR)
        assert cfg.max_parallel == 4

    def test_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHIPCREW_AGENTS_DIR", "/agents")
        monkeypatch.setenv("SHIPCREW_MAX_PARALLEL", "2")
        monkeypatch.setenv("SHIPCREW_MAX_USD", "off")
        monkeypatch.setenv("SHIPCREW_SCHEDULER", "0")
        cfg = ShipcrewSettings.from_env()
        assert (cfg.agents_dir, cfg.max_parallel, cfg.max_usd) == (Path("/agents"), 2, None)
        assert cfg.scheduler_enabled is False
