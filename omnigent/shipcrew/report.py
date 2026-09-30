"""The mission ship report: deterministic Markdown built by the server, not an LLM.

Stored in ``Mission.ship_report_md`` when a ship ends (done or failed) and shown
on the board; it is never committed to the repository. Same inputs, same text:
every timestamp comes from the stored mission and tasks.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from omnigent.shipcrew.store import Mission, Task

_OPEN_SEVERITIES = ("blocker", "major")
_MAX_CI = 3


def _cell(text: object) -> str:
    """One Markdown table cell: no pipe or newline can break the row."""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _inline(text: object) -> str:
    return " ".join(str(text).split())


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _stamp(epoch: float | None) -> str:
    if epoch is None:
        return "n/a"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def format_duration(seconds: float) -> str:
    """``3725`` -> ``"1h 02m 05s"``; ``65`` -> ``"1m 05s"``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _review(task: Task) -> str:
    if task.review is None:
        return "-"
    verdict = task.review.get("verdict")
    if verdict is None:
        return "in progress"
    return str(verdict)


def _pr(task: Task) -> str:
    if task.pr_number is None:
        return "-"
    return f"[#{task.pr_number}]({task.pr_url})" if task.pr_url else f"#{task.pr_number}"


def _role(task: Task) -> str:
    if task.human_assigned and task.assignee is not None:
        return f"{task.role} (human: {task.assignee.id})"
    return task.role


def wall_time_s(mission: Mission, tasks: Sequence[Task]) -> float | None:
    """First task start -> ship end (seconds), or ``None`` without both ends."""
    starts = [t.started_at for t in tasks if t.started_at is not None]
    end = mission.ship_finished_at
    if not starts or end is None:
        return None
    return max(0.0, end - min(starts))


_BUILD_STEPS = (
    ("install_s", "install"),
    ("app_build_s", "app build"),
    ("prepare_s", "context"),
    ("package_s", "image"),
)


def _build_steps(detail: dict[str, object]) -> str:
    """``host-standalone: install 2 s, app build 12 s, image 5 s`` (empty when unknown)."""
    steps = [
        f"{label} {format_duration(float(value))}"
        for key, label in _BUILD_STEPS
        if isinstance(value := detail.get(key), int | float)
    ]
    mode = detail.get("build_mode")
    if isinstance(mode, str) and steps:
        return f"{mode}: {', '.join(steps)}"
    return ", ".join(steps)


