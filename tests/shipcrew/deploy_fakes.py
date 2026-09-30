"""Fake ``docker``, ``cloudflared`` and ``caddy`` executables for the deploy target tests.

Each fake is a small Python script resolved through ``SHIPCREW_DOCKER`` /
``SHIPCREW_CLOUDFLARED`` / ``SHIPCREW_CADDY`` (``tools.resolve``), exactly like
the real binaries. The fake docker keeps its world in a JSON file:

* ``build`` records an image; the context's ``fake-health`` file (e.g. ``500``)
  is the HTTP status its containers answer, ``fake-build-fail`` fails the build;
* ``run`` starts a real detached HTTP server on the published host port, so the
  target's health check, the forwarder and the swap are exercised for real;
* ``rm`` kills it. Every call is logged in ``calls``.

The fake ``pnpm`` / ``npm`` (``SHIPCREW_PNPM`` / ``SHIPCREW_NPM``) log every
call with its cwd and a few env vars (to ``pm.log`` next to them: the host
build scrubs the environment); files in the worktree steer them:
``fake-install-fail``, ``fake-build-fail``, ``fake-*-sleep`` (seconds),
``fake-standalone`` (the build writes ``.next/standalone/server.js``).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

FAKE_DOCKER = r'''
import fcntl, json, os, subprocess, sys, time
STATE = os.environ["FAKE_DOCKER_STATE"]
SERVER = r"""
import http.server, sys
status = int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"fake app {sys.argv[3]} {self.path}".encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""

def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True

def labels_of(args):
    values = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    return dict(v.split("=", 1) for v in values)

def opt(args, name):
    return args[args.index(name) + 1] if name in args else None

