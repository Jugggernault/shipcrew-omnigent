"""Deploy targets: ``SHIPCREW_DEPLOY_TARGET`` = ``docker`` | ``vercel`` | ``argocd`` | ``auto``.

``auto`` (the default) picks ``docker`` when the Docker daemon answers, else
``vercel`` when the CLI is logged in, else ``docker`` (whose preflight then
says what is missing). See :mod:`.base` for the interface.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from omnigent.shipcrew.deploy_targets.base import (
    AgentDeployTarget,
    DeployContext,
    DeployError,
    DeployResult,
    DeployTarget,
    mission_slug,
)

if TYPE_CHECKING:
    from omnigent.shipcrew.settings import ShipcrewSettings

__all__ = [
    "TARGETS",
    "AgentDeployTarget",
    "DeployContext",
    "DeployError",
    "DeployResult",
    "DeployTarget",
    "make_target",
    "mission_slug",
    "select_target",
]

# name -> "module:Class"; the class is built as ``Class(settings)``. Imported
# lazily, so a target whose module is not installed only fails when chosen.
TARGETS: dict[str, str] = {
    "docker": "omnigent.shipcrew.deploy_targets.docker:DockerTarget",
    "vercel": "omnigent.shipcrew.deploy_targets.vercel:VercelTarget",
    "argocd": "omnigent.shipcrew.deploy_targets.argocd:ArgoCDTarget",
}


def make_target(name: str, settings: ShipcrewSettings) -> Any:
    """Build the target registered as ``name``.

    :raises ValueError: Unknown name, or its module is missing.
    """
    spec = TARGETS.get(name)
    if spec is None:
        raise ValueError(f"unknown deploy target {name!r} (known: {', '.join(sorted(TARGETS))})")
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ValueError(f"deploy target {name!r} is not installed ({exc})") from exc
    factory: Callable[[ShipcrewSettings], Any] = getattr(module, attr)
    return factory(settings)


def select_target(settings: ShipcrewSettings) -> Any:
    """The target ``settings.deploy_target`` names (``auto``: see the module docstring)."""
    name = (settings.deploy_target or "auto").strip().lower()
    if name != "auto":
        return make_target(name, settings)
    docker = make_target("docker", settings)
    problem = docker.preflight()
    # Docker works even when only cloudflared is missing (doctor --fix installs it).
    if problem is None or problem.startswith("cloudflared"):
        return docker
    vercel = make_target("vercel", settings)
    if vercel.preflight() is None:
        return vercel
    return docker
