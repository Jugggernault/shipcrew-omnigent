"""Host build for the ``docker`` target: build on the host, package a runtime-only image.

The repo ``Dockerfile`` runs ``pnpm install`` inside docker with a cold cache
(the legacy builder has no cache mounts): minutes per deploy on a slow
network, and a transient registry error fails the deploy. The host already has
a warm package store and a ``node_modules`` next to the main checkout, so with
``SHIPCREW_DOCKER_BUILD=host`` (the default) the docker target:

1. **Restores** the mission's build cache into the clean deploy worktree
   (``node_modules`` and ``.next/cache`` of the previous deploy, moved, not
   copied), else seeds ``node_modules`` from the main checkout
   (:func:`omnigent.shipcrew.deps_seed.seed_node_modules`).
2. **Installs** from the lockfile, offline first (``pnpm install
   --frozen-lockfile --prefer-offline``, ``npm ci --prefer-offline``, ...),
   then **builds** (``<pm> run build`` when the repo has a build script) with
   ``CI=1``, ``NEXT_TELEMETRY_DISABLED=1``, ``NODE_OPTIONS=--max-old-space-size``
   from ``SHIPCREW_NODE_HEAP_MB``, a scrubbed environment (no server secrets)
   and one time budget (``SHIPCREW_DEPLOY_BUILD_TIMEOUT_S``); a timeout kills
   the whole process group.
3. **Packages** a temp build context outside the repo with a server-generated
   Dockerfile that only copies files (``docker build --network none``):

   * Next.js ``output: "standalone"`` (what the scaffolder writes):
     ``.next/standalone`` + ``.next/static`` + ``public/`` on
     ``SHIPCREW_DOCKER_RUNTIME_IMAGE`` (default distroless Node 22, glibc, so
     native modules built on this glibc host keep working), uid 10001,
     ``HEALTHCHECK``, ``node server.js``;
   * anything else (no standalone output): the built app with production
     dependencies only (``pnpm prune --prod`` / ``npm prune --omit=dev`` on a
     hardlinked copy) on ``node:22-bookworm-slim``, ``npm start`` as ``node``.

4. **Stashes** ``node_modules`` and ``.next/cache`` back into
   ``<deploy_state_dir>/<slug>/cache/`` for the next deploy.

A repo without ``package.json``, or whose package manager is not installed
here, is built with its ``Dockerfile`` as before (:class:`Unavailable`).
``SHIPCREW_DOCKER_BUILD=dockerfile`` always does (CI keeps that path).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.shipcrew import main_deps, tools
from omnigent.shipcrew.deps_seed import LOCKFILES, _unshare, seed_node_modules

GENERIC_IMAGE = "node:22-bookworm-slim"
APP_UID = "10001:10001"
LOG_FILE = "build.log"
# How long a deploy waits for the main checkout's background install.
MAIN_INSTALL_WAIT_S = 60.0
_TAIL = 40
# Environment the host build keeps from the server's: nothing secret.
_ENV_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SHELL",
    "PNPM_HOME",
    "NVM_DIR",
    "NVM_BIN",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_ENV_KEEP_PREFIXES = ("XDG_", "npm_config_", "NPM_CONFIG_", "COREPACK_")
_HEALTH_JS = (
    "fetch('http://127.0.0.1:'+process.env.PORT+'/')"
    ".then(r=>process.exit(r.status<500?0:1),()=>process.exit(1))"
)


class HostBuildError(Exception):
    """The install or the build failed; the message ends with the log tail."""


class Unavailable(Exception):
    """Host build does not apply here; the docker target uses the repo Dockerfile."""


@dataclass(frozen=True)
class PackageManager:
    """How one lockfile's package manager installs, builds and prunes."""

    name: str
    install: tuple[str, ...]
    prune: tuple[str, ...] | None


