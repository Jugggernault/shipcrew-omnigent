"""Thin wrappers over the ``gh`` CLI. Ported from shipcrew v0.2.

GitHub is the collaboration bus: issues = backlog, one task = one branch = one
PR, CI + review = merge gates. Humans use the same flow.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from omnigent.shipcrew.tools import session_env

LABELS = {
    "feature": "0E8A16",
    "agent-ready": "5319E7",
    "qa": "D93F0B",
    "security": "B60205",
    "blocked": "000000",
}


def gh(*args: str, cwd: Path, check: bool = True) -> str:
    r = subprocess.run(
        ["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=1800, env=session_env()
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
            "gh",
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
        ["gh", "pr", "checks", str(pr), "--watch", "--fail-fast", "--interval", "20"],
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
        ["gh", "pr", "merge", str(pr), "--squash", "--delete-branch"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=session_env(),
    )
    return r.returncode == 0


def update_branch(cwd: Path, pr: int) -> bool:
    """Bring the PR up to date with main. False = conflict (needs the integrator)."""
    r = subprocess.run(
        ["gh", "pr", "update-branch", str(pr)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=session_env(),
    )
    return r.returncode == 0 or "already up-to-date" in (r.stdout + r.stderr).lower()


def repo_url(cwd: Path) -> str:
    return gh("repo", "view", "--json", "url", "-q", ".url", cwd=cwd, check=False)
