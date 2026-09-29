"""The PR loop: push -> draft PR -> CI fix x3 -> Claude review -> policy -> serialized merge.

The scheduler moves a card to ``review`` when its root agent finishes a turn.
Each tick, :meth:`PrLoop.tick` advances every review card by at most one step.
Nothing lives only in memory: every decision is re-derived from the DB row,
the task's git worktree and ``gh`` (``pr view`` / ``pr checks``), so a server
restart resumes exactly where the loop stopped.

Per card, in order:

1. **Open.** No PR yet: the developer's last line must be ``PASS`` and its
   branch must have commits over ``origin/<base>``. Push, open a draft PR
   (acceptance checklist, ``Closes #<issue>``), store ``pr_number``/``pr_url``.
2. **Sync.** The worktree head differs from the PR head: push new developer
   commits (a CI or review fix), or fast-forward to commits GitHub added.
3. **CI.** ``gh pr checks``: pending waits; red sends the failing logs to the
   developer as a new turn (card back to ``running``), at most
   :data:`MAX_CI_FIX_ATTEMPTS` times, then ``intervention``. No checks = green.
4. **Review.** A fresh read-only reviewer child session per head SHA. Its last
   line is ``APPROVE`` or ``CHANGES: <summary>`` and a fenced JSON block holds
   its findings. ``CHANGES`` sends the feedback to the developer; the
   :data:`MAX_REVIEW_ROUNDS`-th ``CHANGES`` parks the card in ``intervention``.
5. **Policy.** ``APPROVALS.md`` (read from ``origin/<base>``, so a branch cannot
   loosen it) or :data:`DEFAULT_APPROVAL_RULES`: a changed path matching a rule
   needs ``POST /tasks/{id}/approve``.
6. **Merge**, one at a time per repository: ``gh pr ready``,
   ``gh pr update-branch`` (conflict -> integrator child session, then CI
   again), ``gh pr merge --squash --delete-branch``. Then the sessions are
   stopped, the worktree removed and the card set ``merged`` (which unblocks
   dependants through the scheduler's gates).

The PR loop runs ``git``/``gh`` on the server machine, so the mission repo
must be on this filesystem (the same assumption as the folder pre-trust).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.shipcrew import gh
from omnigent.shipcrew.branches import task_branch
from omnigent.shipcrew.sessions import ChildSessionRequest, SessionServiceError
from omnigent.shipcrew.store import Mission, Task
from omnigent.shipcrew.tools import session_env

if TYPE_CHECKING:
    from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)

MAX_CI_FIX_ATTEMPTS = 3
MAX_REVIEW_ROUNDS = 3
TEMPLATES_DIR = Path(__file__).parent / "templates"
APPROVALS_FILE = "APPROVALS.md"
REVIEWER_ROLE = "reviewer"
INTEGRATOR_ROLE = "integrator"
LOOP_LABEL_KEY = "shipcrew.loop"
_GIT_TIMEOUT_S = 120.0
_LOG_RUNS_MAX = 3
_SEVERITIES = ("blocker", "major", "minor")


class GitError(RuntimeError):
    """A git command of the PR loop failed."""


# ── Verdicts ────────────────────────────────────────────────────


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip("*`_#> \t") if lines else ""


def parse_verdict(text: str | None) -> tuple[str, str] | None:
    """A builder's final line: ``("pass", "")``, ``("fail", reason)`` or ``None``."""
    line = _last_line(text)
    if re.fullmatch(r"PASS\.?", line, re.IGNORECASE):
        return "pass", ""
    fail = re.match(r"FAIL\b[\s:.-]*(.*)", line, re.IGNORECASE)
    if fail:
        return "fail", fail.group(1).strip() or "no reason given"
    return None


def _findings(text: str) -> list[dict[str, Any]]:
    """Findings from the last fenced ```json block (``{"findings": [...]}`` or a list)."""
    blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        raw = data.get("findings") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            continue
        findings: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("message"):
                continue
            line = item.get("line")
            severity = str(item.get("severity") or "major").lower()
            findings.append(
                {
                    "file": str(item.get("file") or ""),
                    "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                    "severity": severity if severity in _SEVERITIES else "major",
                    "message": str(item["message"]),
                }
            )
        return findings
    return []


