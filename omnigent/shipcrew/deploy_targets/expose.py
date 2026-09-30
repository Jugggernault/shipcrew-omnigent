"""Public URLs for server-side deploys, with no account and no API key.

Two modes, chosen by ``SHIPCREW_PUBLIC_BASE_DOMAIN``:

* **tunnel** (default): per mission, a detached forwarder on a stable local
  *front* port (:mod:`.forward`) and a Cloudflare quick tunnel
  (``cloudflared tunnel --url http://127.0.0.1:<front>``) that prints a
  ``https://<random>.trycloudflare.com`` URL. A swap only rewrites the
  forwarder's upstream file, so the URL lives as long as the tunnel process.
  Both processes are supervised: started in their own session (they outlive a
  server restart and are adopted again from their state file), restarted when
  they die; a restarted tunnel gets a new URL, which the caller persists.
* **caddy** (VPS): ``https://<slug>.<base domain>`` (e.g. ``<ip>.sslip.io``,
  free wildcard DNS by IP), one Caddyfile snippet per mission
  (``reverse_proxy 127.0.0.1:<container port>``) in a sites dir the main
  Caddyfile imports, then ``caddy reload``. Caddy gets the TLS certificate
  itself (Let's Encrypt); a swap rewrites the snippet and reloads.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Protocol

from omnigent.shipcrew import tools

_logger = logging.getLogger(__name__)

TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# Local e2e only (SHIPCREW_SHIP_ALLOW_PRIVATE_URLS=1): a fake cloudflared may
# print a loopback URL, so nothing public is created.
LOCAL_TUNNEL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost):[0-9]+/?(?=\s|$)")
_STOP_WAIT_S = 5.0
_CADDY_TIMEOUT_S = 30.0


class ExposeError(Exception):
    """The public URL could not be set up."""


def free_port(host: str = "127.0.0.1") -> int:
    """A port free on ``host`` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── Supervised background processes ─────────────────────────────


def _cmdline(pid: int) -> str | None:
    """The process's command line, ``""`` for a zombie, ``None`` when unknown."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except FileNotFoundError:
        return None if Path("/proc/self").exists() else _kill0(pid)
    except OSError:
        return None


def _kill0(pid: int) -> str | None:
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return "?"


class Supervised:
    """One detached process, known by a JSON state file (pid, argv, extras).

    A pid counts as ours only while its command line still holds the last
    argument it was started with, so a recycled pid is never mistaken for it.
    """

    # Our own children, polled so they are reaped (a zombie is not alive).
    _children: ClassVar[dict[int, subprocess.Popen[bytes]]] = {}

    def __init__(self, state_file: Path, log_file: Path) -> None:
        self.state_file = state_file
        self.log_file = log_file

    def state(self) -> dict[str, Any]:
        return _read_json(self.state_file)

    def save(self, **fields: Any) -> None:
        _write_atomic(self.state_file, json.dumps({**self.state(), **fields}))

    def pid(self) -> int | None:
        pid = self.state().get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    def alive(self) -> bool:
        state = self.state()
        pid = state.get("pid")
        argv = state.get("argv")
        if not isinstance(pid, int) or pid <= 0 or not isinstance(argv, list) or not argv:
            return False
        child = self._children.get(pid)
        if child is not None and child.poll() is not None:
            # Reaped. poll() alone is not proof of death: it reports 0 on ECHILD
            # (someone else reaped it, or a live run: a 2 ms "dead" tunnel), so
            # /proc decides below: a reaped pid is gone, a zombie has no cmdline.
            self._children.pop(pid, None)
        line = _cmdline(pid)
        return bool(line) and (line == "?" or str(argv[-1]) in (line or ""))

    def start(self, argv: list[str], **extra: Any) -> int:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("wb") as log:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=tools.session_env(),
            )
        self._children[child.pid] = child
        _write_atomic(
            self.state_file,
            json.dumps({"pid": child.pid, "argv": argv, "started_at": time.time(), **extra}),
        )
        return child.pid

    def stop(self) -> None:
        pid = self.pid()
        if pid is not None and self.alive():
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + _STOP_WAIT_S
            while self.alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.alive():
                with contextlib.suppress(OSError):
                    os.killpg(pid, signal.SIGKILL)
            child = self._children.pop(pid, None)
            if child is not None:
                with contextlib.suppress(Exception):
                    child.wait(timeout=_STOP_WAIT_S)
        with contextlib.suppress(OSError):
            self.state_file.unlink()

    def log_text(self) -> str:
        try:
            return self.log_file.read_text(errors="replace")
        except OSError:
            return ""


# ── Modes ───────────────────────────────────────────────────────


class Exposure(Protocol):
    """Puts a local port on a public URL, per mission slug."""

    mode: str

    def point(self, slug: str, port: int) -> None:
        """Route the mission's public URL to ``127.0.0.1:<port>`` (the new container)."""
        ...

    def ensure(self, slug: str) -> str:
        """Start what is not running; the public URL. Raises :class:`ExposeError`."""
        ...

    def current(self, slug: str) -> str | None:
        """The public URL while it is up, else ``None``. Never starts anything."""
        ...

    def stop(self, slug: str) -> None: ...


