"""Find, check and (when possible) install every tool the crew needs.

Resolution order per tool: ``SHIPCREW_<KEY>`` env var -> ``~/.config/shipcrew/tools.json``
-> PATH -> known dirs. Resolved paths are saved so headless sessions get them on
PATH (see :func:`session_env`). Ported from shipcrew v0.2.
"""

from __future__ import annotations

import glob
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shipcrew" / "tools.json"
)
BIN_DIR = Path.home() / ".local" / "bin"
KNOWN_DIRS = [
    "~/.local/bin",
    "~/.bun/bin",
    "~/.npm-global/bin",
    "~/.nvm/versions/node/*/bin",
    "~/.volta/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/snap/bin",
    "/usr/bin",
]


@dataclass
class Tool:
    """One external tool the crew depends on.

    :param key: Env override suffix: ``SHIPCREW_<KEY>``.
    :param bins: Candidate executable names.
    :param check: Args proving it works (version or auth).
    :param fix: What to tell the human.
    :param install: ``"gh"`` (built-in downloader) or a shell command, run by ``fix``.
    :param scopes: gh token scopes that must be present.
    :param mcp: A Claude Code MCP server, checked via ``claude mcp get``.
    """

    key: str
    name: str
    bins: tuple[str, ...]
    check: tuple[str, ...]
    fix: str
    required: bool = True
    install: str = ""
    scopes: tuple[str, ...] = ()
    mcp: bool = False


_CHROME_MCP_ADD = (
    "claude mcp add --scope user chrome-devtools -- npx -y chrome-devtools-mcp@latest"
)


def _local() -> bool:
    return os.environ.get("SHIPCREW_LOCAL") == "1"


def _deploy() -> bool:
    return not _local() and os.environ.get("SHIPCREW_DEPLOY", "1") != "0"


def registry() -> list[Tool]:
    return [
        Tool(
            "CLAUDE",
            "claude",
            ("claude",),
            ("--version",),
            "npm i -g @anthropic-ai/claude-code",
            install="npm i -g @anthropic-ai/claude-code",
        ),
        Tool("GIT", "git", ("git",), ("--version",), "install git with your package manager"),
        Tool("NPX", "node/npx", ("npx",), ("--version",), "install Node.js 20+ (nodejs + npm)"),
        Tool(
            "UV",
            "uv",
            ("uv",),
            ("--version",),
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            install="curl -LsSf https://astral.sh/uv/install.sh | sh",
        ),
        Tool(
            "GH",
            "gh (logged in, scopes repo+workflow)",
            ("gh",),
            ("auth", "status"),
            "install gh, then: gh auth login -s repo,workflow"
            "   (or SHIPCREW_LOCAL=1 for no GitHub)",
            required=not _local(),
            install="gh",
            scopes=("repo", "workflow"),
        ),
        Tool(
            "VERCEL",
            "vercel (logged in)",
            ("vercel",),
            ("whoami",),
            "npm i -g vercel && vercel login   (or SHIPCREW_DEPLOY=0 to skip deploy)",
            required=_deploy(),
            install="npm i -g vercel",
        ),
        Tool(
            "CHROMIUM",
            "chromium (e2e browser)",
            ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"),
            ("--version",),
            "install chromium with your package manager (Playwright uses it, no download)",
        ),
        Tool(
            "CHROME_DEVTOOLS_MCP",
            "chrome-devtools MCP (QA)",
            ("chrome-devtools",),
            (),
            _CHROME_MCP_ADD,
            mcp=True,
            install=_CHROME_MCP_ADD,
        ),
        Tool(
            "SHADCN_MCP",
            "shadcn MCP (web UI components)",
            ("shadcn",),
            (),
            "claude mcp add --scope user shadcn -- npx -y shadcn@latest mcp",
            mcp=True,
            install="claude mcp add --scope user shadcn -- npx -y shadcn@latest mcp",
        ),
        Tool(
            "DOCKER",
            "docker (for strix)",
            ("docker",),
            ("info",),
            "start docker: sudo systemctl start docker",
            required=False,
        ),
        Tool(
            "STRIX",
            "strix (optional deep pentest)",
            ("strix",),
            ("--version",),
            "curl -sSL https://strix.ai/install | bash  (+ STRIX_LLM, LLM_API_KEY)",
            required=False,
        ),
        Tool(
            "OPENPENCIL",
            "openpencil (brand .fig)",
            ("openpencil",),
            ("--version",),
            "npm i -g @open-pencil/cli",
            required=False,
            install="npm i -g @open-pencil/cli",
        ),
        Tool(
            "MAESTRO",
            "maestro (mobile e2e)",
            ("maestro",),
            ("--version",),
            'curl -fsSL "https://get.maestro.mobile.dev" | bash   (else Expo web + Playwright)',
            required=False,
        ),
    ]


def _saved() -> dict[str, str]:
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(key: str, path: str) -> None:
    data = _saved()
    data[key] = path
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2))