def parse_review(text: str | None) -> dict[str, Any] | None:
    """The reviewer's verdict as the ``Task.review`` shape, or ``None`` without one."""
    line = _last_line(text)
    if re.fullmatch(r"APPROVE[D]?\.?", line, re.IGNORECASE):
        verdict, summary = "approve", ""
    else:
        changes = re.match(r"CHANGES\b[\s:.-]*(.*)", line, re.IGNORECASE)
        if changes is None:
            return None
        verdict, summary = "changes", changes.group(1).strip()
    return {"verdict": verdict, "summary": summary, "findings": _findings(text or "")}


# ── APPROVALS.md policy ─────────────────────────────────────────


@dataclass(frozen=True)
class ApprovalRule:
    """Changes to a path matching ``glob`` need a human approval."""

    glob: str
    reason: str = ""


DEFAULT_APPROVAL_RULES: tuple[ApprovalRule, ...] = (
    ApprovalRule("auth/**", "authentication code"),
    ApprovalRule(".github/**", "CI and GitHub configuration"),
    ApprovalRule("**/migrations/**", "database migrations"),
    ApprovalRule("infra/**", "infrastructure"),
    ApprovalRule("**/.env*", "environment files"),
    ApprovalRule("agents/**", "agent bundles"),
)

_RULE_LINE = re.compile(r"^[-*+]\s+`?([^`\s]+)`?\s*(?:[:—–-]+\s*(.*))?$")


def parse_approvals(text: str) -> tuple[ApprovalRule, ...]:
    """Rules from an ``APPROVALS.md``: one Markdown list item per glob.

    Example::

        # Changes that need a human
        - `auth/**` — authentication
        - `db/schema.sql`: schema changes

    Anything else (headings, prose, fenced blocks) is ignored.
    """
    rules: list[ApprovalRule] = []
    fenced = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _RULE_LINE.match(line)
        if match:
            rules.append(ApprovalRule(match.group(1), (match.group(2) or "").strip()))
    return tuple(rules)


def glob_regex(glob: str) -> re.Pattern[str]:
    """Compile a path glob: ``**`` spans directories, ``*`` and ``?`` do not."""
    glob = glob.strip().lstrip("/")
    if glob.endswith("/"):
        glob += "**"
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out)


def approval_reasons(changed: Sequence[str], rules: Sequence[ApprovalRule]) -> list[str]:
    """One human-readable reason per rule that a changed path matches."""
    reasons: list[str] = []
    for rule in rules:
        pattern = glob_regex(rule.glob)
        hits = [p for p in changed if pattern.fullmatch(p)]
        if hits:
            shown = ", ".join(hits[:5]) + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else "")
            label = f"{rule.glob} ({rule.reason})" if rule.reason else rule.glob
            reasons.append(f"{label}: {shown}")
    return reasons


# ── Prompts ─────────────────────────────────────────────────────


def pr_body(task: Task) -> str:
    """PR description: the task, its acceptance checklist and the issue link."""
    parts: list[str] = []
    if task.body.strip():
        parts += [task.body.strip(), ""]
    if task.acceptance:
        parts += ["## Acceptance criteria", *(f"- [ ] {a}" for a in task.acceptance), ""]
    if task.issue_number:
        parts += [f"Closes #{task.issue_number}", ""]
    parts.append(f"_Opened by shipcrew for task `{task.id}`._")
    return "\n".join(parts)


_VERDICT_RULE = (
    "Commit on your branch; do not push (shipcrew pushes for you). "
    "End with the final line `PASS` or `FAIL: <reason>`."
)


def ci_fix_prompt(task: Task, checks: gh.ChecksStatus, logs: str, attempt: int) -> str:
    failing = ", ".join(checks.failing) or "unknown"
    lines = [
        f"CI failed on PR #{task.pr_number} (fix attempt {attempt} of {MAX_CI_FIX_ATTEMPTS}).",
        "",
        f"Failing checks: {failing}",
    ]
    if checks.summary:
        lines += ["", checks.summary]
    if logs:
        lines += ["", "Failed job logs (truncated):", "```text", logs, "```"]
    lines += [
        "",
        "Find and fix the cause, then run every CI command locally until it passes.",
        _VERDICT_RULE,
    ]
    return "\n".join(lines)


