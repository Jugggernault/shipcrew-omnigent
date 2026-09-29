"""Environment-driven settings for the shipcrew orchestrator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENTS_DIR = "/home/jugggernault/Work/Projects/shipcrew/agents"
_FALSEY = {"0", "false", "no", "off"}


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if raw.lower() in {"none", "off", "unlimited"}:
        return None
    return float(raw)


@dataclass(frozen=True)
class ShipcrewSettings:
    """Orchestrator knobs, read from ``SHIPCREW_*`` environment variables.

    :param agents_dir: Directory holding one agent bundle per role, e.g.
        ``<agents_dir>/developer/config.yaml``.
    :param max_parallel: Capacity gate: at most this many agent-run tasks at once.
    :param max_usd: Budget gate: no new start once the summed task cost reaches
        this. ``None`` disables the gate.
    :param poll_interval_s: Scheduler tick period (status sync + starts).
    :param scheduler_enabled: Whether the server lifespan starts the loop.
    :param host_id: Host to launch task sessions on; ``None`` picks the first
        online host.
    :param base_branch: Base ref task branches fork from; ``None`` uses HEAD.
    :param db_url: Dedicated database for the shipcrew tables; ``None`` shares
        omnigent's database.
    :param pr_loop_enabled: Whether ticks drive review cards through push ->
        PR -> CI -> review -> merge (``SHIPCREW_PR_LOOP``).
    :param pr_base: Branch PRs target and diffs are taken against
        (``SHIPCREW_PR_BASE``).
    :param sync_interval_s: Seconds between GitHub issue syncs of a mission.
    """

    agents_dir: Path = Path(DEFAULT_AGENTS_DIR)
    max_parallel: int = 4
    max_usd: float | None = 100.0
    poll_interval_s: float = 5.0
    scheduler_enabled: bool = True
    host_id: str | None = None
    base_branch: str | None = None
    db_url: str | None = None
    pr_loop_enabled: bool = True
    pr_base: str = "main"
    sync_interval_s: float = 60.0

    @classmethod
    def from_env(cls) -> ShipcrewSettings:
        """Build settings from the process environment."""
        env = os.environ
        return cls(
            agents_dir=Path(env.get("SHIPCREW_AGENTS_DIR") or DEFAULT_AGENTS_DIR),
            max_parallel=max(1, int(env.get("SHIPCREW_MAX_PARALLEL") or 4)),
            max_usd=_env_float("SHIPCREW_MAX_USD", 100.0),
            poll_interval_s=max(0.5, float(env.get("SHIPCREW_POLL_INTERVAL_S") or 5.0)),
            scheduler_enabled=env.get("SHIPCREW_SCHEDULER", "1").strip().lower() not in _FALSEY,
            host_id=env.get("SHIPCREW_HOST_ID") or None,
            base_branch=env.get("SHIPCREW_BASE_BRANCH") or None,
            db_url=env.get("SHIPCREW_DB_URL") or None,
            pr_loop_enabled=env.get("SHIPCREW_PR_LOOP", "1").strip().lower() not in _FALSEY,
            pr_base=env.get("SHIPCREW_PR_BASE") or "main",
            sync_interval_s=max(5.0, float(env.get("SHIPCREW_SYNC_INTERVAL_S") or 60.0)),
        )
