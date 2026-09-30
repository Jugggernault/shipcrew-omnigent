"""Environment-driven settings for the shipcrew orchestrator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENTS_DIR = "/home/jugggernault/Work/Projects/shipcrew/agents"
DEFAULT_DEPLOY_STATE_DIR = Path.home() / ".local" / "state" / "shipcrew" / "deploy"
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
    :param deploy_target: ``docker`` | ``vercel`` | ``argocd`` | ``auto``
        (``SHIPCREW_DEPLOY_TARGET``; see ``omnigent.shipcrew.deploy_targets``).
        :meth:`from_env` defaults to ``auto`` (docker when it works); a
        directly built instance (tests, embedders) keeps ``vercel``, the
        target with no side effect on this machine.
    :param preview_enabled: Server-side targets redeploy ``main`` after every
        merge, from the first one on (``SHIPCREW_PREVIEW``).
    :param preview_debounce_s: Wait this long after a merge before a redeploy,
        so merges in quick succession deploy once (the first deploy never waits).
    :param deploy_state_dir: Ports, tunnel URLs, Caddy snippets of the docker
        target (``SHIPCREW_DEPLOY_STATE_DIR``).
    :param deploy_memory: ``docker run --memory`` per mission container.
    :param deploy_cpus: ``docker run --cpus`` per mission container.
    :param deploy_build_timeout_s: ``docker build`` time limit.
    :param deploy_health_timeout_s: How long a new container may take to answer
        ``GET /`` before the swap is abandoned (the old one keeps serving).
    :param docker_build_network: ``docker build --network`` (e.g. ``host`` when
        the bridge network cannot reach the npm registry).
    :param public_base_domain: VPS mode: ``https://<mission-slug>.<domain>``
        through Caddy (e.g. ``203.0.113.7.sslip.io``) instead of quick tunnels.
    :param caddyfile: The Caddyfile ``caddy reload`` loads (it must
        ``import <caddy_sites_dir>/*.caddy``).
    :param caddy_sites_dir: Where the per-mission snippets go
        (default ``<deploy_state_dir>/caddy``).
    :param tunnel_url_timeout_s: How long cloudflared may take to print its URL.
    :param deploy_parallel: Server-side deploys (docker builds) at once.
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
    deploy_target: str = "vercel"
    preview_enabled: bool = True
    preview_debounce_s: float = 20.0
    deploy_state_dir: Path = DEFAULT_DEPLOY_STATE_DIR
    deploy_memory: str = "384m"
    deploy_cpus: str = "1"
    deploy_build_timeout_s: float = 900.0
    deploy_health_timeout_s: float = 90.0
    docker_build_network: str | None = None
    public_base_domain: str | None = None
    caddyfile: str = "/etc/caddy/Caddyfile"
    caddy_sites_dir: str | None = None
    tunnel_url_timeout_s: float = 45.0
    deploy_parallel: int = 2

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
            deploy_target=(env.get("SHIPCREW_DEPLOY_TARGET") or "auto").strip().lower(),
            preview_enabled=env.get("SHIPCREW_PREVIEW", "1").strip().lower() not in _FALSEY,
            preview_debounce_s=max(0.0, float(env.get("SHIPCREW_PREVIEW_DEBOUNCE_S") or 20.0)),
            deploy_state_dir=Path(
                env.get("SHIPCREW_DEPLOY_STATE_DIR") or DEFAULT_DEPLOY_STATE_DIR
            ).expanduser(),
            deploy_memory=env.get("SHIPCREW_DEPLOY_MEMORY") or "384m",
            deploy_cpus=env.get("SHIPCREW_DEPLOY_CPUS") or "1",
            deploy_build_timeout_s=float(env.get("SHIPCREW_DEPLOY_BUILD_TIMEOUT_S") or 900.0),
            deploy_health_timeout_s=float(env.get("SHIPCREW_DEPLOY_HEALTH_TIMEOUT_S") or 90.0),
            docker_build_network=env.get("SHIPCREW_DOCKER_BUILD_NETWORK") or None,
            public_base_domain=env.get("SHIPCREW_PUBLIC_BASE_DOMAIN") or None,
            caddyfile=env.get("SHIPCREW_CADDYFILE") or "/etc/caddy/Caddyfile",
            caddy_sites_dir=env.get("SHIPCREW_CADDY_SITES_DIR") or None,
            tunnel_url_timeout_s=float(env.get("SHIPCREW_TUNNEL_URL_TIMEOUT_S") or 45.0),
            deploy_parallel=max(1, int(env.get("SHIPCREW_DEPLOY_PARALLEL") or 2)),
        )