def review_feedback_prompt(task: Task, review: dict[str, Any], round_no: int) -> str:
    lines = [
        f"The reviewer requested changes on PR #{task.pr_number} "
        f"(round {round_no} of {MAX_REVIEW_ROUNDS}).",
        "",
        f"CHANGES: {review.get('summary') or '(no summary)'}",
    ]
    findings = review.get("findings") or []
    if findings:
        lines += ["", "Findings:"]
        for f in findings:
            where = f["file"] + (f":{f['line']}" if f.get("line") else "")
            lines.append(f"- [{f['severity']}] {where} — {f['message']}")
    lines += ["", "Address every blocker and major finding.", _VERDICT_RULE]
    return "\n".join(lines)


def human_feedback_prompt(message: str) -> str:
    return "\n".join(
        ["A human reviewer requested changes:", "", message.strip(), "", _VERDICT_RULE]
    )


def reviewer_prompt(
    task: Task, *, branch: str, base_ref: str, base_sha: str, head_sha: str, diff_path: str
) -> str:
    lines = [
        f"Review PR #{task.pr_number} for the task below.",
        "",
        f"- Branch `{branch}`, head `{head_sha}`.",
        f"- Base `{base_ref}` at `{base_sha}`.",
        f"- Saved diff snapshot (`git diff {base_ref}...HEAD`): `{diff_path}`",
        f"  If that file is unreadable, run `git diff {base_ref}...HEAD` in this worktree.",
        "",
        "## Task contract",
        f"### {task.title}",
    ]
    if task.body.strip():
        lines += [task.body.strip()]
    if task.acceptance:
        lines += ["", "Acceptance criteria:", *(f"- {a}" for a in task.acceptance)]
    if task.owned_paths:
        lines += ["", "Owned paths: " + ", ".join(f"`{p}`" for p in task.owned_paths)]
    lines += [
        "",
        "## Output format",
        "Write your review, then one fenced JSON block with every finding:",
        "```json",
        '{"findings": [{"file": "src/app.ts", "line": 12, "severity": "blocker", '
        '"message": "why it matters and the fix"}]}',
        "```",
        '`severity` is one of "blocker", "major", "minor"; `line` may be null; use',
        '`{"findings": []}` when there are none. Then the final line: `APPROVE` or',
        "`CHANGES: <one-line summary>`. Nothing after it.",
    ]
    return "\n".join(lines)


def integrator_prompt(task: Task, *, branch: str, base_ref: str) -> str:
    return "\n".join(
        [
            f"PR #{task.pr_number} (branch `{branch}`) conflicts with `{base_ref}`.",
            "",
            f"In this worktree run `git fetch origin && git merge {base_ref}`, resolve every",
            "conflict keeping both sides' behaviour, run every CI command until it passes,",
            "and commit the merge on this branch. Do not push.",
            "",
            f"Task: {task.title}",
            *(f"- {a}" for a in task.acceptance),
            "",
            "Final line: `PASS` or `FAIL: <reason>`.",
        ]
    )


# ── git ─────────────────────────────────────────────────────────


def _git(
    args: Sequence[str], cwd: Path | str, *, check: bool = True, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**session_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args[:2])}: {exc}") from exc
    if check and r.returncode:
        detail = (r.stderr or r.stdout).strip()[-800:]
        raise GitError(f"git {' '.join(args[:2])} failed: {detail}")
    return r


def find_worktree(repo: Path, branch: str) -> Path | None:
    """Path of the worktree checked out on ``branch``, if any."""
    out = _git(["worktree", "list", "--porcelain"], repo).stdout
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}" and path:
            return Path(path)
    return None


def _rev(cwd: Path, ref: str) -> str | None:
    r = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd, check=False)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return (
        _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd, check=False).returncode
        == 0
    )


def _fetch(cwd: Path, *refs: str) -> None:
    _git(["fetch", "--quiet", "origin", *refs], cwd, check=False)


def default_base_ref(repo_path: str, base: str) -> str | None:
    """``origin/<base>`` when the local repo has it (the loop fetches it after
    each merge, so dependants fork from the merged code), else ``None`` (HEAD)."""
    repo = Path(repo_path)
    if not repo.is_dir():
        return None
    try:
        return f"origin/{base}" if _rev(repo, f"origin/{base}") else None
    except GitError:
        return None


# ── The loop ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Ctx:
    task: Task
    mission: Mission
    repo: Path
    branch: str
    worktree: Path | None
    owner: str | None
    base: str

    @property
    def base_ref(self) -> str:
        return f"origin/{self.base}"


def is_loop_hold(task: Task) -> bool:
    """An intervention the loop set (CI/review exhausted, human approval pending)."""
    return (
        task.status == "intervention" and task.pr_number is not None and bool(task.blocked_reason)
    )


