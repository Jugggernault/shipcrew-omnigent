"""Resource governance: how many agent sessions this machine can hold right now.

``SHIPCREW_MAX_PARALLEL=auto`` (or ``auto:<ceiling>``) replaces the fixed
capacity cap with :func:`auto_max_parallel`, recomputed on every scheduler
tick from ``/proc/meminfo``::

    cap = running + floor((MemAvailable - reserve) / per_session)
    cap = min(cap, cpus, ceiling), at least max(1, running)

``MemAvailable`` already excludes the memory the running sessions use, so
they are added back as ``running``; a new start only needs its own share.
The per-session footprint comes from the measurements in
``docs/shipcrew/RESOURCES.md`` (``SHIPCREW_SESSION_MB`` overrides it). The
reserve (``SHIPCREW_MEM_RESERVE_MB``) keeps room for the build / e2e spikes
a session's commands cause (a ``next build`` peaks at ~1.4 GB PSS) and the
server itself.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Measured PSS per session with several running in parallel (RESOURCES.md):
#: claude-sdk developer ~300 MB; claude-native ~400 MB, ~660 MB with the
#: shadcn MCP server most native roles load. Rounded up.
SESSION_MB_SDK = 350
SESSION_MB_NATIVE = 700
DEFAULT_RESERVE_MB = 2048


def mem_available_mb(meminfo: str | None = None) -> int | None:
    """``MemAvailable`` in MB from ``/proc/meminfo`` text (read when not given).

    :returns: ``None`` when it cannot be read (not Linux): the caller then
        falls back to the configured ceiling.
    """
    if meminfo is None:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="ascii")
        except OSError:
            return None
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) // 1024
            except (IndexError, ValueError):
                return None
    return None


def cgroup_limit_mb(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """``memory.max`` of this process's cgroup v2 in MB, when one is set.

    A container or a systemd slice (``MemoryMax=``) caps the server's tree
    below the machine's RAM; ``MemAvailable`` does not see that cap.
    """
    try:
        rel = Path("/proc/self/cgroup").read_text(encoding="ascii").strip().split("::", 1)[-1]
        raw = (root / rel.lstrip("/") / "memory.max").read_text(encoding="ascii").strip()
        used = int((root / rel.lstrip("/") / "memory.current").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    if raw == "max":
        return None
    try:
        return max(0, (int(raw) - used) // (1024 * 1024))
    except ValueError:
        return None


def cpu_count() -> int:
    """CPUs this process may use (affinity mask, e.g. a container's cpuset)."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def auto_max_parallel(
    *,
    running: int,
    mem_available_mb: int | None,
    reserve_mb: int,
    per_session_mb: int,
    cpus: int,
    ceiling: int | None = None,
) -> int:
    """Capacity cap for this tick (pure).

    :param running: Agent-active tasks now (their memory is already used).
    :param mem_available_mb: ``MemAvailable`` (or the cgroup headroom when
        lower); ``None`` = unknown, then only ``cpus`` / ``ceiling`` bound it.
    :param reserve_mb: Memory never handed to new sessions.
    :param per_session_mb: Footprint of one more session.
    :param cpus: Usable CPUs: at most one session per CPU (a turn uses
        0.4-0.85 of a core, builds and e2e runs more).
    :param ceiling: Optional hard upper bound (``auto:<n>``).
    :returns: The cap, never below ``max(1, running)``: memory pressure stops
        new starts, it never evicts a running session, and one session may
        always run (else a small machine would never start anything).
    """
    cap = cpus
    if mem_available_mb is not None:
        headroom = max(0, mem_available_mb - reserve_mb)
        cap = min(cap, running + headroom // max(1, per_session_mb))
    if ceiling is not None:
        cap = min(cap, ceiling)
    return max(cap, running, 1)


def parse_max_parallel(raw: str | None, default: int = 4) -> tuple[int | None, int | None]:
    """``SHIPCREW_MAX_PARALLEL`` -> ``(fixed, auto_ceiling)``.

    ``"6"`` -> ``(6, None)``; ``"auto"`` -> ``(None, None)``; ``"auto:8"`` ->
    ``(None, 8)``; empty -> ``(default, None)``.

    :raises ValueError: On anything else.
    """
    value = (raw or "").strip().lower()
    if not value:
        return max(1, default), None
    if value == "auto":
        return None, None
    if value.startswith("auto:"):
        return None, max(1, int(value[5:]))
    return max(1, int(value)), None


#: Playwright's headless-only Chromium build (``chrome-headless-shell``): the
#: same Blink as Chromium without the browser UI layers. One e2e run measured
#: 220-330 MB PSS for the browser instead of 480-770 MB for a full Chromium in
#: ``--headless=new``, 25-30 % less CPU (RESOURCES.md). Found on PATH or in
#: Playwright's cache when some earlier tool put it there; never downloaded.
HEADLESS_SHELL_BIN = "chrome-headless-shell"
_PLAYWRIGHT_SHELL_GLOB = "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"


def headless_chromium(
    *,
    path_env: str | None = None,
    playwright_cache: Path | None = None,
) -> str | None:
    """The preferred headless browser binary, or ``None`` when no headless shell exists.

    Order: ``chrome-headless-shell`` on PATH, then the newest one in
    Playwright's browser cache (``$PLAYWRIGHT_BROWSERS_PATH`` or
    ``~/.cache/ms-playwright``). The caller falls back to the full Chromium
    (``$CHROMIUM_PATH`` / ``/usr/bin/chromium``), which Playwright runs in
    ``--headless=new`` anyway.
    """
    import shutil

    found = shutil.which(HEADLESS_SHELL_BIN, path=path_env)
    if found:
        return found
    if playwright_cache is None:
        env_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        playwright_cache = (
            Path(env_cache)
            if env_cache and env_cache != "0"
            else Path.home() / ".cache" / "ms-playwright"
        )

    def version(p: Path) -> int:
        tail = p.parents[1].name.rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else -1

    candidates = sorted(playwright_cache.glob(_PLAYWRIGHT_SHELL_GLOB), key=version, reverse=True)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None
