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
    :param ship_enabled: Whether ticks auto-ship missions whose agent tasks are
        all merged (``SHIPCREW_SHIP``; ``POST /ship`` works either way).
    :param ship_verify_s: How long the server retries the deployment URL
        before the ship fails (``SHIPCREW_SHIP_VERIFY_S``).
    :param ship_verify_interval_s: Pause between two URL checks.
    :param ship_allow_private_urls: Accept ``http://`` and private / loopback
        hosts as the deployment URL (tests and local fakes only).
    :param install_ci: Whether the first task start of a mission commits the
        shipcrew CI workflow to ``origin/<pr_base>`` when it has none
        (``SHIPCREW_INSTALL_CI``; needs the PR loop).
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
    ship_enabled: bool = True
    ship_verify_s: float = 120.0
    ship_verify_interval_s: float = 5.0
    ship_allow_private_urls: bool = False
    install_ci: bool = True

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
            ship_enabled=env.get("SHIPCREW_SHIP", "1").strip().lower() not in _FALSEY,
            ship_verify_s=max(1.0, float(env.get("SHIPCREW_SHIP_VERIFY_S") or 120.0)),
            ship_verify_interval_s=max(
                0.1, float(env.get("SHIPCREW_SHIP_VERIFY_INTERVAL_S") or 5.0)
            ),
            ship_allow_private_urls=(
                env.get("SHIPCREW_SHIP_ALLOW_PRIVATE_URLS", "0").strip().lower() not in _FALSEY
            ),
            install_ci=env.get("SHIPCREW_INSTALL_CI", "1").strip().lower() not in _FALSEY,
        )
