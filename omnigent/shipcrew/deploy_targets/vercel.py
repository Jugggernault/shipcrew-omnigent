"""The ``vercel`` target: a ``devops`` agent session runs the Vercel CLI.

Unchanged behaviour from the round 4 ship stage: a server preflight
(``vercel whoami`` through ``tools.resolve``, so ``SHIPCREW_VERCEL`` can point
at a fake), then a devops session in a fresh worktree of ``main`` that runs
``vercel link --yes --project <repo-name>`` and ``vercel deploy --prod --yes``
and ends with ``DEPLOYED: <url>`` or ``FAIL: <reason>``. Only the ship stage
deploys (no preview after each merge: every deploy is an agent session).
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from omnigent.shipcrew import tools
from omnigent.shipcrew.deploy_targets.base import repo_name

if TYPE_CHECKING:
    from omnigent.shipcrew.settings import ShipcrewSettings
    from omnigent.shipcrew.store import Mission

DEVOPS_ROLE = "devops"
_PREFLIGHT_TIMEOUT_S = 30.0
_PROJECT_MAX = 100


def vercel_project_name(repo_url: str | None, repo_path: str) -> str:
    """A valid Vercel project name from the repo name (lowercase, ``[a-z0-9._-]``)."""
    raw = repo_name(repo_url, repo_path)
    name = re.sub(r"[^a-z0-9._-]+", "-", raw.lower())
    name = re.sub(r"-{2,}", "-", name).strip("-._")[:_PROJECT_MAX].strip("-._")
    return name or "shipcrew-app"


def ship_prompt(mission: Mission, project: str) -> str:
    """The devops session's only message."""
    repo = mission.repo_url or mission.repo_path
    return "\n".join(
        [
            f"# Ship: {mission.title}",
            "",
            "Every task of this mission is merged. Your cwd is a fresh worktree of `main` "
            f"of {repo}.",
            "Deploy it to Vercel production, nothing else:",
            "",
            "1. `vercel whoami` (not logged in: stop with `FAIL: vercel is not logged in`).",
            f"2. `vercel link --yes --project {project}`",
            "3. `vercel deploy --prod --yes` (Vercel builds remotely: do not build locally).",
            "4. If the deployment fails, read `vercel inspect <deployment-url> --logs` and report",
            "   the cause. If the build needs environment variables, list their NAMES in the FAIL",
            "   reason; never invent values or secrets.",
            "",
            "Do not edit files, do not commit, do not push. The server checks the URL itself.",
            "",
            "End your reply with a `Decisions:` list (what you chose without asking, one line",
            "each, or `Decisions: none`), then the final line, nothing after it:",
            "`DEPLOYED: <https production url>` (prefer the production alias Vercel prints,",
            "e.g. `https://<project>.vercel.app`) or `FAIL: <reason>`.",
        ]
    )


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip("*`_#> \t") if lines else ""


_DEPLOYED = re.compile(r"^\W*DEPLOYED\W*:?\s*<?`?(?P<url>\S+?)`?>?[.)]*$", re.IGNORECASE)


def parse_deploy_reply(text: str | None) -> tuple[str, str] | None:
    """``("deployed", url)``, ``("fail", reason)`` or ``None`` (no verdict).

    The final line wins; a ``DEPLOYED:`` line further up counts when the final
    line is no verdict (an agent adding a sign-off).
    """
    line = _last_line(text)
    fail = re.match(r"FAIL\b[\s:.-]*(.*)", line, re.IGNORECASE)
    if fail:
        return "fail", fail.group(1).strip() or "no reason given"
    for candidate in [line, *reversed([ln.strip() for ln in (text or "").splitlines()])]:
        match = _DEPLOYED.match(candidate.strip("*_#> \t"))
        if match:
            return "deployed", match.group("url")
    return None


def _vercel_tool() -> tools.Tool:
    return next(t for t in tools.registry() if t.key == "VERCEL")


def vercel_preflight() -> str | None:
    """Why the ``vercel`` CLI cannot deploy from this machine, or ``None``."""
    path = tools.resolve(_vercel_tool())
    if path is None:
        return "vercel CLI not found: npm i -g vercel (or set SHIPCREW_VERCEL)"
    try:
        run = subprocess.run(
            [path, "whoami"],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT_S,
            env=tools.session_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"vercel whoami failed: {exc}"
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        tail = f" ({detail[-1][:200]})" if detail else ""
        return f"vercel is not logged in on the server machine: run `vercel login`{tail}"
    return None


class VercelTarget:
    """Vercel production through a devops session (agent target)."""

    name = "vercel"
    server_side = False
    agent_role = DEVOPS_ROLE

    def __init__(self, settings: ShipcrewSettings | None = None) -> None:
        self.settings = settings

    def preflight(self) -> str | None:
        return vercel_preflight()

    def agent_prompt(self, mission: Mission) -> str:
        return ship_prompt(mission, vercel_project_name(mission.repo_url, mission.repo_path))
