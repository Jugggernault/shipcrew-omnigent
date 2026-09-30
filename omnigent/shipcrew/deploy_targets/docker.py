"""The ``docker`` target: the server builds and runs the mission itself.

Deterministic, no agent, no approval. For a commit of ``origin/<base>``:

1. **Build** ``shipcrew/<slug>:<sha12>`` from the clean worktree with the
   repo's ``Dockerfile`` (the scaffolder writes one: Next.js standalone on bare
   Alpine, non-root, HEALTHCHECK); a repo without one gets the generic Node
   fallback (``templates/Dockerfile.node``) and ``templates/dockerignore``.
   The same commit already running and healthy skips the build.
2. **Run** it as ``shipcrew-<slug>-next`` with ``--restart unless-stopped``,
   ``--memory`` / ``--cpus`` limits, on a free ``127.0.0.1`` port, and wait
   for ``GET /`` to answer below 400.
3. **Swap**: point the public URL at the new port (forwarder or Caddy, see
   :mod:`.expose`), stop the old ``shipcrew-<slug>``, rename the new one to
   it. An unhealthy new container is removed and the old one keeps serving
   (:class:`DeployError` with ``kept_previous``).
4. **Prune** the mission's images, keeping the newest two (one rollback).

State (ports, tunnel URL) lives in ``<deploy_state_dir>/<slug>/``; containers
survive a server restart (and a reboot), the forwarder and the tunnel survive
a server restart and are restarted by :meth:`DockerTarget.refresh`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.shipcrew import tools
from omnigent.shipcrew.deploy_targets.base import DeployContext, DeployError, DeployResult
from omnigent.shipcrew.deploy_targets.base import mission_slug as _mission_slug
from omnigent.shipcrew.deploy_targets.expose import (
    CaddyExposure,
    ExposeError,
    Exposure,
    TunnelExposure,
    free_port,
)

if TYPE_CHECKING:
    from omnigent.shipcrew.settings import ShipcrewSettings
    from omnigent.shipcrew.store import Mission

_logger = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
FALLBACK_DOCKERFILE = TEMPLATES / "Dockerfile.node"
FALLBACK_DOCKERIGNORE = TEMPLATES / "dockerignore"
MISSION_LABEL = "shipcrew.mission"
SHA_LABEL = "shipcrew.sha"
KEEP_IMAGES = 2
DEFAULT_APP_PORT = 3000
_INFO_TIMEOUT_S = 20.0
_CMD_TIMEOUT_S = 60.0
_TAIL = 40


def _tool(key: str) -> tools.Tool:
    return next(t for t in tools.registry() if t.key == key)


def _tail(text: str, lines: int = _TAIL) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


class DockerTarget:
    """Build + run on this machine's Docker; public URL by tunnel or Caddy."""

    name = "docker"
    server_side = True

    def __init__(self, settings: ShipcrewSettings) -> None:
        self.settings = settings
        self.state_dir = Path(settings.deploy_state_dir).expanduser()
        self.exposure = self._exposure()

    # ── plumbing ──

    def _exposure(self) -> Exposure:
        domain = self.settings.public_base_domain
        if domain:
            if tools.resolve(_tool("CADDY")) is not None:
                return CaddyExposure(
                    domain=domain,
                    sites_dir=Path(self.settings.caddy_sites_dir or self.state_dir / "caddy"),
                    caddyfile=Path(self.settings.caddyfile),
                    caddy=lambda: tools.resolve(_tool("CADDY")),
                )
            _logger.warning(
                "shipcrew deploy: SHIPCREW_PUBLIC_BASE_DOMAIN is set but caddy is not "
                "installed: falling back to Cloudflare quick tunnels"
            )
        return TunnelExposure(
            self.state_dir,
            cloudflared=lambda: tools.resolve(_tool("CLOUDFLARED")),
            url_timeout_s=self.settings.tunnel_url_timeout_s,
        )

    def _docker_path(self) -> str | None:
        return tools.resolve(_tool("DOCKER"))

    def _docker(
        self, *args: str, timeout: float = _CMD_TIMEOUT_S, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        path = self._docker_path()
        if path is None:
            raise DeployError("docker not found (SHIPCREW_DOCKER)")
        try:
            run = subprocess.run(
                [path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=tools.session_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"docker {args[0]} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise DeployError(f"docker {args[0]} failed: {exc}") from exc
        if check and run.returncode != 0:
            raise DeployError(
                f"docker {' '.join(args[:2])} failed: {_tail(run.stderr or run.stdout, 15)}"
            )
        return run

    def _state_file(self, slug: str) -> Path:
        return self.state_dir / slug / "container.json"

    def _state(self, slug: str) -> dict[str, Any]:
        try:
            data = json.loads(self._state_file(slug).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, slug: str, **fields: Any) -> None:
        path = self._state_file(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**self._state(slug), **fields}))
        tmp.replace(path)

    @staticmethod
    def container(slug: str) -> str:
        return f"shipcrew-{slug}"

    @staticmethod
    def image_repo(slug: str) -> str:
        return f"shipcrew/{slug}"

    # ── DeployTarget ──

    def preflight(self) -> str | None:
        path = self._docker_path()
        if path is None:
            return "docker not found: install Docker (or set SHIPCREW_DOCKER)"
        try:
            run = subprocess.run(
                [path, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=_INFO_TIMEOUT_S,
                env=tools.session_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"docker info failed: {exc}"
        if run.returncode != 0:
            detail = _tail(run.stderr or run.stdout, 1)
            return f"the docker daemon is not reachable: {detail or 'docker info failed'}"
        if isinstance(self.exposure, TunnelExposure) and (
            tools.resolve(_tool("CLOUDFLARED")) is None
        ):
            return (
                "cloudflared not found: `python -m omnigent.shipcrew.tools --fix` downloads it to "
                "~/.local/bin (or set SHIPCREW_PUBLIC_BASE_DOMAIN with Caddy on a VPS)"
            )
        return None

    def _running_sha(self, slug: str) -> str | None:
        run = self._docker(
            "inspect",
            "--format",
            f'{{{{.State.Running}}}} {{{{index .Config.Labels "{SHA_LABEL}"}}}}',
            self.container(slug),
            check=False,
        )
        if run.returncode != 0:
            return None
        running, _, sha = run.stdout.strip().partition(" ")
        return sha.strip() if running == "true" else None

    def _healthy(self, port: int) -> tuple[bool, str]:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/",
                follow_redirects=True,
                timeout=5.0,
                headers={"User-Agent": "shipcrew-health/1"},
            )
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}"
        return response.status_code < 400, f"HTTP {response.status_code}"

    def _wait_healthy(self, name: str, port: int) -> None:
        deadline = time.monotonic() + self.settings.deploy_health_timeout_s
        last = "no answer"
        while True:
            ok, last = self._healthy(port)
            if ok:
                return
            state = self._docker(
                "inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name, check=False
            ).stdout.split()
            if state and state[0] != "true":
                raise DeployError(
                    f"the new container exited (code {state[-1] if len(state) > 1 else '?'})"
                )
            if time.monotonic() >= deadline:
                raise DeployError(
                    f"the new container did not answer GET / below 400 within "
                    f"{self.settings.deploy_health_timeout_s:.0f}s (last: {last})"
                )
            time.sleep(0.5)

    def _app_port(self, image: str) -> int:
        run = self._docker(
            "image", "inspect", "--format", "{{json .Config.ExposedPorts}}", image, check=False
        )
        try:
            ports = json.loads(run.stdout or "null") or {}
        except ValueError:
            ports = {}
        for key in sorted(ports):
            number, _, proto = str(key).partition("/")
            if proto in ("", "tcp") and number.isdigit():
                return int(number)
        return DEFAULT_APP_PORT

    def _image_mb(self, image: str) -> float | None:
        run = self._docker("image", "inspect", "--format", "{{.Size}}", image, check=False)
        try:
            return round(int(run.stdout.strip()) / 1_000_000, 1)
        except ValueError:
            return None

    def _build(self, ctx: DeployContext, image: str) -> str | None:
        """Build ``image``; the note when the fallback Dockerfile was used."""
        note = None
        args = [
            "build",
            "--tag",
            image,
            "--label",
            f"{MISSION_LABEL}={ctx.mission_id}",
            "--label",
            f"{SHA_LABEL}={ctx.sha}",
        ]
        if self.settings.docker_build_network:
            args += ["--network", self.settings.docker_build_network]
        dockerfile = ctx.worktree / "Dockerfile"
        if not dockerfile.is_file():
            fallback = ctx.worktree / ".shipcrew.Dockerfile"
            shutil.copyfile(FALLBACK_DOCKERFILE, fallback)
            args += ["--file", str(fallback)]
            note = "the repo has no Dockerfile: built with shipcrew's generic Node image"
        if not (ctx.worktree / ".dockerignore").is_file():
            shutil.copyfile(FALLBACK_DOCKERIGNORE, ctx.worktree / ".dockerignore")
        run = self._docker(
            *args, str(ctx.worktree), timeout=self.settings.deploy_build_timeout_s, check=False
        )
        if run.returncode != 0:
            raise DeployError(
                f"docker build failed (the previous version keeps serving):\n"
                f"{_tail(run.stderr + run.stdout)}",
                kept_previous=True,
            )
        return note

    def _prune(self, slug: str, keep_tag: str) -> None:
        run = self._docker(
            "image", "ls", self.image_repo(slug), "--format", "{{.Tag}}", check=False
        )
        tags = [t for t in run.stdout.split() if t and t != "<none>"]
        # `docker image ls` lists the newest first.
        keep = {keep_tag, *tags[:KEEP_IMAGES]}
        for tag in tags:
            if tag not in keep:
                self._docker("rmi", f"{self.image_repo(slug)}:{tag}", check=False)

    def deploy(self, ctx: DeployContext) -> DeployResult:
        slug, tag = ctx.slug, ctx.sha[:12]
        image = f"{self.image_repo(slug)}:{tag}"
        name, candidate = self.container(slug), f"{self.container(slug)}-next"
        state = self._state(slug)
        port = state.get("port")
        if self._running_sha(slug) == ctx.sha and isinstance(port, int) and self._healthy(port)[0]:
            url = self._expose_url(slug)
            return DeployResult(url, detail={"sha": ctx.sha, "skipped_build": True})
        started = time.monotonic()
        note = self._build(ctx, image)
        build_s = time.monotonic() - started
        app_port = self._app_port(image)
        host_port = free_port()
        self._docker("rm", "--force", candidate, check=False)
        self._docker(
            "run",
            "--detach",
            "--name",
            candidate,
            "--restart",
            "unless-stopped",
            "--memory",
            self.settings.deploy_memory,
            "--cpus",
            self.settings.deploy_cpus,
            "--pids-limit",
            "256",
            "--label",
            f"{MISSION_LABEL}={ctx.mission_id}",
            "--label",
            f"{SHA_LABEL}={ctx.sha}",
            "--env",
            f"PORT={app_port}",
            "--publish",
            f"127.0.0.1:{host_port}:{app_port}",
            image,
        )
        run_started = time.monotonic()
        try:
            self._wait_healthy(candidate, host_port)
        except DeployError as exc:
            logs = self._docker("logs", "--tail", "30", candidate, check=False)
            self._docker("rm", "--force", candidate, check=False)
            kept = self._running_sha(slug) is not None
            suffix = " The previous version keeps serving." if kept else ""
            raise DeployError(
                f"{exc}.{suffix}\n{_tail(logs.stdout + logs.stderr, 30)}", kept_previous=kept
            ) from exc
        start_s = time.monotonic() - run_started
        try:
            self.exposure.point(slug, host_port)
        except ExposeError as exc:
            self._docker("rm", "--force", candidate, check=False)
            raise DeployError(
                f"could not route the public URL: {exc}", kept_previous=True
            ) from exc
        # The public URL now reaches the new container: retire the old one.
        self._docker("rm", "--force", name, check=False)
        self._docker("rename", candidate, name)
        self._save(slug, port=host_port, sha=ctx.sha, image=image, app_port=app_port)
        with contextlib.suppress(DeployError):
            self._prune(slug, tag)
        expose_started = time.monotonic()
        url = self._expose_url(slug)
        detail = {
            "sha": ctx.sha,
            "image": image,
            "image_mb": self._image_mb(image),
            "build_s": round(build_s, 1),
            "start_s": round(start_s, 1),
            "expose_s": round(time.monotonic() - expose_started, 1),
        }
        return DeployResult(url, note=note, detail=detail)

    def _expose_url(self, slug: str) -> str:
        try:
            return self.exposure.ensure(slug)
        except ExposeError as exc:
            raise DeployError(
                f"the app runs but has no public URL: {exc}", kept_previous=True
            ) from exc

    def refresh(self, mission: Mission) -> str | None:
        slug = _mission_slug(mission)
        if not self._state(slug):
            return None
        current = self.exposure.current(slug)
        if current is not None:
            return current
        port = self._state(slug).get("port")
        if isinstance(port, int):
            with contextlib.suppress(ExposeError):
                self.exposure.point(slug, port)
        try:
            return self.exposure.ensure(slug)
        except ExposeError as exc:
            _logger.warning("shipcrew deploy: cannot expose %s: %s", slug, exc)
            return None

    def teardown(self, mission: Mission) -> None:
        slug = _mission_slug(mission)
        self.exposure.stop(slug)
        ids = self._docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={MISSION_LABEL}={mission.id}",
            check=False,
        ).stdout.split()
        for name in (self.container(slug), f"{self.container(slug)}-next", *ids):
            self._docker("rm", "--force", name, check=False)
        tags = self._docker(
            "image", "ls", self.image_repo(slug), "--format", "{{.Tag}}", check=False
        ).stdout.split()
        for tag in tags:
            self._docker("rmi", "--force", f"{self.image_repo(slug)}:{tag}", check=False)
        shutil.rmtree(self.state_dir / slug, ignore_errors=True)
