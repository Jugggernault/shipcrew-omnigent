"""INTEGRATION STUB - replace with ``deploy_targets/base.py`` and delete this file.

A minimal local copy of the deploy-target contract that another branch
defines in ``omnigent/shipcrew/deploy_targets/base.py`` (``DeployTarget``
protocol: ``name``, ``preflight() -> str | None``, ``deploy(ctx) ->
DeployResult(url, note)``, ``teardown(mission)``). Only
:mod:`omnigent.shipcrew.deploy_targets.argocd` imports it; at integration,
switch that import to ``.base`` and adapt ``DeployContext`` field names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from omnigent.shipcrew.store import Mission


class DeployError(RuntimeError):
    """A deploy that did not produce a live URL; the message is the reason."""


@dataclass(frozen=True)
class DeployResult:
    url: str
    note: str | None = None


@dataclass(frozen=True)
class DeployContext:
    """What a target gets to deploy one mission.

    :param mission: The mission (``repo_url``, ``repo_path``, ``id``).
    :param base: The branch that is shipped (``SHIPCREW_PR_BASE``).
    """

    mission: Mission
    base: str = "main"


class DeployTarget(Protocol):
    name: str

    def preflight(self) -> str | None: ...

    def deploy(self, ctx: DeployContext) -> DeployResult: ...

    def teardown(self, mission: Mission) -> None: ...
