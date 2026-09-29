"""Pure gate functions: deps, capacity, overlap, budget."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.shipcrew.gates import (
    GateContext,
    budget_gate,
    capacity_gate,
    deps_gate,
    evaluate_gates,
    find_cycle,
    globs_overlap,
    overlap_gate,
    paths_overlap,
)
from omnigent.shipcrew.store import Task


def _t(tid: str, mission_id: str = "m1", **kw: Any) -> Task:
    return Task(id=tid, mission_id=mission_id, title=tid, **kw)


class TestDepsGate:
    def test_passes_without_dependencies(self) -> None:
        assert deps_gate(_t("a"), {}) is None

    def test_passes_when_all_merged(self) -> None:
        tasks = {"d1": _t("d1", status="merged"), "d2": _t("d2", status="merged")}
        assert deps_gate(_t("a", depends_on=["d1", "d2"]), tasks) is None

    def test_blocks_on_unmerged_dependency(self) -> None:
        tasks = {"d1": _t("d1", status="merged"), "d2": _t("d2", status="review")}
        reason = deps_gate(_t("a", depends_on=["d1", "d2"]), tasks)
        assert reason == "waiting on dependencies: d2"

    def test_blocks_on_unknown_dependency(self) -> None:
        assert deps_gate(_t("a", depends_on=["ghost"]), {}) == "unknown dependencies: ghost"


class TestCapacityGate:
    def test_under_cap(self) -> None:
        assert capacity_gate(3, 4) is None

    def test_at_cap(self) -> None:
        assert capacity_gate(4, 4) == "capacity: 4/4 agents running"


class TestBudgetGate:
    def test_under_budget(self) -> None:
        assert budget_gate(9.99, 10.0) is None

    def test_at_budget(self) -> None:
        assert budget_gate(10.0, 10.0) == "budget: $10.00 spent of $10.00"

    def test_unlimited(self) -> None:
        assert budget_gate(1e9, None) is None


class TestGlobOverlap:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("src/api", "src/api/users.py"),  # directory owns its contents
            ("src/api/users.py", "./src/api/users.py"),
            ("src/**", "src/web/app.ts"),
            ("src/*.ts", "src/**/*.ts"),
            ("web/src/**", "web"),
            ("*.md", "docs/readme.md"),  # wildcard at root may reach anything
            ("", "anything/at/all"),  # empty literal = whole repo
        ],
    )
    def test_overlapping(self, a: str, b: str) -> None:
        assert globs_overlap(a, b)
        assert globs_overlap(b, a)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("src/api", "src/apiv2"),
            ("src/api/**", "src/web/**"),
            ("web/src/board/**", "omnigent/shipcrew/**"),
            ("docs/readme.md", "src/*.ts"),
            ("README.md", "LICENSE"),
        ],
    )
    def test_disjoint(self, a: str, b: str) -> None:
        assert not globs_overlap(a, b)
        assert not globs_overlap(b, a)

    def test_empty_owned_paths_overlap_everything(self) -> None:
        assert paths_overlap([], ["src/**"])
        assert paths_overlap(["src/**"], [])

    def test_lists_overlap_when_any_pair_does(self) -> None:
        assert paths_overlap(["a/**", "b/x.py"], ["c/**", "b/**"])
        assert not paths_overlap(["a/**"], ["c/**", "d/e.py"])


class TestOverlapGate:
    def test_blocks_on_running_task_with_shared_path(self) -> None:
        running = _t("r", status="running", owned_paths=["src/**"])
        task = _t("a", owned_paths=["src/app.py"])
        reason = overlap_gate(task, [running], {"m1": "/repo"})
        assert reason == "owned paths overlap with running task: r"

    def test_other_repository_never_collides(self) -> None:
        running = _t("r", mission_id="m2", status="running", owned_paths=["src/**"])
        task = _t("a", owned_paths=["src/app.py"])
        assert overlap_gate(task, [running], {"m1": "/repo", "m2": "/other"}) is None

    def test_same_repo_across_missions_collides(self) -> None:
        running = _t("r", mission_id="m2", status="running", owned_paths=["src/**"])
        task = _t("a", owned_paths=["src/app.py"])
        assert overlap_gate(task, [running], {"m1": "/repo", "m2": "/repo"}) is not None

    def test_disjoint_paths_pass(self) -> None:
        running = _t("r", status="running", owned_paths=["web/**"])
        assert overlap_gate(_t("a", owned_paths=["api/**"]), [running], {"m1": "/r"}) is None


class TestEvaluateGates:
    def _ctx(
        self, *tasks: Task, max_parallel: int = 4, max_usd: float | None = 10.0
    ) -> GateContext:
        return GateContext(
            tasks_by_id={t.id: t for t in tasks},
            repo_of={"m1": "/repo"},
            max_parallel=max_parallel,
            max_usd=max_usd,
        )

    def test_all_gates_pass(self) -> None:
        dep = _t("d", status="merged")
        task = _t("a", status="ready", depends_on=["d"], owned_paths=["x/**"])
        assert evaluate_gates(task, self._ctx(dep, task)) is None

    def test_capacity_counts_running_and_intervention(self) -> None:
        r1 = _t("r1", status="running", owned_paths=["p1/**"])
        r2 = _t("r2", status="intervention", owned_paths=["p2/**"])
        task = _t("a", status="ready", owned_paths=["p3/**"])
        reason = evaluate_gates(task, self._ctx(r1, r2, task, max_parallel=2))
        assert reason == "capacity: 2/2 agents running"

    def test_budget_sums_every_task(self) -> None:
        done = _t("done", status="merged", cost_usd=7.5, owned_paths=["p1/**"])
        rev = _t("rev", status="review", cost_usd=2.5, owned_paths=["p2/**"])
        task = _t("a", status="ready", owned_paths=["p3/**"])
        assert evaluate_gates(task, self._ctx(done, rev, task)) == "budget: $10.00 spent of $10.00"


class TestFindCycle:
    def test_self_dependency(self) -> None:
        assert find_cycle("a", ["a"], {})

    def test_transitive_cycle(self) -> None:
        tasks = {"b": _t("b", depends_on=["c"]), "c": _t("c", depends_on=["a"])}
        assert find_cycle("a", ["b"], tasks)

    def test_dag(self) -> None:
        tasks = {"b": _t("b", depends_on=["c"]), "c": _t("c")}
        assert not find_cycle("a", ["b", "c"], tasks)