def _conflict(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.CONFLICT)


class PrLoop:
    """Drives review cards to merged. See the module docstring for the steps.

    :param service: The board service (store, sessions, event bus, settings).
    """

    def __init__(self, service: ShipcrewService) -> None:
        self._svc = service
        self._merge_locks: dict[str, asyncio.Lock] = {}

    # ── plumbing ──

    def merge_lock(self, repo: Path | str) -> asyncio.Lock:
        """The one lock that serializes merges into a repository."""
        key = str(Path(repo).resolve())
        return self._merge_locks.setdefault(key, asyncio.Lock())

    async def _update(self, task: Task, **fields: Any) -> Task:
        changed = {k: v for k, v in fields.items() if getattr(task, k) != v}
        if not changed:
            return task
        return await self._svc._update(task.id, **changed)

    async def _hold(self, ctx: _Ctx, reason: str, **fields: Any) -> Task:
        """Park the card in ``intervention`` until a human acts."""
        return await self._update(ctx.task, status="intervention", blocked_reason=reason, **fields)

    async def _block(self, ctx: _Ctx, reason: str) -> Task:
        return await self._update(ctx.task, status="blocked", blocked_reason=reason)

    @staticmethod
    async def _io(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _context(self, task: Task) -> _Ctx:
        mission = await self._svc.require_mission(task.mission_id)
        repo = Path(mission.repo_path)
        if not repo.is_dir():
            raise GitError(f"repository {repo} is not on this machine")
        branch = task.branch or task_branch(task.id, task.title)
        worktree = await self._io(find_worktree, repo, branch)
        return _Ctx(
            task=task,
            mission=mission,
            repo=repo,
            branch=branch,
            worktree=worktree,
            owner=mission.owner_user_id,
            base=self._svc.settings.pr_base,
        )

    async def _send(self, ctx: _Ctx, text: str, **fields: Any) -> Task:
        """Hand the developer a new turn: card back to ``running``, then the message."""
        task = await self._update(
            ctx.task, status="running", blocked_reason=None, session_seen_active=False, **fields
        )
        assert task.root_session_id is not None
        try:
            await self._svc.sessions.send_message(
                task.root_session_id, text, acting_user=ctx.owner
            )
        except SessionServiceError as exc:
            return await self._update(
                task, status="blocked", blocked_reason=f"could not message the developer: {exc}"
            )
        return task

    async def _stop(self, ctx: _Ctx, session_id: str | None) -> None:
        if session_id is not None:
            await self._svc._stop_quietly(ctx.task, ctx.owner, session_id)

    def _bundle(self, role: str) -> Path | None:
        agent_dir = self._svc.settings.agents_dir / role
        return agent_dir if (agent_dir / "config.yaml").is_file() else None

    async def _start_child(self, ctx: _Ctx, role: str, title: str, prompt: str) -> str:
        agent_dir = self._bundle(role)
        if agent_dir is None:
            raise SessionServiceError(
                f"no agent bundle for role {role!r} in {self._svc.settings.agents_dir}"
            )
        assert ctx.task.root_session_id is not None and ctx.worktree is not None
        return await self._svc.sessions.create_child_session(
            ChildSessionRequest(
                parent_session_id=ctx.task.root_session_id,
                title=title,
                prompt=prompt,
                workspace=str(ctx.worktree),
                agent_dir=agent_dir,
                acting_user=ctx.owner,
                labels={
                    "shipcrew.task_id": ctx.task.id,
                    "shipcrew.role": role,
                    LOOP_LABEL_KEY: role,
                },
            )
        )

    # ── tick ──

    async def tick(self) -> None:
        """Advance every agent-run review card one step, concurrently.

        Held cards (:func:`is_loop_hold`) are only checked for a PR that a
        human merged or closed on GitHub meanwhile.
        """
        tasks = await self._io(self._svc.store.list_tasks_by_status, {"review", "intervention"})
        steps = [
            self._advance_safely(t)
            for t in tasks
            if t.root_session_id
            and not t.human_assigned
            and (t.status == "review" or is_loop_hold(t))
        ]
        await asyncio.gather(*steps)

    async def _advance_safely(self, task: Task) -> None:
        try:
            await self.advance(task)
        except (gh.GhError, GitError, SessionServiceError, OmnigentError) as exc:
            # Transient (network, gh, a busy worktree): note it and retry next tick.
            _logger.warning("shipcrew pr loop: task %s: %s", task.id, exc)
            current = await self._io(self._svc.store.get_task, task.id)
            if current is not None and current.status == "review":
                await self._update(current, blocked_reason=f"PR loop: {exc}"[:2000])
        except Exception:
            _logger.exception("shipcrew pr loop: task %s crashed", task.id)

    async def advance(self, task: Task) -> Task:
        """Drive one ``review`` card one step through the loop."""
        ctx = await self._context(task)
        if task.pr_number is None:
            return await self._open_pr(ctx)
        pr = await self._io(gh.pr_view, ctx.repo, task.pr_number)
        if pr.state == "MERGED":
            return await self._finish_merged(ctx)
        if pr.state == "CLOSED":
            return await self._block(ctx, f"PR #{task.pr_number} was closed without merging")
        if task.status != "review":
            return task  # a hold waits for a human
        if ctx.worktree is None:
            return await self._hold(ctx, f"worktree of branch {ctx.branch} is missing")
        integrated = False
        if task.integrator_session_id is not None:
            outcome = await self._integrator_outcome(ctx)
            if outcome is None:
                return ctx.task
            ctx = outcome
            if ctx.task.status != "review":
                return ctx.task
            integrated = True
        local = await self._io(_rev, ctx.worktree, "HEAD")
        if local is None:
            raise GitError(f"cannot read HEAD of {ctx.worktree}")
        if local != pr.head_sha:
            return await self._sync_branch(ctx, local, pr.head_sha, integrated=integrated)
        return await self._after_push(ctx, pr)

    async def _after_push(self, ctx: _Ctx, pr: gh.PrState) -> Task:
        task = ctx.task
        assert task.pr_number is not None
        checks = await self._io(gh.pr_checks_status, ctx.repo, task.pr_number)
        if checks.state == "pending":
            return await self._update(task, ci="pending", blocked_reason=None)
        if checks.state == "red":
            return await self._ci_red(ctx, checks)
        task = await self._update(task, ci="green", blocked_reason=None)
        ctx = _replace(ctx, task)
        if not task.human_approved:
            ctx_or_task = await self._review_step(ctx, pr.head_sha)
            if isinstance(ctx_or_task, Task):
                return ctx_or_task
            ctx = ctx_or_task
        policy = await self._policy_step(ctx, pr.head_sha)
        if isinstance(policy, Task):
            return policy
        return await self._merge(policy, pr.head_sha)

    # ── 1. open ──

    async def _developer_verdict(self, ctx: _Ctx) -> tuple[str, str] | None:
        assert ctx.task.root_session_id is not None
        text = await self._svc.sessions.last_agent_text(
            ctx.task.root_session_id, acting_user=ctx.owner
        )
        return parse_verdict(text)

    async def _open_pr(self, ctx: _Ctx) -> Task:
        verdict = await self._developer_verdict(ctx)
        if verdict is None:
            return await self._block(ctx, "developer ended its turn without a PASS/FAIL line")
        if verdict[0] == "fail":
            return await self._block(ctx, f"developer reported FAIL: {verdict[1]}")
        if ctx.worktree is None:
            return await self._block(ctx, f"no worktree on branch {ctx.branch}")
        await self._io(_fetch, ctx.worktree, ctx.base)
        base = ctx.base_ref if await self._io(_rev, ctx.worktree, ctx.base_ref) else ctx.base
        count = await self._io(_git, ["rev-list", "--count", f"{base}..HEAD"], ctx.worktree)
        if int(count.stdout.strip() or 0) == 0:
            return await self._block(
                ctx, f"developer reported PASS but {ctx.branch} has no commits"
            )
        await self._io(
            _git, ["push", "-u", "origin", f"HEAD:refs/heads/{ctx.branch}"], ctx.worktree
        )
        number = await self._io(gh.pr_for_branch, ctx.repo, ctx.branch)
        if number is None:
            number, url = await self._io(
                gh.pr_create,
                ctx.repo,
                title=ctx.task.title,
                body=pr_body(ctx.task),
                head=ctx.branch,
                base=ctx.base,
                draft=True,
            )
        else:
            url = (await self._io(gh.pr_view, ctx.repo, number)).url
        return await self._update(
            ctx.task,
            pr_number=number,
            pr_url=url,
            branch=ctx.branch,
            ci="pending",
            blocked_reason=None,
        )

    # ── 2. sync ──

    async def _sync_branch(self, ctx: _Ctx, local: str, remote: str, *, integrated: bool) -> Task:
        assert ctx.worktree is not None
        task = ctx.task
        await self._io(_fetch, ctx.worktree, ctx.branch)
        # A review of the pre-sync head carries over a merge of main.
        carry = (
            task.review_sha in (local, remote) and (task.review or {}).get("verdict") == "approve"
        )
        if remote and await self._io(_is_ancestor, ctx.worktree, local, remote):
            # GitHub moved the branch (update-branch merged main in): follow it.
            await self._io(_git, ["merge", "--ff-only", "--quiet", remote], ctx.worktree)
            return await self._update(
                task,
                ci="pending",
                blocked_reason=None,
                review_sha=remote if carry else task.review_sha,
            )
        if not integrated:
            verdict = await self._developer_verdict(ctx)
            if verdict is None or verdict[0] != "pass":
                reason = verdict[1] if verdict else "no PASS/FAIL line"
                return await self._block(ctx, f"developer did not pass its fix turn: {reason}")
        push = await self._io(
            _git, ["push", "origin", f"HEAD:refs/heads/{ctx.branch}"], ctx.worktree, check=False
        )
        if push.returncode:
            detail = (push.stderr or push.stdout).strip()[-500:]
            return await self._hold(ctx, f"push of {ctx.branch} rejected: {detail}")
        fields: dict[str, Any] = {"ci": "pending", "blocked_reason": None}
        if integrated:
            # A merge of main keeps the review and the human approval.
            if carry:
                fields["review_sha"] = local
        else:
            fields["human_approved"] = False
        return await self._update(task, **fields)

    # ── 3. CI ──

    async def _ci_red(self, ctx: _Ctx, checks: gh.ChecksStatus) -> Task:
        task = ctx.task
        if task.ci_attempts >= MAX_CI_FIX_ATTEMPTS:
            return await self._hold(
                ctx,
                f"CI still red after {MAX_CI_FIX_ATTEMPTS} fix attempts: "
                + (", ".join(checks.failing) or "failing checks"),
                ci="red",
            )
        logs: list[str] = []
        for run_id in checks.run_ids[:_LOG_RUNS_MAX]:
            log = await self._io(gh.run_failed_log, ctx.repo, run_id)
            if log:
                logs.append(log)
        attempt = task.ci_attempts + 1
        prompt = ci_fix_prompt(task, checks, "\n\n".join(logs), attempt)
        return await self._send(ctx, prompt, ci="red", ci_attempts=attempt)

    # ── 4. review ──

    async def _review_step(self, ctx: _Ctx, head: str) -> _Ctx | Task:
        """``_Ctx`` to go on (approved), or the card after this step."""
        task = ctx.task
        review = task.review or {}
        if task.review_sha != head:
            return await self._start_reviewer(ctx, head)
        if review.get("verdict") is None:
            done = await self._collect_review(ctx, head)
            if isinstance(done, Task):
                return done
            ctx, review = done, done.task.review or {}
        if review.get("verdict") == "approve":
            return ctx
        rounds = ctx.task.review_rounds + 1
        if rounds >= MAX_REVIEW_ROUNDS:
            summary = review.get("summary") or "see the review"
            return await self._hold(
                ctx,
                f"reviewer requested changes {rounds} times: {summary}",
                review_rounds=rounds,
            )
        return await self._send(
            ctx, review_feedback_prompt(ctx.task, review, rounds), review_rounds=rounds
        )

    async def _start_reviewer(self, ctx: _Ctx, head: str) -> Task:
        assert ctx.worktree is not None
        await self._stop(ctx, ctx.task.reviewer_session_id)
        await self._io(_fetch, ctx.worktree, ctx.base)
        base_sha = await self._io(_rev, ctx.worktree, ctx.base_ref) or ""
        diff = await self._io(_git, ["diff", f"{ctx.base_ref}...HEAD"], ctx.worktree, check=False)
        common = (await self._io(_git, ["rev-parse", "--git-common-dir"], ctx.worktree)).stdout
        snapshot_dir = (ctx.worktree / common.strip()).resolve() / "shipcrew" / "reviews"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        diff_path = snapshot_dir / f"{ctx.task.id}-{head[:12]}.diff"
        diff_path.write_text(diff.stdout)
        prompt = reviewer_prompt(
            ctx.task,
            branch=ctx.branch,
            base_ref=ctx.base_ref,
            base_sha=base_sha,
            head_sha=head,
            diff_path=str(diff_path),
        )
        try:
            session_id = await self._start_child(
                ctx, REVIEWER_ROLE, f"Review: {ctx.task.title}", prompt
            )
        except SessionServiceError as exc:
            return await self._hold(ctx, f"could not start the reviewer: {exc}")
        return await self._update(
            ctx.task,
            reviewer_session_id=session_id,
            review_sha=head,
            review={"verdict": None, "summary": "", "findings": []},
            blocked_reason=None,
        )

    async def _collect_review(self, ctx: _Ctx, head: str) -> _Ctx | Task:
        session_id = ctx.task.reviewer_session_id
        if session_id is None:
            return await self._start_reviewer(ctx, head)
        sessions = self._svc.sessions
        snap = await sessions.snapshot(session_id, acting_user=ctx.owner)
        if snap is None:
            return await self._start_reviewer(ctx, head)
        if snap.status == "failed":
            await self._stop(ctx, session_id)
            return await self._hold(ctx, f"reviewer session failed: {snap.error or 'unknown'}")
        if snap.awaiting_human or snap.status != "idle" or not snap.agent_replied:
            return ctx.task
        text = await sessions.last_agent_text(session_id, acting_user=ctx.owner)
        await self._stop(ctx, session_id)
        review = parse_review(text)
        if review is None:
            return await self._hold(ctx, "reviewer ended without an APPROVE / CHANGES line")
        task = await self._update(ctx.task, review=review)
        return _replace(ctx, task)

    # ── 5. policy ──

    async def _approval_rules(self, ctx: _Ctx) -> tuple[ApprovalRule, ...]:
        shown = await self._io(
            _git, ["show", f"{ctx.base_ref}:{APPROVALS_FILE}"], ctx.repo, check=False
        )
        if shown.returncode == 0:
            return parse_approvals(shown.stdout)
        return DEFAULT_APPROVAL_RULES

    async def _policy_step(self, ctx: _Ctx, head: str) -> _Ctx | Task:
        assert ctx.worktree is not None
        await self._io(_fetch, ctx.repo, ctx.base)
        names = await self._io(
            _git, ["diff", "--name-only", f"{ctx.base_ref}...{head}"], ctx.worktree
        )
        changed = [n for n in names.stdout.splitlines() if n.strip()]
        reasons = approval_reasons(changed, await self._approval_rules(ctx))
        task = await self._update(
            ctx.task, needs_human_approval=bool(reasons), approval_reasons=reasons
        )
        if reasons and not task.human_approved:
            return await self._hold(
                _replace(ctx, task), "needs human approval: " + "; ".join(reasons)
            )
        return _replace(ctx, task)

    # ── 6. merge ──

    async def _merge(self, ctx: _Ctx, head: str) -> Task:
        task = ctx.task
        assert task.pr_number is not None and ctx.worktree is not None
        async with self.merge_lock(ctx.repo):
            pr = await self._io(gh.pr_view, ctx.repo, task.pr_number)
            if pr.state == "MERGED":
                return await self._finish_merged(ctx)
            if pr.head_sha != head:
                return task  # moved under us: next tick re-derives
            if pr.is_draft:
                await self._io(gh.pr_ready, ctx.repo, task.pr_number)
            outcome, detail = await self._io(gh.update_branch_status, ctx.repo, task.pr_number)
            if outcome == "conflict":
                return await self._start_integrator(ctx)
            if outcome == "error":
                return await self._update(task, blocked_reason=f"update-branch failed: {detail}")
            after = await self._io(gh.pr_view, ctx.repo, task.pr_number)
            if after.head_sha and after.head_sha != head:
                # main moved in: CI must pass on the updated branch first.
                return await self._sync_branch(ctx, head, after.head_sha, integrated=True)
            merged, detail = await self._io(gh.pr_merge_result, ctx.repo, task.pr_number)
            if not merged:
                return await self._update(task, blocked_reason=f"merge failed: {detail}")
        return await self._finish_merged(ctx)

    async def _start_integrator(self, ctx: _Ctx) -> Task:
        prompt = integrator_prompt(ctx.task, branch=ctx.branch, base_ref=ctx.base_ref)
        try:
            session_id = await self._start_child(
                ctx, INTEGRATOR_ROLE, f"Merge main: {ctx.task.title}", prompt
            )
        except SessionServiceError as exc:
            return await self._hold(
                ctx, f"merge conflict, and the integrator failed to start: {exc}"
            )
        return await self._update(
            ctx.task,
            integrator_session_id=session_id,
            ci="pending",
            blocked_reason="merge conflict with main: integrator working",
        )

    async def _integrator_outcome(self, ctx: _Ctx) -> _Ctx | None:
        """``None`` while the integrator works; else the context after it finished."""
        session_id = ctx.task.integrator_session_id
        assert session_id is not None
        sessions = self._svc.sessions
        snap = await sessions.snapshot(session_id, acting_user=ctx.owner)
        if snap is not None and snap.status != "failed":
            if snap.awaiting_human or snap.status != "idle" or not snap.agent_replied:
                return None
        text = (
            await sessions.last_agent_text(session_id, acting_user=ctx.owner)
            if snap is not None
            else None
        )
        await self._stop(ctx, session_id)
        verdict = parse_verdict(text)
        task = await self._update(ctx.task, integrator_session_id=None, blocked_reason=None)
        if verdict is None or verdict[0] != "pass":
            reason = verdict[1] if verdict else (snap.error if snap else None) or "no verdict"
            task = await self._hold(
                _replace(ctx, task), f"integrator could not merge main: {reason}"
            )
        return _replace(ctx, task)

    async def _finish_merged(self, ctx: _Ctx) -> Task:
        """Stop the task's sessions, remove its worktree, mark it merged."""
        task = ctx.task
        for session_id in (
            task.reviewer_session_id,
            task.integrator_session_id,
            task.root_session_id,
        ):
            await self._stop(ctx, session_id)
        await self._io(_cleanup_worktree, ctx.repo, ctx.worktree, ctx.branch, ctx.base)
        return await self._update(
            task,
            status="merged",
            blocked_reason=None,
            integrator_session_id=None,
            branch=ctx.branch,
        )

    # ── human actions ──

    async def approve(self, task_id: str) -> Task:
        """A human approves the merge (``needs_human_approval`` or a stuck review)."""
        task = await self._svc.require_task(task_id)
        if task.pr_number is None:
            raise _conflict("task has no pull request yet")
        if task.status not in ("intervention", "review"):
            raise _conflict(f"cannot approve a task in status {task.status!r}")
        return await self._update(task, human_approved=True, status="review", blocked_reason=None)

    async def request_changes(self, task_id: str, message: str) -> Task:
        """A human's feedback becomes the developer's next turn."""
        task = await self._svc.require_task(task_id)
        if task.root_session_id is None:
            raise _conflict("task has no agent session")
        if task.status not in ("review", "intervention", "blocked"):
            raise _conflict(f"cannot request changes on a task in status {task.status!r}")
        mission = await self._svc.require_mission(task.mission_id)
        ctx = _Ctx(
            task=task,
            mission=mission,
            repo=Path(mission.repo_path),
            branch=task.branch or task_branch(task.id, task.title),
            worktree=None,
            owner=mission.owner_user_id,
            base=self._svc.settings.pr_base,
        )
        fields: dict[str, Any] = {"human_approved": False, "ci_attempts": 0, "review_rounds": 0}
        if task.reviewer_session_id and (task.review or {}).get("verdict") is None:
            await self._stop(ctx, task.reviewer_session_id)
            fields["review_sha"] = None
        return await self._send(ctx, human_feedback_prompt(message), **fields)


def _replace(ctx: _Ctx, task: Task) -> _Ctx:
    return _Ctx(
        task=task,
        mission=ctx.mission,
        repo=ctx.repo,
        branch=ctx.branch,
        worktree=ctx.worktree,
        owner=ctx.owner,
        base=ctx.base,
    )


def _cleanup_worktree(repo: Path, worktree: Path | None, branch: str, base: str) -> None:
    """Remove the task worktree + local branch; fast-forward a clean local base."""
    if worktree is not None and worktree.exists():
        _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)
    _git(["worktree", "prune"], repo, check=False)
    _git(["branch", "-D", branch], repo, check=False)
    _fetch(repo, base)
    current = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo, check=False)
    clean = _git(["status", "--porcelain", "--untracked-files=no"], repo, check=False)
    if current.stdout.strip() == base and clean.returncode == 0 and not clean.stdout.strip():
        _git(["merge", "--ff-only", "--quiet", f"origin/{base}"], repo, check=False)