def main(argv):
    lock = open(STATE + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    st = load()
    st.setdefault("images", {}); st.setdefault("containers", {}); st.setdefault("calls", [])
    st.setdefault("seq", 0)
    st["calls"].append(argv)
    out, rc = "", 0
    cmd = argv[0] if argv else ""
    if cmd == "info":
        if st.get("down"):
            out, rc = "Cannot connect to the Docker daemon", 1
        else:
            out = "29.0.0"
    elif cmd == "build":
        ctx = argv[-1]
        tag = opt(argv, "--tag")
        if os.path.exists(os.path.join(ctx, "fake-build-fail")):
            out, rc = "npm ERR! build failed", 1
        else:
            health = 200
            for sub in ("", "app", "standalone"):
                hf = os.path.join(ctx, sub, "fake-health")
                if os.path.exists(hf):
                    health = int(open(hf).read().strip() or 200)
            labels = labels_of(argv)
            st["seq"] += 1
            df = os.path.join(ctx, "Dockerfile")
            files = sorted(
                os.path.relpath(os.path.join(root, n), ctx)
                for root, dirs, names in os.walk(ctx) for n in names + dirs
            )
            st["images"][tag] = {"health": health, "labels": labels, "seq": st["seq"],
                                 "file": opt(argv, "--file"), "network": opt(argv, "--network"),
                                 "context": ctx, "files": files,
                                 "dockerfile": open(df).read() if os.path.isfile(df) else None}
            out = "Successfully built"
    elif cmd == "image" and argv[1] == "inspect":
        img = st["images"].get(argv[-1])
        if img is None:
            out, rc = "No such image", 1
        elif "ExposedPorts" in argv[3]:
            out = json.dumps({"3000/tcp": {}})
        else:
            out = "123456789"
    elif cmd == "image" and argv[1] == "ls":
        repo = argv[2]
        mine = [(v["seq"], k.split(":", 1)[1]) for k, v in st["images"].items()
                if k.split(":", 1)[0] == repo]
        tags = sorted(mine)
        out = "\n".join(t for _, t in reversed(tags))
    elif cmd == "image" and argv[1] == "prune":
        out = "Total reclaimed space: 0B"
    elif cmd == "rmi":
        st["images"].pop(argv[-1], None)
    elif cmd == "run":
        name = opt(argv, "--name")
        image = argv[-1]
        host, cport = opt(argv, "--publish").rsplit(":", 2)[1:]
        img = st["images"][image]
        labels = labels_of(argv)
        proc = subprocess.Popen(
            [sys.executable, "-c", SERVER, host, str(img["health"]), image],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st["containers"][name] = {
            "image": image, "pid": proc.pid, "labels": labels, "port": int(host),
            "memory": opt(argv, "--memory"), "cpus": opt(argv, "--cpus"),
            "restart": opt(argv, "--restart")}
        out = "f" * 64
    elif cmd == "inspect":
        c = st["containers"].get(argv[-1])
        if c is None:
            out, rc = "No such object", 1
        else:
            running = "true" if alive(c["pid"]) else "false"
            if "shipcrew.sha" in argv[2]:
                out = f"{running} {c['labels'].get('shipcrew.sha', '')}"
            else:
                out = f"{running} 0"
    elif cmd == "logs":
        out = "fake container log line"
    elif cmd == "rm":
        c = st["containers"].pop(argv[-1], None)
        if c is None:
            out, rc = "No such container", 1
        else:
            try:
                os.killpg(c["pid"], 9)
            except OSError:
                pass
    elif cmd == "rename":
        st["containers"][argv[2]] = st["containers"].pop(argv[1])
    elif cmd == "ps":
        label = opt(argv, "--filter").split("=", 1)[1]
        key, _, value = label.partition("=")
        out = "\n".join(n for n, c in st["containers"].items() if c["labels"].get(key) == value)
    else:
        out, rc = f"fake docker: unsupported {argv}", 2
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE + ".tmp", STATE)
    (sys.stdout if rc == 0 else sys.stderr).write(out + ("\n" if out else ""))
    return rc

sys.exit(main(sys.argv[1:]))
'''

FAKE_CLOUDFLARED = r"""
import os, signal, sys, time
if sys.argv[1:2] == ["--version"]:
    print("cloudflared version 2026.9.3 (fake)"); sys.exit(0)
counter = os.environ["FAKE_CLOUDFLARED_COUNTER"]
if os.environ.get("FAKE_CLOUDFLARED_FAIL"):
    print("ERR failed to request quick Tunnel", file=sys.stderr); sys.exit(1)
try:
    n = int(open(counter).read()) + 1
except (OSError, ValueError):
    n = 1
open(counter, "w").write(str(n))
url = sys.argv[sys.argv.index("--url") + 1]
print(f"INF Requesting new quick Tunnel on trycloudflare.com for {url}",
      file=sys.stderr, flush=True)
print(f"INF |  https://fake-{n}.trycloudflare.com  |", file=sys.stderr, flush=True)
while True:
    time.sleep(3600)
"""

FAKE_CADDY = r"""
import json, os, sys
log = os.environ["FAKE_CADDY_LOG"]
if sys.argv[1:2] == ["version"]:
    print("v2.10.0 (fake)"); sys.exit(0)
sites = os.environ.get("FAKE_CADDY_SITES", "")
snap = {}
if sites and os.path.isdir(sites):
    snap = {n: open(os.path.join(sites, n)).read() for n in sorted(os.listdir(sites))}
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "sites": snap}) + "\n")
"""


FAKE_PM = r"""
import json, os, pathlib, sys, time
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
cwd = pathlib.Path.cwd()
env_keys = ("CI", "NEXT_TELEMETRY_DISABLED", "NODE_OPTIONS", "FAKE_SECRET", "PATH", "HOME")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm.log"), "a") as f:
    f.write(json.dumps({"pm": name, "args": args, "cwd": str(cwd),
                        "env": {k: os.environ.get(k) for k in env_keys}}) + "\n")
def flag(n):
    p = cwd / n
    return p.read_text().strip() if p.exists() else None
def sleep_for(n):
    s = flag(n)
    if s:
        (cwd.parent / (cwd.name + ".pid")).write_text(str(os.getpid()))
        time.sleep(float(s))
if args[:1] == ["--version"]:
    print("11.0.0-fake"); sys.exit(0)
if args[:1] in (["install"], ["ci"]):
    sleep_for("fake-install-sleep")
    if flag("fake-install-fail"):
        print("ERR_PNPM_FETCH_FAIL fake registry down"); sys.exit(1)
    nm = cwd / "node_modules"
    (nm / "dep").mkdir(parents=True, exist_ok=True)
    (nm / "devdep").mkdir(parents=True, exist_ok=True)
    (nm / "dep" / "index.js").write_text("module.exports = 1")
    count = int((nm / ".installs").read_text()) if (nm / ".installs").exists() else 0
    (nm / ".installs").write_text(str(count + 1))
    sys.exit(0)
if args[:2] == ["run", "build"]:
    sleep_for("fake-build-sleep")
    if flag("fake-build-fail"):
        print("Error: fake next build failed: Type error in app/page.tsx"); sys.exit(1)
    nxt = cwd / ".next"
    (nxt / "cache").mkdir(parents=True, exist_ok=True)
    runs = int((nxt / "cache" / "runs").read_text()) if (nxt / "cache" / "runs").exists() else 0
    (nxt / "cache" / "runs").write_text(str(runs + 1))
    (nxt / "BUILD_ID").write_text("fake")
    if flag("fake-standalone"):
        sa = nxt / "standalone"
        sa.mkdir(parents=True, exist_ok=True)
        (sa / "server.js").write_text("// fake standalone server")
        if (cwd / "fake-health").exists():
            (sa / "fake-health").write_text((cwd / "fake-health").read_text())
        (nxt / "static" / "chunks").mkdir(parents=True, exist_ok=True)
        (nxt / "static" / "chunks" / "app.js").write_text("//")
    sys.exit(0)
if args[:1] == ["prune"]:
    import shutil
    shutil.rmtree(cwd / "node_modules" / "devdep", ignore_errors=True)
    sys.exit(0)
print(f"fake {name}: unsupported {args}", file=sys.stderr); sys.exit(2)
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


class DeployFakes:
    """Installs the three fakes in ``root`` and points ``SHIPCREW_*`` at them."""

    def __init__(self, root: Path, monkeypatch: Any) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.state = root / "docker.json"
        self.counter = root / "cloudflared.count"
        self.caddy_log = root / "caddy.log"
        self.docker = _script(root / "docker", FAKE_DOCKER)
        self.cloudflared = _script(root / "cloudflared", FAKE_CLOUDFLARED)
        self.caddy = _script(root / "caddy", FAKE_CADDY)
        self.pm_log = root / "pm.log"
        self.pnpm = _script(root / "pnpm", FAKE_PM)
        self.npm = _script(root / "npm", FAKE_PM)
        monkeypatch.setenv("SHIPCREW_PNPM", str(self.pnpm))
        monkeypatch.setenv("SHIPCREW_NPM", str(self.npm))
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(self.state))
        monkeypatch.setenv("FAKE_CLOUDFLARED_COUNTER", str(self.counter))
        monkeypatch.setenv("FAKE_CADDY_LOG", str(self.caddy_log))
        monkeypatch.setenv("SHIPCREW_DOCKER", str(self.docker))
        monkeypatch.setenv("SHIPCREW_CLOUDFLARED", str(self.cloudflared))
        monkeypatch.setenv("SHIPCREW_CADDY", str(self.caddy))
        # Never pick up a real binary saved by a real `doctor` run.
        monkeypatch.setattr("omnigent.shipcrew.tools.CONFIG", root / "tools.json")

    def world(self) -> dict[str, Any]:
        try:
            return json.loads(self.state.read_text())
        except (OSError, ValueError):
            return {}

    def calls(self, command: str) -> list[list[str]]:
        return [c for c in self.world().get("calls", []) if c and c[0] == command]

    def pm_calls(self) -> list[dict[str, Any]]:
        """Every fake pnpm / npm call: ``{pm, args, cwd, env}``."""
        if not self.pm_log.is_file():
            return []
        return [json.loads(line) for line in self.pm_log.read_text().splitlines()]

    def set_down(self, down: bool = True) -> None:
        world = self.world()
        world["down"] = down
        self.state.write_text(json.dumps(world))

    def caddy_reloads(self) -> list[dict[str, Any]]:
        if not self.caddy_log.is_file():
            return []
        return [json.loads(line) for line in self.caddy_log.read_text().splitlines()]

    def kill_all(self, state_dir: Path | None = None) -> None:
        """Stop every fake container, and the forwarders / tunnels under ``state_dir``."""
        for container in self.world().get("containers", {}).values():
            _kill(int(container["pid"]))
        if state_dir is not None and state_dir.is_dir():
            for state in state_dir.rglob("*.json"):
                try:
                    pid = json.loads(state.read_text()).get("pid")
                except (OSError, ValueError):
                    continue
                if isinstance(pid, int):
                    _kill(pid)


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        time.sleep(0.02)