class TunnelExposure:
    """Forwarder on a stable front port + a Cloudflare quick tunnel to it."""

    mode = "tunnel"

    def __init__(
        self,
        state_dir: Path,
        *,
        cloudflared: Callable[[], str | None],
        url_timeout_s: float = 45.0,
        python: str = sys.executable,
        allow_local_url: bool = False,
    ) -> None:
        self.state_dir = state_dir
        self._cloudflared = cloudflared
        self.url_timeout_s = url_timeout_s
        self._url_patterns = [TUNNEL_URL, *([LOCAL_TUNNEL_URL] if allow_local_url else [])]
        self.python = python

    def _dir(self, slug: str) -> Path:
        return self.state_dir / slug

    def _upstream(self, slug: str) -> Path:
        return self._dir(slug) / "upstream"

    def _forwarder(self, slug: str) -> Supervised:
        d = self._dir(slug)
        return Supervised(d / "forwarder.json", d / "forwarder.log")

    def _tunnel(self, slug: str) -> Supervised:
        d = self._dir(slug)
        return Supervised(d / "tunnel.json", d / "tunnel.log")

    def front_port(self, slug: str) -> int | None:
        port = self._forwarder(slug).state().get("port")
        return port if isinstance(port, int) else None

    def point(self, slug: str, port: int) -> None:
        _write_atomic(self._upstream(slug), f"127.0.0.1:{port}\n")
        self._ensure_forwarder(slug)

    def _ensure_forwarder(self, slug: str) -> int:
        """The front port, with its forwarder running (a new port if it was taken)."""
        fwd = self._forwarder(slug)
        port = self.front_port(slug)
        if port is not None and fwd.alive():
            return port
        for attempt in range(3):
            if port is None or attempt:
                port = free_port()
            argv = [
                self.python,
                "-m",
                "omnigent.shipcrew.deploy_targets.forward",
                "--listen",
                f"127.0.0.1:{port}",
                "--upstream-file",
                str(self._upstream(slug)),
            ]
            fwd.start(argv, port=port)
            if _wait_listening(port, lambda: fwd.alive(), 5.0):
                return port
            fwd.stop()
        raise ExposeError(f"the forwarder did not start: {fwd.log_text()[-300:]}")

    def ensure(self, slug: str) -> str:
        if not self._upstream(slug).is_file():
            raise ExposeError("nothing to expose yet (no container)")
        old_front = self.front_port(slug)
        front = self._ensure_forwarder(slug)
        tunnel = self._tunnel(slug)
        state = tunnel.state()
        url = state.get("url")
        if tunnel.alive() and state.get("front") == front and isinstance(url, str):
            return url
        if tunnel.alive():
            tunnel.stop()  # the front port moved
        binary = self._cloudflared()
        if binary is None:
            raise ExposeError(
                "cloudflared not found: `python -m omnigent.shipcrew.tools --fix` installs it "
                "(or set SHIPCREW_CLOUDFLARED, or SHIPCREW_PUBLIC_BASE_DOMAIN for Caddy)"
            )
        argv = [
            binary,
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://127.0.0.1:{front}",
        ]
        _logger.info("shipcrew deploy: starting a quick tunnel for %s (front %s)", slug, front)
        tunnel.start(argv, front=front)
        deadline = time.monotonic() + self.url_timeout_s
        while time.monotonic() < deadline:
            text = tunnel.log_text()
            found = next((f for rx in self._url_patterns if (f := rx.findall(text))), None)
            if found:
                tunnel.save(url=found[-1], front=front)
                if old_front is not None and old_front != front:
                    _logger.info("shipcrew deploy: %s front port moved to %s", slug, front)
                return found[-1]
            if not tunnel.alive():
                break
            time.sleep(0.2)
        log = tunnel.log_text()[-500:]
        tunnel.stop()
        raise ExposeError(f"cloudflared printed no trycloudflare.com URL: {log.strip()}")

    def current(self, slug: str) -> str | None:
        tunnel = self._tunnel(slug)
        url = tunnel.state().get("url")
        if isinstance(url, str) and tunnel.alive() and self._forwarder(slug).alive():
            return url
        return None

    def stop(self, slug: str) -> None:
        self._tunnel(slug).stop()
        self._forwarder(slug).stop()
        with contextlib.suppress(OSError):
            self._upstream(slug).unlink()


