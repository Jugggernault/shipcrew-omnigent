"""Thin wrappers over the ``gh`` CLI. Ported from shipcrew v0.2.

GitHub is the collaboration bus: issues = backlog, one task = one branch = one
PR, CI + review = merge gates. Humans use the same flow.

Every call resolves the executable through :func:`omnigent.shipcrew.tools.resolve`
(``SHIPCREW_GH`` overrides), so tests and the local e2e inject a fake ``gh``
(``scripts/shipcrew-fake-gh``). Calls block: async callers wrap them in
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
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

# Bounded per call: the PR loop runs these from the scheduler tick.
DEFAULT_TIMEOUT_S = 120.0
_LOG_MAX_CHARS = 12_000


class GhError(RuntimeError):
    """A ``gh`` call failed (non-zero exit, timeout or missing binary)."""


def gh_bin() -> str:
    """The ``gh`` executable: ``SHIPCREW_GH``, the saved tool path, PATH, or ``"gh"``."""
    tool = next(t for t in registry() if t.key == "GH")
    return resolve(tool) or "gh"


def _run(
    args: Sequence[str],
    cwd: Path | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [gh_bin(), *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**session_env(), "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh {' '.join(args[:3])}: {exc}") from exc


def gh(*args: str, cwd: Path | None, check: bool = True, timeout: float = 1800) -> str:
    r = _run(args, cwd, timeout=timeout)
    if check and r.returncode:
        raise GhError(f"gh {' '.join(args[:3])} failed: {r.stderr[-1500:]}")
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
    try:
        r = _run(
            ["api", "-X", "PUT", "repos/{owner}/{repo}/branches/main/protection", "--input", "-"],
            cwd,
            stdin=json.dumps(body),
        )
    except GhError:
        return False
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
        timeout=DEFAULT_TIMEOUT_S,
    )
    return int(out) if out and out != "null" else None


def pr_checks(cwd: Path, pr: int) -> tuple[bool, str]:
    """Wait for CI on the PR. (green, failing-check summary)."""
    r = _run(
        ["pr", "checks", str(pr), "--watch", "--fail-fast", "--interval", "20"],
        cwd,
        timeout=3600,
    )
    if "no checks reported" in (r.stdout + r.stderr):
        return True, "no CI configured"
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def pr_merge(cwd: Path, pr: int) -> bool:
    return pr_merge_result(cwd, pr)[0]


def pr_merge_result(cwd: Path, pr: int) -> tuple[bool, str]:
    """Squash-merge and delete the head branch. ``(merged, gh output)``."""
    r = _run(["pr", "merge", str(pr), "--squash", "--delete-branch"], cwd)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-1500:]


def update_branch(cwd: Path, pr: int) -> bool:
    """Bring the PR up to date with main. False = conflict (needs the integrator)."""
    return update_branch_status(cwd, pr)[0] == "ok"


def update_branch_status(cwd: Path, pr: int) -> tuple[str, str]:
    """``gh pr update-branch``: ``("ok" | "conflict" | "error", gh output)``."""
    r = _run(["pr", "update-branch", str(pr)], cwd)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0 or "already up-to-date" in out.lower():
        return "ok", out
    return ("conflict" if "conflict" in out.lower() else "error"), out[-1500:]


def repo_url(cwd: Path) -> str:
    return gh("repo", "view", "--json", "url", "-q", ".url", cwd=cwd, check=False)


# ── PR loop ─────────────────────────────────────────────────────


def pr_create(
    cwd: Path, *, title: str, body: str, head: str, base: str, draft: bool = True
) -> tuple[int, str]:
    """Open a PR from ``head`` into ``base``. ``(number, url)``."""
    args = ["pr", "create", "--title", title, "--body", body, "--head", head, "--base", base]
    if draft:
        args.append("--draft")
    out = gh(*args, cwd=cwd, timeout=DEFAULT_TIMEOUT_S)
    url = out.splitlines()[-1].strip() if out else ""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1]), url
    except ValueError as exc:
        raise GhError(f"gh pr create: unexpected output {out[-300:]!r}") from exc


@dataclass(frozen=True)
class PrState:
    """The slice of ``gh pr view`` the PR loop reads.

    :param state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``.
    """

    number: int
    state: str
    head_sha: str
    is_draft: bool
    url: str


def pr_view(cwd: Path, pr: int) -> PrState:
    out = gh(
        "pr",
        "view",
        str(pr),
        "--json",
        "number,state,headRefOid,isDraft,url",
        cwd=cwd,
        timeout=DEFAULT_TIMEOUT_S,
    )
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise GhError(f"gh pr view {pr}: unreadable output {out[-300:]!r}") from exc
    return PrState(
        number=int(data["number"]),
        state=str(data["state"]).upper(),
        head_sha=str(data.get("headRefOid") or ""),
        is_draft=bool(data.get("isDraft")),
        url=str(data.get("url") or ""),
    )


def pr_ready(cwd: Path, pr: int) -> None:
    """Mark a draft PR ready for review."""
    gh("pr", "ready", str(pr), cwd=cwd, timeout=DEFAULT_TIMEOUT_S)


@dataclass(frozen=True)
class ChecksStatus:
    """CI state of a PR's head.

    :param state: ``"green"``, ``"red"`` or ``"pending"``.
    :param summary: One ``name: state`` line per check.
    :param failing: Names of the failing checks.
    :param run_ids: Actions run ids of the failing checks (for their logs).
    """

    state: str
    summary: str = ""
    failing: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()


def _run_id(link: str) -> str | None:
    parts = link.rstrip("/").split("/")
    if "runs" in parts:
        idx = parts.index("runs")
        if idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return parts[idx + 1]
    return None


def pr_checks_status(cwd: Path, pr: int) -> ChecksStatus:
    """One non-blocking read of the PR's checks. "No checks reported" is green."""
    r = _run(["pr", "checks", str(pr), "--json", "name,state,bucket,link"], cwd)
    out = r.stdout + r.stderr
    if "no checks reported" in out.lower():
        return ChecksStatus("green", "no checks reported")
    try:
        checks: list[dict[str, Any]] = json.loads(r.stdout or "[]")
    except ValueError as exc:
        raise GhError(f"gh pr checks {pr}: unreadable output {out[-500:]!r}") from exc
    if not checks:
        return ChecksStatus("green", "no checks reported")
    buckets = [str(c.get("bucket") or "").lower() for c in checks]
    summary = "\n".join(f"{c.get('name')}: {c.get('state')}" for c in checks)
    failing = [c for c, b in zip(checks, buckets, strict=True) if b in ("fail", "cancel")]
    if failing:
        run_ids = [rid for c in failing if (rid := _run_id(str(c.get("link") or "")))]
        return ChecksStatus(
            "red",
            summary,
            failing=tuple(str(c.get("name")) for c in failing),
            run_ids=tuple(dict.fromkeys(run_ids)),
        )
    if "pending" in buckets:
        return ChecksStatus("pending", summary)
    return ChecksStatus("green", summary)


def run_failed_log(cwd: Path, run_id: str, max_chars: int = _LOG_MAX_CHARS) -> str:
    """Tail of ``gh run view <id> --log-failed`` (empty when unavailable)."""
    try:
        r = _run(["run", "view", run_id, "--log-failed"], cwd)
    except GhError:
        return ""
    text = (r.stdout or r.stderr).strip()
    return text if len(text) <= max_chars else "[...truncated...]\n" + text[-max_chars:]


# ── Issue sync (repo-targeted, bounded) ─────────────────────────────
# Every call names the repository with ``--repo`` so the result never depends on
# the server's cwd, and uses a short timeout so a hung ``gh`` cannot stall a tick.

SYNC_TIMEOUT_S = 60.0


def auth_status(cwd: Path | None = None) -> tuple[bool, str]:
    """``(logged_in, reason)`` from ``gh auth status``; reason is empty when logged in."""
    try:
        r = _run(["auth", "status"], cwd, timeout=SYNC_TIMEOUT_S)
    except GhError as exc:
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
