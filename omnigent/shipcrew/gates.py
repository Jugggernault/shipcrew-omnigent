"""The scheduler's four start gates, as pure functions.

A ready task starts only when every gate passes:

1. **deps** — every ``depends_on`` task is ``merged``;
2. **capacity** — fewer than ``max_parallel`` tasks are agent-active;
3. **overlap** — no active task in the same repo owns an overlapping path;
4. **budget** — the summed task cost of the task's mission is below ``max_usd``.

Each gate returns ``None`` when it passes, or a human-readable reason that the
scheduler writes to ``blocked_reason``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from omnigent.shipcrew.store import ACTIVE_STATUSES, Task

_WILDCARD = re.compile(r"[*?\[]")


def deps_gate(task: Task, tasks_by_id: Mapping[str, Task]) -> str | None:
    """Pass when every dependency exists and is merged."""
    missing = [d for d in task.depends_on if d not in tasks_by_id]
    if missing:
        return f"unknown dependencies: {', '.join(missing)}"
    pending = [d for d in task.depends_on if tasks_by_id[d].status != "merged"]
    if pending:
        titles = ", ".join(tasks_by_id[d].title or d for d in pending)
        return f"waiting on dependencies: {titles}"
    return None


def capacity_gate(running_count: int, max_parallel: int) -> str | None:
    """Pass while ``running_count`` (recomputed from the DB) is under the cap."""
    if running_count >= max_parallel:
        return f"capacity: {running_count}/{max_parallel} agents running"
    return None


def budget_gate(spent_usd: float, max_usd: float | None) -> str | None:
    """Pass while total spend is under ``max_usd`` (``None`` = unlimited)."""
    if max_usd is not None and spent_usd >= max_usd:
        return f"budget: ${spent_usd:.2f} spent of ${max_usd:.2f}"
    return None


def _norm(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/").rstrip("/")


def _literal_prefix(glob: str) -> str:
    match = _WILDCARD.search(glob)
    return glob if match is None else glob[: match.start()]


def _is_dir_prefix(parent: str, child: str) -> bool:
    """``parent`` is ``child`` or one of its ancestor directories."""
    return parent == "" or child == parent or child.startswith(parent + "/")


def globs_overlap(a: str, b: str) -> bool:
    """Whether two owned-path globs may claim a common file.

    Conservative: a directory owns everything below it, and two wildcard
    globs overlap unless their literal prefixes diverge.
    """
    a, b = _norm(a), _norm(b)
    a_wild, b_wild = bool(_WILDCARD.search(a)), bool(_WILDCARD.search(b))
    if not a_wild and not b_wild:
        return _is_dir_prefix(a, b) or _is_dir_prefix(b, a)
    if a_wild and b_wild:
        pa, pb = _literal_prefix(a), _literal_prefix(b)
        return pa.startswith(pb) or pb.startswith(pa)
    literal, glob = (a, b) if b_wild else (b, a)
    prefix = _literal_prefix(glob)
    # Overlap when the literal is a directory above the glob's root, or a path
    # the glob's wildcard part could reach (any file or directory it matches).
    return _is_dir_prefix(literal, prefix.rstrip("/")) or literal.startswith(prefix)


def paths_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether two owned-path lists may collide. An empty list owns everything."""
    if not a or not b:
        return True
    return any(globs_overlap(x, y) for x in a for y in b)


def overlap_gate(
    task: Task,
    active: Iterable[Task],
    repo_of: Mapping[str, str],
) -> str | None:
    """Pass when no active task in the same repository owns an overlapping path.

    :param repo_of: Mission id -> repository path, to scope collisions per repo.
    """
    repo = repo_of.get(task.mission_id)
    for other in active:
        if other.id == task.id or repo_of.get(other.mission_id) != repo:
            continue
        if paths_overlap(task.owned_paths, other.owned_paths):
            return f"owned paths overlap with unmerged task: {other.title or other.id}"
    return None


@dataclass(frozen=True)
class GateContext:
    """A DB snapshot the gates are evaluated against."""

    tasks_by_id: Mapping[str, Task]
    repo_of: Mapping[str, str]
    max_parallel: int
    max_usd: float | None

    @property
    def active(self) -> list[Task]:
        """In-flight tasks, whoever works them (they all own their paths)."""
        return [t for t in self.tasks_by_id.values() if t.status in ACTIVE_STATUSES]

    @property
    def path_holders(self) -> list[Task]:
        """Tasks whose unmerged changes own their paths: in flight or in review."""
        return [t for t in self.tasks_by_id.values() if t.status in ACTIVE_STATUSES | {"review"}]

    @property
    def agent_active(self) -> list[Task]:
        """In-flight tasks that hold an agent slot (a human took the others)."""
        return [t for t in self.active if not t.human_assigned]

    @property
    def spent_usd(self) -> float:
        return sum(t.cost_usd for t in self.tasks_by_id.values())

    def mission_spent_usd(self, mission_id: str) -> float:
        """Spend of one mission: the budget is per mission, not across all of them."""
        return sum(t.cost_usd for t in self.tasks_by_id.values() if t.mission_id == mission_id)


def evaluate_gates(task: Task, ctx: GateContext) -> str | None:
    """First failing gate's reason, or ``None`` when the task may start."""
    return (
        deps_gate(task, ctx.tasks_by_id)
        or capacity_gate(len(ctx.agent_active), ctx.max_parallel)
        or overlap_gate(task, ctx.path_holders, ctx.repo_of)
        or budget_gate(ctx.mission_spent_usd(task.mission_id), ctx.max_usd)
    )


def find_cycle(task_id: str, depends_on: Sequence[str], tasks_by_id: Mapping[str, Task]) -> bool:
    """Whether giving ``task_id`` these ``depends_on`` would create a cycle."""
    stack = list(depends_on)
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        dep = tasks_by_id.get(current)
        if dep is not None:
            stack.extend(dep.depends_on)
    return False
