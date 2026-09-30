"""The deploy target interface: where a mission's ``main`` runs and how it is reached.

A target is picked once per server by ``SHIPCREW_DEPLOY_TARGET`` (see
:func:`omnigent.shipcrew.deploy_targets.select_target`). Two kinds:

* **server-side** (``server_side = True``, e.g. ``docker``, ``argocd``): the
  server calls :meth:`DeployTarget.deploy` itself, deterministically, with no
  agent and no approval. Such a target is redeployed after every merge (the
  mission *preview*, :mod:`omnigent.shipcrew.preview`) and the ship stage is
  only a last redeploy + URL check + report.
* **agent** (``server_side = False``, ``vercel``): a ``devops`` session deploys
  with the prompt :meth:`AgentDeployTarget.agent_prompt` gives, and replies
  ``DEPLOYED: <url>`` / ``FAIL: <reason>``. Only the ship stage deploys.

Whatever the target says, the server checks the public URL itself
(:func:`omnigent.shipcrew.ship.verify_url`) before it calls a deploy live.

Implementing a target (e.g. ``argocd`` in ``deploy_targets/argocd.py``): a class
with the attributes and methods of :class:`DeployTarget`, constructed as
``Target(settings)``, and an entry in ``deploy_targets.TARGETS``. Every method
is blocking (the server runs them in a worker thread) and must be idempotent:
a server restart may call ``deploy`` again for the same commit.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omnigent.shipcrew.store import Mission

_SLUG_MAX = 40
_GITHUB_NAME = re.compile(r"[/:](?P<name>[^/:]+?)(?:\.git)?/?$")


class DeployError(Exception):
    """A deploy that did not happen; the message is shown on the board.

    :param kept_previous: The previous version still serves (a failed swap).
    """

    def __init__(self, message: str, *, kept_previous: bool = False) -> None:
        super().__init__(message)
        self.kept_previous = kept_previous


@dataclass(frozen=True)
class DeployContext:
    """What a server-side deploy gets.

    :param slug: DNS-safe and stable for the mission (:func:`mission_slug`):
        image, container and host names derive from it.
    :param sha: The full commit of ``origin/<base>`` being deployed.
    :param worktree: A clean, detached checkout of ``sha`` (removed after the
        deploy; the target must not keep references into it).
    :param final: The ship stage's last deploy (vs. a preview after a merge).
    """

    mission_id: str
    slug: str
    title: str
    repo_path: Path
    repo_url: str | None
    sha: str
    worktree: Path
    base: str = "main"
    final: bool = False


@dataclass(frozen=True)
class DeployResult:
    """A deploy that is up.

    :param url: The public URL (the server verifies it before trusting it).
    :param note: A remark for the board and the report.
    :param detail: Numbers worth showing (timings, image size), JSON-safe.
    """

    url: str
    note: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DeployTarget(Protocol):
    """One way to run a mission. See the module docstring."""

    name: str
    server_side: bool

    def preflight(self) -> str | None:
        """Why this target cannot deploy from this machine, or ``None``. Fast."""
        ...

    def deploy(self, ctx: DeployContext) -> DeployResult:
        """Build and run ``ctx.sha``; raise :class:`DeployError` on failure.

        Server-side targets only. The previous version must keep serving when
        the new one is not healthy.
        """
        ...

    def refresh(self, mission: Mission) -> str | None:
        """Keep the mission's exposure alive (restart a dead tunnel); the
        current public URL, or ``None`` when nothing is deployed. Cheap when
        everything is up: called every few seconds."""
        ...

    def teardown(self, mission: Mission) -> None:
        """Stop and remove everything the target runs for ``mission``."""
        ...


class AgentDeployTarget(Protocol):
    """An agent target: a session of ``agent_role`` deploys."""

    name: str
    server_side: bool
    agent_role: str

    def preflight(self) -> str | None: ...

    def agent_prompt(self, mission: Mission) -> str:
        """The deploy session's only message."""
        ...


def repo_name(repo_url: str | None, repo_path: str) -> str:
    """The repository name from its URL (``acme/web.git`` -> ``web``), else the folder."""
    if repo_url:
        match = _GITHUB_NAME.search(repo_url.strip())
        if match:
            return match.group("name")
    return Path(repo_path).name


def mission_slug(mission: Mission) -> str:
    """``<repo-name>-<6 hex>``: lowercase, ``[a-z0-9-]``, one DNS label, stable.

    The suffix comes from the mission id, so two missions on one repo differ.
    """
    raw = repo_name(mission.repo_url, mission.repo_path).lower()
    base = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:_SLUG_MAX].strip("-") or "app"
    suffix = (mission.id or hashlib.sha1(raw.encode()).hexdigest())[:6].lower()
    suffix = re.sub(r"[^a-z0-9]", "0", suffix)
    return f"{base}-{suffix}"