PACKAGE_MANAGERS: dict[str, PackageManager] = {
    "pnpm-lock.yaml": PackageManager(
        "pnpm", ("install", "--frozen-lockfile", "--prefer-offline"), ("prune", "--prod")
    ),
    "package-lock.json": PackageManager(
        "npm",
        ("ci", "--prefer-offline", "--no-audit", "--no-fund"),
        ("prune", "--omit=dev", "--no-audit", "--no-fund"),
    ),
    "yarn.lock": PackageManager(
        "yarn", ("install", "--frozen-lockfile", "--prefer-offline"), None
    ),
    "bun.lock": PackageManager("bun", ("install", "--frozen-lockfile"), None),
    "bun.lockb": PackageManager("bun", ("install", "--frozen-lockfile"), None),
}
# No lockfile: npm, and an install only when package.json declares dependencies.
NO_LOCKFILE = PackageManager(
    "npm",
    ("install", "--prefer-offline", "--no-audit", "--no-fund"),
    ("prune", "--omit=dev", "--no-audit", "--no-fund"),
)


def _pm_tool(name: str) -> tools.Tool:
    """``SHIPCREW_PNPM`` / ``SHIPCREW_NPM`` / ... override, else PATH (``tools.resolve``)."""
    return tools.Tool(name.upper(), name, (name,), ("--version",), f"install {name}")


@dataclass
class HostBuild:
    """A packaged build context, ready for ``docker build --network none``.

    :param kind: ``"standalone"`` (Next.js standalone) or ``"generic"``.
    :param timings: ``install_s``, ``app_build_s``, ``prepare_s`` (seconds).
    """

    context: Path
    kind: str
    package_manager: str
    seeded: str | None
    timings: dict[str, float] = field(default_factory=dict)

    def cleanup(self) -> None:
        shutil.rmtree(self.context, ignore_errors=True)


def build_env(heap_mb: int | None) -> dict[str, str]:
    """The host build's environment: tool PATH, locale, proxies; no server secrets."""
    full = tools.session_env()
    env = {k: v for k, v in full.items() if k in _ENV_KEEP or k.startswith(_ENV_KEEP_PREFIXES)}
    env.update(
        CI="1",
        NEXT_TELEMETRY_DISABLED="1",
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1",
        HUSKY="0",
        npm_config_update_notifier="false",
    )
    if heap_mb:
        env["NODE_OPTIONS"] = f"--max-old-space-size={int(heap_mb)}"
    return env


def _tail(path: Path, lines: int = _TAIL) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(5)