def build_report(mission: Mission, tasks: Sequence[Task]) -> str:
    """The Markdown ship report of ``mission`` (see the module docstring)."""
    lines: list[str] = [f"# Ship report: {_inline(mission.title)}", ""]
    shipped = mission.ship_status == "done"
    preview = mission.preview or {}
    live_url = preview.get("url") if preview.get("status") == "live" else None
    if mission.ship_url:
        lines.append(f"- **Deployment:** {mission.ship_url}")
    elif live_url:
        lines.append(f"- **Deployment:** {live_url} (live preview)")
    else:
        lines.append("- **Deployment:** not deployed")
    if preview.get("live_since"):
        lines.append(f"- **Live since:** {_stamp(float(preview['live_since']))} (first deploy)")
    if preview.get("sha"):
        detail = preview.get("detail") or {}
        facts = [f"commit `{str(preview['sha'])[:12]}`"]
        if preview.get("target"):
            facts.insert(0, str(preview["target"]))
        for key, label in (("build_s", "build"), ("start_s", "start"), ("deploy_s", "deploy")):
            if isinstance(detail.get(key), int | float):
                facts.append(f"{label} {format_duration(float(detail[key]))}")
            if key == "build_s" and (steps := _build_steps(detail)):
                facts[-1] += f" ({steps})"
        if isinstance(detail.get("image_mb"), int | float):
            facts.append(f"image {detail['image_mb']} MB")
        lines.append(
            f"- **Last deploy:** {', '.join(facts)} at {_stamp(preview.get('updated_at'))}"
        )
    lines.append(f"- **Repository:** {mission.repo_url or mission.repo_path}")
    if shipped:
        lines.append("- **Status:** Shipped")
    elif mission.ship_status == "failed":
        error = _inline(mission.ship_error or "unknown error")
        lines.append(f"- **Status:** Ship failed: {error}")
    else:
        lines.append(f"- **Status:** {mission.ship_status}")
    if mission.ship_note:
        lines.append(f"- **Note:** {_inline(mission.ship_note)}")
    task_cost = sum(t.cost_usd for t in tasks)
    total = task_cost + mission.ship_cost_usd
    lines.append(
        f"- **Total cost:** {_money(total)} "
        f"(tasks {_money(task_cost)}, deploy {_money(mission.ship_cost_usd)})"
    )
    wall = wall_time_s(mission, tasks)
    wall_text = f"{format_duration(wall)} (first task start to ship end)" if wall else "n/a"
    lines.append(f"- **Wall time:** {wall_text}")
    per_policy: dict[str, int] = {}
    for task in tasks:
        for entry in task.interventions:
            key = str(entry.get("policy") or "other")
            per_policy[key] = per_policy.get(key, 0) + 1
    from_children = sum(1 for t in tasks for e in t.interventions if e.get("role"))
    if per_policy:
        counts = ", ".join(
            f"{_inline(name)} {n}" for name, n in sorted(per_policy.items(), key=lambda i: -i[1])
        )
        children = f"; {from_children} in reviewer/integrator sessions" if from_children else ""
        lines.append(f"- **Human interventions:** {sum(per_policy.values())} ({counts}{children})")
    else:
        lines.append("- **Human interventions:** 0")
    lines.append("")

    merged = sum(1 for t in tasks if t.status == "merged")
    lines += [
        f"## Tasks ({merged} of {len(tasks)} merged)",
        "",
        "| # | Task | Role | Status | PR | CI fixes | Review | Cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, task in enumerate(tasks, 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(i),
                    _cell(task.title),
                    _cell(_role(task)),
                    _cell(task.status),
                    _pr(task),
                    f"{task.ci_attempts}/{_MAX_CI}",
                    _cell(_review(task)),
                    _money(task.cost_usd),
                ]
            )
            + " |"
        )
    if not tasks:
        lines.append("| - | no tasks | | | | | | |")
    lines.append("")

    lines += ["## Decisions", ""]
    groups: list[tuple[str, list[str]]] = [("Plan", mission.plan_decisions)]
    groups += [(t.title, t.decisions) for t in tasks]
    groups.append(("Deploy", mission.ship_decisions))
    any_decision = False
    for heading, items in groups:
        if not items:
            continue
        any_decision = True
        lines += [f"### {_inline(heading)}", "", *(f"- {_inline(d)}" for d in items), ""]
    if not any_decision:
        lines += ["_No decisions were reported._", ""]

    lines += ["## Open review findings (major and blocker)", ""]
    open_findings = 0
    for task in tasks:
        for finding in (task.review or {}).get("findings") or []:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "")
            if severity not in _OPEN_SEVERITIES:
                continue
            where = str(finding.get("file") or "")
            if finding.get("line"):
                where += f":{finding['line']}"
            where_md = f" `{where}`" if where else ""
            lines.append(
                f"- **{_inline(task.title)}**{where_md} [{severity}] "
                f"{_inline(finding.get('message') or '')}"
            )
            open_findings += 1
    if not open_findings:
        lines.append("_None._")
    lines.append("")

    lines += ["## Security", ""]
    security = [t for t in tasks if t.role == "security"]
    for task in security:
        parts = [f"status {task.status}"]
        if task.pr_number is not None:
            parts.append(f"PR {_pr(task)}")
        if task.review is not None:
            parts.append(f"review {_review(task)}")
            summary = _inline((task.review or {}).get("summary") or "")
            if summary:
                parts.append(summary)
        if task.blocked_reason and task.status != "merged":
            parts.append(_inline(task.blocked_reason))
        lines.append(f"- **{_inline(task.title)}**: " + "; ".join(parts))
        lines += [f"  - {_inline(d)}" for d in task.decisions]
    if not security:
        lines.append("_No security task in this mission._")
    lines.append("")

    lines += ["## Human interventions", ""]
    count = 0
    for task in tasks:
        for entry in task.interventions:
            at = entry.get("at")
            when = _stamp(float(at)) if isinstance(at, int | float) else "n/a"
            policy, preview = entry.get("policy"), entry.get("preview")
            what = (
                f"{_inline(policy)}: {_inline(preview)}"
                if policy and preview
                else _inline(entry.get("reason") or "")
            )
            role = entry.get("role")
            where = f"{when}, {_inline(role)}" if role else when
            lines.append(f"- **{_inline(task.title)}** ({where}): {what}")
            count += 1
    if not count:
        lines.append("_None: the crew needed no human._")
    lines.append("")

    lines.append(f"_Generated by shipcrew at {_stamp(mission.ship_finished_at)}._")
    return "\n".join(lines) + "\n"
