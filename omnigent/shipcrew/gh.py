"""Thin wrappers over the ``gh`` CLI. Ported from shipcrew v0.2.

GitHub is the collaboration bus: issues = backlog, one task = one branch = one
PR, CI + review = merge gates. Humans use the same flow.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnigent.shipcrew.tools import registry, resolve, session_env

LABELS = {
    "feature": "0E8A16",
    "agent-ready": "5319E7",
    "qa": "D93F0B",
    "security": "B60205",
    "blocked": "000000",
}


def gh_bin() -> str:
    """The ``gh`` executable: ``SHIPCREW_GH`` env, saved tools config, PATH, known dirs.

    Tests and the local e2e point ``SHIPCREW_GH`` at a fake ``gh``.
    """
    tool = next(t for t in registry() if t.key == "GH")
    return resolve(tool) or "gh"


def gh(*args: str, cwd: Path | None, check: bool = True, timeout: float = 1800) -> str:
    r = subprocess.run(
        [gh_bin(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=session_env(),
    )
    if check and r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr[-1500:]}")
    return r.stdout.strip()


def repo_create(cwd: Path, name: str, private: bool = True) -> str:
    """Create the GitHub repo from the local one and push, unless origin already exists."""
    has_origin = (
        subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True
        ).returncode
        == 0
    )
    if not has_origin:
        visibility = "--private" if private else "--public"
        gh("repo", "create", name, visibility, "--source", ".", "--push", cwd=cwd)
    else:
        subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"], cwd=cwd, check=True, env=session_env()
        )
    return gh("repo", "view", "--json", "url", "-q", ".url", cwd=cwd)


def protect_main(cwd: Path, checks: list[str], human_approval: bool) -> bool:
    """main only moves through PRs with green CI. Best effort: needs admin, and a paid
    plan for private repos."""
    body = {
        "required_status_checks": {"strict": True, "contexts": checks},
        "enforce_admins": False,
        "required_pull_request_reviews": (
            {"required_approving_review_count": 1} if human_approval else None
        ),
        "restrictions": None,
    }
    r = subprocess.run(
        [
            gh_bin(),
            "api",
            "-X",
            "PUT",
            "repos/{owner}/{repo}/branches/main/protection",
            "--input",
            "-",
        ],
        cwd=cwd,
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=session_env(),
    )
    return r.returncode == 0


def ensure_labels(cwd: Path, extra: Sequence[str] = ()) -> None:
    for name, color in {**LABELS, **dict.fromkeys(extra, "C5DEF5")}.items():
        gh("label", "create", name, "--color", color, "--force", cwd=cwd, check=False)


def issue_create(cwd: Path, title: str, body: str, labels: list[str]) -> int:
    label_args = [arg for label in labels for arg in ("--label", label)]
    url = gh("issue", "create", "--title", title, "--body", body, *label_args, cwd=cwd)
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def issue_claimed_by_human(cwd: Path, number: int) -> bool:
    """A human took it: assigned to someone, or the agent-ready label was removed."""
    data = json.loads(
        gh("issue", "view", str(number), "--json", "assignees,labels,state", cwd=cwd)
    )
    labels = {label["name"] for label in data["labels"]}
    return bool(data["assignees"]) or "agent-ready" not in labels or data["state"] != "OPEN"


def pr_for_branch(cwd: Path, branch: str) -> int | None:
    out = gh(
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number",
        "-q",
        ".[0].number",
        cwd=cwd,
    )
    return int(out) if out else None


def pr_checks(cwd: Path, pr: int) -> tuple[bool, str]:
    """Wait for CI on the PR. (green, failing-check summary)."""
    r = subprocess.run(
        [gh_bin(), "pr", "checks", str(pr), "--watch", "--fail-fast", "--interval", "20"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=3600,
        env=session_env(),
    )
    if "no checks reported" in (r.stdout + r.stderr):
        return True, "no CI configured"
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def pr_merge(cwd: Path, pr: int) -> bool:
    r = subprocess.run(
        [gh_bin(), "pr", "merge", str(pr), "--squash", "--delete-branch"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=session_env(),
    )
    return r.returncode == 0


def update_branch(cwd: Path, pr: int) -> bool:
    """Bring the PR up to date with main. False = conflict (needs the integrator)."""
    r = subprocess.run(
        [gh_bin(), "pr", "update-branch", str(pr)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=session_env(),
    )
    return r.returncode == 0 or "already up-to-date" in (r.stdout + r.stderr).lower()


def repo_url(cwd: Path) -> str:
    return gh("repo", "view", "--json", "url", "-q", ".url", cwd=cwd, check=False)


# ── Issue sync (repo-targeted, bounded) ─────────────────────────────
# Every call names the repository with ``--repo`` so the result never depends on
# the server's cwd, and uses a short timeout so a hung ``gh`` cannot stall a tick.

SYNC_TIMEOUT_S = 60.0


def auth_status(cwd: Path | None = None) -> tuple[bool, str]:
    """``(logged_in, reason)`` from ``gh auth status``; reason is empty when logged in."""
    try:
        r = subprocess.run(
            [gh_bin(), "auth", "status"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT_S,
            env=session_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"gh unavailable: {exc}"
    if r.returncode:
        return False, "gh is not logged in (run `gh auth login`)"
    return True, ""


def ensure_repo_labels(repo: str, names: Sequence[str], cwd: Path | None = None) -> None:
    """Create (or refresh) labels in ``repo``; best effort."""
    for name in names:
        gh(
            "label",
            "create",
            name,
            "--color",
            LABELS.get(name, "C5DEF5"),
            "--force",
            "--repo",
            repo,
            cwd=cwd,
            check=False,
            timeout=SYNC_TIMEOUT_S,
        )


def create_issue(
    repo: str, title: str, body: str, labels: Sequence[str], cwd: Path | None = None
) -> tuple[int, str]:
    """Open an issue in ``repo``. Returns ``(number, url)``."""
    label_args = [arg for label in labels for arg in ("--label", label)]
    out = gh(
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        *label_args,
        cwd=cwd,
        timeout=SYNC_TIMEOUT_S,
    )
    url = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return int(url.rstrip("/").rsplit("/", 1)[-1]), url


def list_issues(
    repo: str, label: str, cwd: Path | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    """Open and closed issues carrying ``label``: number, state, assignees, labels, url."""
    out = gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        label,
        "--state",
        "all",
        "--limit",
        str(limit),
        "--json",
        "number,state,assignees,labels,url",
        cwd=cwd,
        timeout=SYNC_TIMEOUT_S,
    )
    data = json.loads(out or "[]")
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def list_pull_requests(
    repo: str, cwd: Path | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Recent PRs in any state: number, state, headRefName, url."""
    out = gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        str(limit),
        "--json",
        "number,state,headRefName,url",
        cwd=cwd,
        timeout=SYNC_TIMEOUT_S,
    )
    data = json.loads(out or "[]")
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