def run_step(
    argv: list[str], cwd: Path, env: dict[str, str], log: Path, *, deadline: float, label: str
) -> float:
    """Run one build step, output appended to ``log``; its duration in seconds.

    :raises HostBuildError: Non-zero exit, timeout (the process group is
        killed) or not runnable.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HostBuildError(f"{label}: no time left in the build budget")
    started = time.monotonic()
    with log.open("a", encoding="utf-8") as out:
        out.write(f"\n=== {label}: {' '.join(argv)}\n")
        out.flush()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise HostBuildError(f"{label} could not start: {exc}") from exc
        try:
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            out.write(f"timed out after {remaining:.0f} s\n")
            raise HostBuildError(
                f"{label} timed out after {remaining:.0f}s\n{_tail(log)}"
            ) from exc
        except BaseException:
            _kill_group(proc)
            raise
        elapsed = time.monotonic() - started
        out.write(f"exit {code} after {elapsed:.1f} s\n")
    if code != 0:
        raise HostBuildError(f"{label} failed (exit {code}):\n{_tail(log)}")
    return elapsed


def _package_json(worktree: Path) -> dict[str, Any]:
    try:
        data = json.loads((worktree / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable(f"package.json is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise Unavailable("package.json is not an object")
    return data


def plan(worktree: Path) -> tuple[str | None, PackageManager, str]:
    """``(lockfile, package manager, executable)`` for ``worktree``.

    :raises Unavailable: No ``package.json``, or the package manager is not installed.
    """
    if not (worktree / "package.json").is_file():
        raise Unavailable("the repo has no package.json")
    lockfile = next((n for n in LOCKFILES if (worktree / n).is_file()), None)
    pm = PACKAGE_MANAGERS[lockfile] if lockfile else NO_LOCKFILE
    exe = tools.resolve(_pm_tool(pm.name))
    if exe is None:
        raise Unavailable(f"{pm.name} is not installed on this machine")
    return lockfile, pm, exe


def _move(src: Path, dest: Path) -> bool:
    """``os.rename`` a directory (same filesystem); ``False`` when it did not happen."""
    if not src.is_dir() or src.is_symlink() or dest.exists() or dest.is_symlink():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dest)
    except OSError:
        return False
    return True


def _discard(path: Path) -> None:
    if path.exists() or path.is_symlink():
        doomed = path.with_name(f"{path.name}.old-{time.monotonic_ns()}")
        try:
            os.rename(path, doomed)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
            return
        threading.Thread(
            target=shutil.rmtree, args=(doomed,), kwargs={"ignore_errors": True}, daemon=True
        ).start()


def restore(cache: Path, worktree: Path, repo: Path) -> str | None:
    """Bring ``node_modules`` (and ``.next/cache``) into the worktree; how, or ``None``."""
    _move(cache / "next-cache", worktree / ".next" / "cache")
    if _move(cache / "node_modules", worktree / "node_modules"):
        return "cache"
    busy = repo.is_dir() and not main_deps.wait_idle(repo, MAIN_INSTALL_WAIT_S)
    return seed_node_modules(str(repo), str(worktree), skip_primary=busy)


def stash(cache: Path, worktree: Path) -> None:
    """Keep ``node_modules`` and ``.next/cache`` for the next deploy (moves)."""
    cache.mkdir(parents=True, exist_ok=True)
    for src, name in ((worktree / "node_modules", "node_modules"),
                      (worktree / ".next" / "cache", "next-cache")):  # fmt: skip
        if src.is_dir() and not src.is_symlink():
            _discard(cache / name)
            _move(src, cache / name)


def _copy(src: Path, dest: Path) -> None:
    """``cp -a`` (reflink when possible), symlinks kept as symlinks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run = subprocess.run(
        ["cp", "-a", "--reflink=auto", str(src), str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if run.returncode != 0:
        raise HostBuildError(f"copying {src.name} into the build context failed: {run.stderr}")


def _hardlink_copy(src: Path, dest: Path) -> None:
    run = subprocess.run(
        ["cp", "-al", str(src), str(dest)], capture_output=True, text=True, timeout=300
    )
    if run.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        _copy(src, dest)
    else:
        _unshare(dest)  # the package managers' in-place metadata files get their own copy


def node_binary(image: str) -> str:
    """The node executable of a runtime image (distroless has no ``node`` on PATH)."""
    return "/nodejs/bin/node" if "distroless" in image else "node"


def standalone_dockerfile(image: str) -> str:
    node = node_binary(image)
    health = json.dumps([node, "-e", _HEALTH_JS])
    return (
        "# Generated by shipcrew (host build): Next.js standalone, runtime only.\n"
        f"FROM {image}\n"
        "WORKDIR /app\n"
        "ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1\n"
        f"COPY --chown={APP_UID} standalone/ ./\n"
        f"COPY --chown={APP_UID} static/ ./.next/static/\n"
        f"COPY --chown={APP_UID} public/ ./public/\n"
        f"USER {APP_UID}\n"
        "EXPOSE 3000\n"
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\\n"
        f"  CMD {health}\n"
        f'ENTRYPOINT ["{node}", "server.js"]\n'
    )


def generic_dockerfile(image: str = GENERIC_IMAGE) -> str:
    health = json.dumps(["node", "-e", _HEALTH_JS])
    return (
        "# Generated by shipcrew (host build): the built app + production deps.\n"
        f"FROM {image}\n"
        "WORKDIR /app\n"
        "ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1 \\\n"
        "    npm_config_update_notifier=false\n"
        "COPY --chown=node:node app/ ./\n"
        "USER node\n"
        "EXPOSE 3000\n"
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\\n"
        f"  CMD {health}\n"
        'CMD ["npm", "start"]\n'
    )


# Never shipped in the generic image (the Dockerfile template's .dockerignore).
_GENERIC_SKIP = {".git", ".github", ".shipcrew", ".vercel", "coverage", "playwright-report",
                 "test-results", "node_modules", "Dockerfile", ".dockerignore"}  # fmt: skip


def _package_standalone(worktree: Path, context: Path, image: str) -> None:
    _copy(worktree / ".next" / "standalone", context / "standalone")
    static = worktree / ".next" / "static"
    if static.is_dir():
        _copy(static, context / "static")
    else:
        (context / "static").mkdir()
    public = worktree / "public"
    if public.is_dir():
        _copy(public, context / "public")
    else:
        (context / "public").mkdir()
    (context / "Dockerfile").write_text(standalone_dockerfile(image), encoding="utf-8")


def _package_generic(
    worktree: Path,
    context: Path,
    pm: PackageManager,
    exe: str,
    env: dict[str, str],
    log: Path,
    deadline: float,
) -> None:
    app = context / "app"
    app.mkdir()
    for entry in sorted(worktree.iterdir()):
        name = entry.name
        if name in _GENERIC_SKIP or (name.startswith(".env") and name != ".env.example"):
            continue
        _copy(entry, app / name)
    shutil.rmtree(app / ".next" / "cache", ignore_errors=True)
    modules = worktree / "node_modules"
    if modules.is_dir():
        _hardlink_copy(modules, app / "node_modules")
        for rel in (".cache", ".vite", ".vite-temp", ".vitest"):
            shutil.rmtree(app / "node_modules" / rel, ignore_errors=True)
        if pm.prune is not None:
            run_step([exe, *pm.prune], app, env, log, deadline=deadline, label=f"{pm.name} prune")
    (context / "Dockerfile").write_text(generic_dockerfile(), encoding="utf-8")


def host_build(
    worktree: Path,
    *,
    repo: Path,
    cache: Path,
    context: Path,
    log: Path,
    timeout_s: float,
    heap_mb: int | None,
    runtime_image: str,
) -> HostBuild:
    """Install, build and package ``worktree`` (see the module docstring).

    :raises Unavailable: Host build does not apply (use the repo Dockerfile).
    :raises HostBuildError: The install, build or packaging failed.
    """
    lockfile, pm, exe = plan(worktree)
    package = _package_json(worktree)
    deadline = time.monotonic() + timeout_s
    env = build_env(heap_mb)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"shipcrew host build of {worktree}\n", encoding="utf-8")
    timings: dict[str, float] = {}
    seeded = restore(cache, worktree, repo)
    installed = False
    try:
        wants_install = lockfile is not None or any(
            package.get(k) for k in ("dependencies", "devDependencies")
        )
        if wants_install:
            timings["install_s"] = run_step(
                [exe, *pm.install],
                worktree,
                env,
                log,
                deadline=deadline,
                label=f"{pm.name} install",
            )
        installed = True
        scripts = package.get("scripts")
        if isinstance(scripts, dict) and scripts.get("build"):
            timings["app_build_s"] = run_step(
                [exe, "run", "build"],
                worktree,
                env,
                log,
                deadline=deadline,
                label=f"{pm.name} run build",
            )
        started = time.monotonic()
        shutil.rmtree(context, ignore_errors=True)
        context.mkdir(parents=True)
        standalone = (worktree / ".next" / "standalone" / "server.js").is_file()
        try:
            if standalone:
                _package_standalone(worktree, context, runtime_image)
            else:
                _package_generic(worktree, context, pm, exe, env, log, deadline)
        except BaseException:
            shutil.rmtree(context, ignore_errors=True)
            raise
        timings["prepare_s"] = time.monotonic() - started
    finally:
        if installed:
            stash(cache, worktree)
        else:
            _discard(cache / "node_modules")  # a half-done install is not reused
    return HostBuild(
        context=context,
        kind="standalone" if standalone else "generic",
        package_manager=pm.name,
        seeded=seeded,
        timings={k: round(v, 1) for k, v in timings.items()},
    )
