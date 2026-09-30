"""The docker deploy target with fake docker / cloudflared / caddy executables.

The fakes are real executables resolved through ``SHIPCREW_DOCKER`` /
``SHIPCREW_CLOUDFLARED`` / ``SHIPCREW_CADDY``; the fake containers are real
HTTP servers, so the health check, the forwarder and the swap run for real.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.shipcrew.deploy_targets import (
    DeployContext,
    DeployError,
    make_target,
    mission_slug,
    select_target,
)
from omnigent.shipcrew.deploy_targets.docker import DockerTarget
from omnigent.shipcrew.deploy_targets.expose import (
    LOCAL_TUNNEL_URL,
    TUNNEL_URL,
    Supervised,
    TunnelExposure,
)
from omnigent.shipcrew.preview import _add_worktree, _remove_worktree
from omnigent.shipcrew.settings import ShipcrewSettings
from omnigent.shipcrew.store import Mission

from .deploy_fakes import DeployFakes

MISSION = Mission(
    id="abc123def456",
    title="Polls",
    repo_path="/nowhere",
    repo_url="https://github.com/acme/Polls_Web.git",
    status="active",
    created_at=0,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DeployFakes]:
    f = DeployFakes(tmp_path / "fakes", monkeypatch)
    yield f
    f.kill_all(tmp_path / "state")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    commit(path, {"Dockerfile": "FROM scratch\n", "package.json": "{}\n"}, "init")
    return path


def _settings(tmp_path: Path, **fields: Any) -> ShipcrewSettings:
    values: dict[str, Any] = {
        "deploy_target": "docker",
        "deploy_state_dir": tmp_path / "state",
        "deploy_health_timeout_s": 5.0,
        "tunnel_url_timeout_s": 5.0,
        "docker_build": "dockerfile",  # host builds: test_deploy_host_build.py
        **fields,
    }
    return ShipcrewSettings(**values)


@pytest.fixture
def target(tmp_path: Path, fakes: DeployFakes) -> DockerTarget:
    return DockerTarget(_settings(tmp_path))


def deploy(target: DockerTarget, repo: Path, mission: Mission = MISSION) -> Any:
    sha = _git(repo, "rev-parse", "HEAD")
    worktree = target.state_dir / "worktrees" / sha[:12]
    _add_worktree(repo, worktree, sha)
    try:
        return target.deploy(
            DeployContext(
                mission_id=mission.id,
                slug=mission_slug(mission),
                title=mission.title,
                repo_path=repo,
                repo_url=mission.repo_url,
                sha=sha,
                worktree=worktree,
            )
        )
    finally:
        _remove_worktree(repo, worktree)


def front_get(target: DockerTarget, path: str = "/") -> httpx.Response:
    port = target.exposure.front_port(mission_slug(MISSION))  # type: ignore[attr-defined]
    return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=5)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


class TestSlugAndSelection:
    def test_mission_slug_is_one_stable_dns_label(self) -> None:
        assert mission_slug(MISSION) == "polls-web-abc123"
        odd = Mission(
            id="ZZ9", title="x", repo_path="/w/My App!!", repo_url=None, status="a", created_at=0
        )
        assert mission_slug(odd) == "my-app-0009" or mission_slug(odd).startswith("my-app-")

    def test_explicit_and_auto_selection(
        self, tmp_path: Path, fakes: DeployFakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert make_target("vercel", _settings(tmp_path)).server_side is False
        assert make_target("docker", _settings(tmp_path)).server_side is True
        auto = ShipcrewSettings(deploy_target="auto", deploy_state_dir=tmp_path / "state")
        assert select_target(auto).name == "docker"  # the fake daemon answers
        # Docker down and no vercel CLI: still docker, whose preflight says why.
        fakes.set_down()
        monkeypatch.setenv("SHIPCREW_VERCEL", str(tmp_path / "missing"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("omnigent.shipcrew.tools.KNOWN_DIRS", [])
        chosen = select_target(auto)
        assert chosen.name == "docker"
        problem = chosen.preflight()
        assert problem is not None and "daemon is not reachable" in problem
        with pytest.raises(ValueError, match="unknown deploy target"):
            make_target("heroku", auto)

    def test_preflight_names_a_missing_cloudflared(
        self, target: DockerTarget, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert target.preflight() is None
        monkeypatch.setenv("SHIPCREW_CLOUDFLARED", str(tmp_path / "nope"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("omnigent.shipcrew.tools.KNOWN_DIRS", [])
        problem = target.preflight()
        assert problem is not None and problem.startswith("cloudflared not found")


class TestDeployAndSwap:
    def test_first_deploy_builds_runs_and_tunnels(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        result = deploy(target, repo)
        sha = _git(repo, "rev-parse", "HEAD")
        assert result.url == "https://fake-1.trycloudflare.com"
        assert result.detail["image"] == f"shipcrew/polls-web-abc123:{sha[:12]}"
        assert result.detail["image_mb"] == 123.5
        (build,) = fakes.calls("build")
        assert "--file" not in build  # the repo's own Dockerfile
        assert f"shipcrew.sha={sha}" in build
        (container,) = fakes.world()["containers"].items()
        name, info = container
        assert name == "shipcrew-polls-web-abc123"
        assert (info["restart"], info["memory"], info["cpus"]) == ("unless-stopped", "384m", "1")
        # The stable front port reaches the container through the forwarder.
        r = front_get(target, "/api/polls")
        assert r.status_code == 200 and r.text.endswith("/api/polls")
        tunnel = json.loads((target.state_dir / "polls-web-abc123" / "tunnel.json").read_text())
        assert tunnel["url"] == result.url
        assert (
            tunnel["argv"][-1]
            == f"http://127.0.0.1:{target.exposure.front_port('polls-web-abc123')}"
        )  # type: ignore[attr-defined]

    def test_redeploy_swaps_the_container_and_keeps_the_url(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        first = deploy(target, repo)
        old_pid = fakes.world()["containers"]["shipcrew-polls-web-abc123"]["pid"]
        new_sha = commit(repo, {"page.txt": "v2"})
        second = deploy(target, repo)
        assert second.url == first.url  # same tunnel, same URL
        containers = fakes.world()["containers"]
        assert list(containers) == ["shipcrew-polls-web-abc123"]
        assert containers["shipcrew-polls-web-abc123"]["labels"]["shipcrew.sha"] == new_sha
        assert not _pid_alive(old_pid)
        assert new_sha[:12] in front_get(target).text
        assert len(fakes.calls("build")) == 2
        assert int(fakes.counter.read_text()) == 1  # one tunnel for both deploys

    def test_same_commit_running_skips_the_build(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        deploy(target, repo)
        again = deploy(target, repo)
        assert again.detail.get("skipped_build") is True
        assert len(fakes.calls("build")) == 1

    def test_unhealthy_new_version_keeps_the_old_container(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        target.settings = _settings(target.state_dir.parent, deploy_health_timeout_s=1.0)
        first = deploy(target, repo)
        old_sha = _git(repo, "rev-parse", "HEAD")
        commit(repo, {"fake-health": "500"})
        with pytest.raises(DeployError) as info:
            deploy(target, repo)
        assert info.value.kept_previous
        assert "keeps serving" in str(info.value) and "HTTP 500" in str(info.value)
        containers = fakes.world()["containers"]
        assert list(containers) == ["shipcrew-polls-web-abc123"]  # candidate removed
        assert containers["shipcrew-polls-web-abc123"]["labels"]["shipcrew.sha"] == old_sha
        assert old_sha[:12] in front_get(target).text  # still the old version
        assert target.exposure.current("polls-web-abc123") == first.url

    def test_build_failure_keeps_the_old_container(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        deploy(target, repo)
        commit(repo, {"fake-build-fail": "1"})
        with pytest.raises(DeployError, match="docker build failed") as info:
            deploy(target, repo)
        assert info.value.kept_previous
        assert "npm ERR! build failed" in str(info.value)
        assert len(fakes.world()["containers"]) == 1

    def test_prune_keeps_two_images(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        for n in range(4):
            commit(repo, {"v.txt": str(n)})
            deploy(target, repo)
        tags = [k for k in fakes.world()["images"] if k.startswith("shipcrew/polls-web-abc123:")]
        assert len(tags) == 2
        head = _git(repo, "rev-parse", "HEAD")[:12]
        assert f"shipcrew/polls-web-abc123:{head}" in tags

    def test_repo_without_dockerfile_uses_the_generic_node_image(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        _git(repo, "rm", "-q", "Dockerfile")
        commit(repo, {})
        result = deploy(target, repo)
        (build,) = fakes.calls("build")
        assert build[build.index("--file") + 1].endswith(".shipcrew.Dockerfile")
        assert result.note is not None and "generic Node" in result.note

    def test_build_network_setting(self, tmp_path: Path, repo: Path, fakes: DeployFakes) -> None:
        target = DockerTarget(_settings(tmp_path, docker_build_network="host"))
        deploy(target, repo)
        (build,) = fakes.calls("build")
        assert build[build.index("--network") + 1] == "host"


class TestTunnelSupervision:
    def test_dead_tunnel_is_restarted_and_the_new_url_returned(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        first = deploy(target, repo)
        assert target.refresh(MISSION) == first.url  # alive: nothing restarted
        assert int(fakes.counter.read_text()) == 1
        pid = json.loads((target.state_dir / "polls-web-abc123" / "tunnel.json").read_text())[
            "pid"
        ]
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert target.exposure.current("polls-web-abc123") is None
        assert target.refresh(MISSION) == "https://fake-2.trycloudflare.com"
        assert front_get(target).status_code == 200

    def test_a_restarted_server_adopts_the_running_tunnel(
        self, tmp_path: Path, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        first = deploy(target, repo)
        Supervised._children.clear()  # a new server process has no child handles
        fresh = DockerTarget(_settings(tmp_path))
        assert fresh.refresh(MISSION) == first.url
        assert int(fakes.counter.read_text()) == 1  # no new tunnel

    def test_dead_forwarder_is_restarted_behind_the_same_url(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        first = deploy(target, repo)
        state = target.state_dir / "polls-web-abc123" / "forwarder.json"
        os.killpg(json.loads(state.read_text())["pid"], signal.SIGKILL)
        time.sleep(0.2)
        url = target.refresh(MISSION)
        assert url is not None
        assert front_get(target).status_code == 200
        # Same front port free again: the tunnel keeps its URL.
        assert url == first.url or url == "https://fake-2.trycloudflare.com"

    def test_refresh_before_any_deploy_is_none(self, target: DockerTarget) -> None:
        assert target.refresh(MISSION) is None

    def test_tunnel_without_url_fails_the_deploy_but_keeps_the_app(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CLOUDFLARED_FAIL", "1")
        with pytest.raises(DeployError, match="no public URL") as info:
            deploy(target, repo)
        assert "failed to request quick Tunnel" in str(info.value)
        assert len(fakes.world()["containers"]) == 1


class TestCaddyMode:
    def test_vps_mode_routes_slug_subdomain_through_caddy(
        self, tmp_path: Path, repo: Path, fakes: DeployFakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sites = tmp_path / "sites"
        monkeypatch.setenv("FAKE_CADDY_SITES", str(sites))
        target = DockerTarget(
            _settings(
                tmp_path,
                public_base_domain="203.0.113.7.sslip.io",
                caddy_sites_dir=str(sites),
                caddyfile=str(tmp_path / "Caddyfile"),
            )
        )
        assert target.exposure.mode == "caddy"
        assert target.preflight() is None  # no cloudflared needed
        result = deploy(target, repo)
        assert result.url == "https://polls-web-abc123.203.0.113.7.sslip.io"
        port = fakes.world()["containers"]["shipcrew-polls-web-abc123"]["port"]
        reloads = fakes.caddy_reloads()
        assert reloads[-1]["argv"] == [
            "reload", "--config", str(tmp_path / "Caddyfile"), "--adapter", "caddyfile",
        ]  # fmt: skip
        snippet = reloads[-1]["sites"]["polls-web-abc123.caddy"]
        assert "polls-web-abc123.203.0.113.7.sslip.io {" in snippet
        assert f"reverse_proxy 127.0.0.1:{port}" in snippet
        assert fakes.counter.exists() is False  # no tunnel in VPS mode
        commit(repo, {"v": "2"})
        deploy(target, repo)
        new_port = fakes.world()["containers"]["shipcrew-polls-web-abc123"]["port"]
        assert new_port != port
        assert (
            f"127.0.0.1:{new_port}" in fakes.caddy_reloads()[-1]["sites"]["polls-web-abc123.caddy"]
        )
        target.teardown(MISSION)
        assert fakes.caddy_reloads()[-1]["sites"] == {}

    def test_domain_without_caddy_falls_back_to_the_tunnel(
        self, tmp_path: Path, fakes: DeployFakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHIPCREW_CADDY", str(tmp_path / "no-caddy"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("omnigent.shipcrew.tools.KNOWN_DIRS", [])
        target = DockerTarget(_settings(tmp_path, public_base_domain="x.sslip.io"))
        assert target.exposure.mode == "tunnel"


class TestTeardown:
    def test_teardown_stops_everything(
        self, target: DockerTarget, repo: Path, fakes: DeployFakes
    ) -> None:
        deploy(target, repo)
        slug_dir = target.state_dir / "polls-web-abc123"
        pids = [
            json.loads((slug_dir / name).read_text())["pid"]
            for name in ("tunnel.json", "forwarder.json")
        ]
        container_pid = fakes.world()["containers"]["shipcrew-polls-web-abc123"]["pid"]
        target.teardown(MISSION)
        time.sleep(0.2)
        assert fakes.world()["containers"] == {}
        assert not any(k.startswith("shipcrew/polls-web-abc123") for k in fakes.world()["images"])
        assert not any(_pid_alive(p) for p in [*pids, container_pid])
        assert not slug_dir.exists()
        assert target.refresh(MISSION) is None
        # Untagged leftovers of the mission's builds go too (by label).
        prune = [c for c in fakes.calls("image") if c[1] == "prune"]
        assert prune and prune[-1][-1] == f"label=shipcrew.mission={MISSION.id}"


class TestTools:
    def test_cloudflared_and_caddy_are_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.shipcrew import tools

        monkeypatch.setenv("SHIPCREW_DEPLOY_TARGET", "docker")
        by_key = {t.key: t for t in tools.registry()}
        assert by_key["CLOUDFLARED"].install == "cloudflared"
        assert by_key["CLOUDFLARED"].required and by_key["DOCKER"].required
        assert not by_key["VERCEL"].required  # only for SHIPCREW_DEPLOY_TARGET=vercel
        assert not by_key["CADDY"].required
        monkeypatch.setenv("SHIPCREW_PUBLIC_BASE_DOMAIN", "203.0.113.7.sslip.io")
        assert not {t.key: t for t in tools.registry()}["CLOUDFLARED"].required
        monkeypatch.setenv("SHIPCREW_DEPLOY_TARGET", "vercel")
        assert {t.key: t for t in tools.registry()}["VERCEL"].required

    def test_doctor_fix_downloads_the_static_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnigent.shipcrew import tools

        fetched: list[str] = []

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"\x7fELF fake binary"

        def urlopen(url: str, timeout: float) -> _Response:
            fetched.append(url)
            return _Response()

        monkeypatch.setattr(tools, "BIN_DIR", tmp_path / "bin")
        monkeypatch.setattr(tools, "CONFIG", tmp_path / "tools.json")
        monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(tools.platform, "machine", lambda: "x86_64")
        tool = next(t for t in tools.registry() if t.key == "CLOUDFLARED")
        assert tools.install(tool)
        assert fetched == [
            "https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-linux-amd64"
        ]
        dest = tmp_path / "bin" / "cloudflared"
        assert dest.read_bytes().startswith(b"\x7fELF") and os.access(dest, os.X_OK)
        assert json.loads((tmp_path / "tools.json").read_text())["CLOUDFLARED"] == str(dest)


def test_a_loopback_tunnel_url_only_counts_in_local_e2e_mode(tmp_path: Path) -> None:
    log = "INF |  http://127.0.0.1:40123  |\n"
    assert LOCAL_TUNNEL_URL.findall(log) == ["http://127.0.0.1:40123"]
    assert not TUNNEL_URL.findall(log)
    assert not LOCAL_TUNNEL_URL.findall("http://127.0.0.1:40123.evil.example\n")
    strict = TunnelExposure(tmp_path, cloudflared=lambda: None)
    local = TunnelExposure(tmp_path, cloudflared=lambda: None, allow_local_url=True)
    assert strict._url_patterns == [TUNNEL_URL]
    assert local._url_patterns == [TUNNEL_URL, LOCAL_TUNNEL_URL]
