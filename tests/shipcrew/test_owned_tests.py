"""A task owns its own test files (omnigent/shipcrew/owned_tests.py, plan import)."""

from __future__ import annotations

import json

from omnigent.shipcrew.owned_tests import MAX_OWNED_WITH_TESTS, own_test_globs, with_own_tests
from omnigent.shipcrew.planner import parse_plan
from omnigent.shipcrew.policies import owned_paths

ROOT = "/work/mission"


def _write(evaluator, path: str) -> str:  # type: ignore[no-untyped-def]
    event = {"type": "tool_call", "data": {"name": "Write", "arguments": {
        "file_path": f"{ROOT}/{path}", "content": "x"}}}  # fmt: skip
    return evaluator(event, {})["result"]


class TestOwnTestGlobs:
    def test_task_level_globs_use_the_title_slug(self) -> None:
        globs = own_test_globs("Home page!", [])
        assert globs == ["e2e/home-page*.spec.*", "test/home-page*", "tests/home-page*"]

    def test_colocated_next_to_owned_sources(self) -> None:
        owned = with_own_tests("Cart", ["app/cart/page.tsx", "lib/cart.py", "app/ui/*.tsx"])
        assert owned[:3] == ["app/cart/page.tsx", "lib/cart.py", "app/ui/*.tsx"]
        for glob in ("app/cart/page.test.*", "app/cart/page.spec.*", "lib/test_cart.py",
                     "app/ui/*.test.*", "app/ui/__tests__/**"):  # fmt: skip
            assert glob in owned
        deep = with_own_tests("Cart", ["app/cart/**/*.tsx"])
        assert "app/cart/**/*.test.*" in deep

    def test_root_is_never_broadened(self) -> None:
        owned = with_own_tests("Cart", ["*.ts", "app/cart/**", "app", ".github/workflows/ci.yml",
                                        "package.json", "README.md"])  # fmt: skip
        extra = owned[6:]
        assert extra == ["e2e/cart*.spec.*", "test/cart*", "tests/cart*"]
        assert not any(g.startswith(("*", "**")) for g in extra)

    def test_idempotent_and_capped(self) -> None:
        first = with_own_tests("Cart", ["app/cart/page.tsx"])
        assert with_own_tests("Cart", first) == first
        many = [f"app/m{i}/page.tsx" for i in range(100)]
        assert len(with_own_tests("Cart", many)) == MAX_OWNED_WITH_TESTS

    def test_no_contract_stays_empty(self) -> None:
        assert with_own_tests("QA", []) == []


class TestPlanImport:
    PLAN = {"tasks": [{"key": "T02", "title": "Home page", "role": "developer",
                       "owned_paths": ["app/page.tsx", "app/home/**"]}]}  # fmt: skip

    def test_imported_task_owns_its_e2e_spec(self) -> None:
        (task,) = parse_plan(json.dumps(self.PLAN))
        assert task.owned_paths[:2] == ["app/page.tsx", "app/home/**"]
        # Parsing is pure: a re-import yields the same list.
        assert parse_plan(json.dumps(self.PLAN))[0].owned_paths == task.owned_paths
        guard = owned_paths(owned_paths=task.owned_paths, root=ROOT)
        assert _write(guard, "e2e/home-page.spec.ts") == "ALLOW"
        assert _write(guard, "app/page.test.tsx") == "ALLOW"
        assert _write(guard, "tests/home-page.test.ts") == "ALLOW"
        # Not another task's tests, not the rest of the repo.
        assert _write(guard, "e2e/cart.spec.ts") == "ASK"
        assert _write(guard, "lib/db.ts") == "ASK"
        assert _write(guard, "package.json") == "ASK"