def resolve(t: Tool) -> str | None:
    """Absolute path of the tool's executable, or None."""
    if t.mcp:
        return None
    for cand in (os.environ.get(f"SHIPCREW_{t.key}"), _saved().get(t.key)):
        if cand and os.access(cand, os.X_OK):
            return cand
    for b in t.bins:
        if p := shutil.which(b):
            return p
    for d in KNOWN_DIRS:
        # Newest nvm version first.
        for folder in sorted(glob.glob(os.path.expanduser(d)), reverse=True):
            for b in t.bins:
                p = os.path.join(folder, b)
                if os.access(p, os.X_OK):
                    return p
    return None


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=session_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, r.stdout + r.stderr


def check(t: Tool, path: str | None = None) -> tuple[bool, str]:
    """(ok, reason). Reason is empty when ok."""
    if t.mcp:
        claude = resolve(registry()[0]) or "claude"
        ok, _ = _run([claude, "mcp", "get", t.bins[0]])
        return ok, "" if ok else "not configured"
    path = path or resolve(t)
    if not path:
        return False, "not found"
    ok, out = _run([path, *t.check])
    if not ok:
        auth_check = bool(t.check) and t.check[0] in ("auth", "whoami")
        return False, "not logged in / not working" if auth_check else "not working"
    missing = [s for s in t.scopes if not re.search(rf"['\"]{s}['\"]", out)]
    if missing:
        return False, f"token lacks scopes {missing}: gh auth refresh -s {','.join(t.scopes)}"
    return True, ""


def _install_gh() -> str | None:
    """Latest gh release into ~/.local/bin, no sudo. Linux only."""
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if sys.platform != "linux" or not arch:
        return None
    latest = "https://api.github.com/repos/cli/cli/releases/latest"
    with urllib.request.urlopen(latest, timeout=30) as r:
        assets: list[dict[str, Any]] = json.load(r)["assets"]
    url = next(
        a["browser_download_url"] for a in assets if a["name"].endswith(f"linux_{arch}.tar.gz")
    )
    with urllib.request.urlopen(url, timeout=300) as r:
        payload = r.read()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("/bin/gh"))
        extracted = tar.extractfile(member)
        if extracted is None:
            return None
        binary = extracted.read()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    dest = BIN_DIR / "gh"
    dest.write_bytes(binary)
    dest.chmod(0o755)
    return str(dest)


def install(t: Tool) -> bool:
    if not t.install:
        return False
    print(f"  installing {t.name}...")
    try:
        if t.install == "gh":
            if p := _install_gh():
                save(t.key, p)
                return True
            return False
        return (
            subprocess.run(t.install, shell=True, timeout=900, env=session_env()).returncode == 0
        )
    except Exception as e:  # noqa: BLE001 — network, permissions: report and move on
        print(f"  install failed: {e}")
        return False


def _ask_path(t: Tool) -> bool:
    """Interactive: let the human point at an existing binary. True if one was saved."""
    while True:
        ans = input(f"  path to {t.bins[0]}? (Enter = skip): ").strip()
        if not ans:
            return False
        p = os.path.expanduser(ans)
        if os.path.isdir(p):
            p = os.path.join(p, t.bins[0])
        if not os.access(p, os.X_OK):
            print(f"  {p} is not an executable")
            continue
        save(t.key, os.path.abspath(p))
        return True


def doctor(fix: bool = False, interactive: bool | None = None) -> bool:
    interactive = sys.stdin.isatty() if interactive is None else interactive
    ok = True
    for t in registry():
        good, why = check(t)
        if not good and fix and resolve(t) is None and install(t):
            good, why = check(t)
        if not good and interactive and not t.mcp and resolve(t) is None and _ask_path(t):
            good, why = check(t)
        if good and (p := resolve(t)):
            save(t.key, p)
        tag = "ok " if good else ("MISSING (required)" if t.required else "missing (optional)")
        print(f"{tag:20} {t.name}" + ("" if good else f"   [{why}] -> {t.fix}"))
        ok &= good or not t.required
    if not ok:
        print(
            "\nLogins are interactive: in Claude Code type "
            "`! gh auth login -s repo,workflow` / `! vercel login`."
        )
    return ok


def session_env() -> dict[str, str]:
    """os.environ + resolved tool dirs first on PATH + CHROMIUM_PATH, for every subprocess.

    ``CHROMIUM_PATH`` (when not set already): ``SHIPCREW_CHROMIUM``, else
    ``chrome-headless-shell`` (:func:`omnigent.shipcrew.resources.headless_chromium`),
    else the resolved full browser.
    """
    env = dict(os.environ)
    saved = _saved()
    dirs: list[str] = []
    for p in saved.values():
        d = os.path.dirname(p)
        if d and d not in dirs:
            dirs.append(d)
    env["PATH"] = os.pathsep.join([*dirs, env.get("PATH", "")])
    if "CHROMIUM_PATH" not in env:
        # Headless shell first (lighter), else the resolved full Chromium.
        from omnigent.shipcrew.resources import headless_chromium

        chromium = (
            os.environ.get("SHIPCREW_CHROMIUM") or headless_chromium() or saved.get("CHROMIUM")
        )
        if chromium:
            env["CHROMIUM_PATH"] = chromium
    return env