def _wait_listening(port: int, alive: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        if not alive():
            return False
        time.sleep(0.05)
    return False


class CaddyExposure:
    """``https://<slug>.<domain>`` through a Caddy reverse proxy (automatic TLS)."""

    mode = "caddy"

    def __init__(
        self,
        *,
        domain: str,
        sites_dir: Path,
        caddyfile: Path,
        caddy: Callable[[], str | None],
    ) -> None:
        self.domain = domain.strip().strip(".").lower()
        self.sites_dir = sites_dir
        self.caddyfile = caddyfile
        self._caddy = caddy

    def url(self, slug: str) -> str:
        return f"https://{slug}.{self.domain}"

    def _snippet(self, slug: str) -> Path:
        return self.sites_dir / f"{slug}.caddy"

    def _reload(self) -> None:
        binary = self._caddy()
        if binary is None:
            raise ExposeError("caddy not found (SHIPCREW_CADDY): install it with the VPS script")
        try:
            run = subprocess.run(
                [binary, "reload", "--config", str(self.caddyfile), "--adapter", "caddyfile"],
                capture_output=True,
                text=True,
                timeout=_CADDY_TIMEOUT_S,
                env=tools.session_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExposeError(f"caddy reload failed: {exc}") from exc
        if run.returncode != 0:
            raise ExposeError(f"caddy reload failed: {(run.stderr or run.stdout).strip()[-300:]}")

    def point(self, slug: str, port: int) -> None:
        body = (
            f"# shipcrew mission preview (generated; rewritten on every deploy)\n"
            f"{slug}.{self.domain} {{\n"
            f"\tencode gzip\n"
            f"\treverse_proxy 127.0.0.1:{port}\n"
            f"}}\n"
        )
        _write_atomic(self._snippet(slug), body)
        self._reload()

    def ensure(self, slug: str) -> str:
        if not self._snippet(slug).is_file():
            raise ExposeError("nothing to expose yet (no container)")
        return self.url(slug)

    def current(self, slug: str) -> str | None:
        return self.url(slug) if self._snippet(slug).is_file() else None

    def stop(self, slug: str) -> None:
        path = self._snippet(slug)
        if path.is_file():
            path.unlink()
            with contextlib.suppress(ExposeError):
                self._reload()
