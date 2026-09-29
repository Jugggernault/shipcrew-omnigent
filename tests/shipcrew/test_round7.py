"""Round 7: refusals with a hint instead of approval cards, inherited tests, merge memory.

* a complex ``sed -i`` / ``perl -pi`` is DENY with a "use the Edit tool" hint;
* a write to another in-progress task's file is DENY with a "work against the
  contract" hint (the server injects the other tasks at session start);
* a task inherits the existing tests that only import its own modules;
* a write outside owned paths a human accepted is not held again at merge;
* ``pnpm -s lint`` / ``npm run --silent typecheck`` match their plain entries.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from omnigent.server.routes.sessions.routes_hooks import _notify_shipcrew_approval
from omnigent.shipcrew.approvals import APP_STATE_HOOK, approved_write_paths
from omnigent.shipcrew.inherited_tests import (
    grant_inherited_tests,
    grantable_tests,
    import_specifiers,
)
from omnigent.shipcrew.policies import (
    IN_PLACE_EDIT_HINT,
    OTHER_TASK_HINT,
    _normalize_program,
    owned_paths,
    shell_allowlist,
)
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import inject_task_contract

from .conftest import FakeSessions

ROOT = "/wt/task"
FS_EDIT = ["@sed:sed_in_place", "@perl:perl_in_place"]
BUILDER_ALLOW = ["cat", "ls", "npm test", "pnpm lint", "pnpm run *", "npm run *", *FS_EDIT]
_RANK = {"ALLOW": 0, "ASK": 1, "DENY": 2}
OWNER = "alice@example.com"


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


def _write(path: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Write", "arguments": {"file_path": path}}}


def _worst(policies: list[Any], event: dict[str, Any]) -> dict[str, Any]:
    worst: dict[str, Any] = {"result": "ALLOW"}
    for fn in policies:
        out = fn(event, {})
        if _RANK[out["result"]] > _RANK[worst["result"]]:
            worst = out
    return worst


def _builder(owned: list[str], others: list[dict[str, Any]] | None = None) -> list[Any]:
    return [
        shell_allowlist(allow=BUILDER_ALLOW, role="builder"),
        owned_paths(owned_paths=owned, root=ROOT, other_tasks=others),
    ]


# ── 1. complex in-place edits are refused with a hint ─────────────


class TestInPlaceEditHint:
    @pytest.mark.parametrize(
        "command",
        [
            "sed -i '/re/,+1d' app/page.tsx",
            "sed -i '/re/d' app/page.tsx",
            "sed -i 's/a/b/; s/c/d/' app/page.tsx",
            "sed -i -e 's/a/b/' -e 's/c/d/' app/page.tsx",
            "sed -i -E 's/(x)/\\1\\n  y/' app/page.tsx",  # a newline in the replacement
            "sed -i 's/a/b/\ns/c/d/' app/page.tsx",
            "sed -i '3a extra line' app/page.tsx",
            "sed -i.bak 's/a/b/' app/page.tsx",
            "perl -pi -e 's/a/b/; s/c/d/' app/page.tsx",
            "perl -0pi -e 's/a\\n/b/' app/page.tsx",
            'sed -i "s/a/$REPL/" app/page.tsx',
        ],
    )
    def test_denied_with_the_edit_hint(self, command: str) -> None:
        out = _worst(_builder(["app/**"]), _bash(command))
        assert out["result"] == "DENY", command
        assert IN_PLACE_EDIT_HINT in out["reason"]

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/g' app/page.tsx",
            "sed -i -E 's/(a+)/\\1b/' app/page.tsx",
            'sed -i \'s|"test": "vitest"|"test": "vitest run"|\' app/x.json',
            "sed -i '3s/a/b/' app/page.tsx",
            "sed -i -e 's/a\\/b/c/' app/page.tsx",
            "perl -pi -e 's/a/$1b/g' app/page.tsx",
        ],
    )
    def test_simple_substitution_still_allowed(self, command: str) -> None:
        assert _worst(_builder(["app/**"]), _bash(command))["result"] == "ALLOW", command

    def test_simple_substitution_outside_owned_still_asks(self) -> None:
        assert _worst(_builder(["app/**"]), _bash("sed -i 's/a/b/' lib/x.ts"))["result"] == "ASK"

    def test_role_without_in_place_edits_keeps_asking(self) -> None:
        reader = shell_allowlist(allow=["cat", "ls"], shell_writes=False, role="reviewer")
        assert reader(_bash("sed -i '/re/d' app/page.tsx"), {})["result"] == "ASK"

    def test_unanalyzable_command_still_asks(self) -> None:
        cmd = 'sed -i "s/a/$(whoami)/" app/page.tsx'
        assert _worst(_builder(["app/**"]), _bash(cmd))["result"] == "ASK"


# ── 2. another task's files are refused with a hint ───────────────


OTHERS = [
    {"title": "Polls API", "owned_paths": ["app/api/polls/**", "test/polls-api*"]},
    {"title": "Foundation", "owned_paths": ["package.json"]},
]


class TestOtherTasksFiles:
    def test_write_to_another_tasks_file_is_denied(self) -> None:
        out = _worst(_builder(["app/polls/**"], OTHERS), _write(f"{ROOT}/app/api/polls/route.ts"))
        assert out["result"] == "DENY"
        owner = "`app/api/polls/route.ts` belongs to task 'Polls API' (in progress)"
        assert owner in out["reason"]
        assert OTHER_TASK_HINT in out["reason"]
        assert "lib/api-client.ts" in out["reason"]

    def test_own_files_and_unowned_files_are_unchanged(self) -> None:
        fns = _builder(["app/polls/**"], OTHERS)
        assert _worst(fns, _write(f"{ROOT}/app/polls/page.tsx"))["result"] == "ALLOW"
        assert _worst(fns, _write(f"{ROOT}/lib/db.ts"))["result"] == "ASK"  # nobody's: a human

    def test_a_shared_file_another_task_owns_is_denied(self) -> None:
        out = _worst(_builder(["app/polls/**"], OTHERS), _write(f"{ROOT}/package.json"))
        assert out["result"] == "DENY" and "'Foundation'" in out["reason"]

    def test_owning_it_too_wins(self) -> None:
        fns = _builder(["app/api/polls/route.ts"], OTHERS)
        assert _worst(fns, _write(f"{ROOT}/app/api/polls/route.ts"))["result"] == "ALLOW"

    def test_shell_writes_deny_first(self) -> None:
        cmd = "touch lib/x.ts app/api/polls/route.ts"
        assert _worst(_builder(["app/polls/**"], OTHERS), _bash(cmd))["result"] == "DENY"

    def test_no_snapshot_keeps_asking(self) -> None:
        out = _worst(_builder(["app/polls/**"]), _write(f"{ROOT}/app/api/polls/route.ts"))
        assert out["result"] == "ASK"

    def test_injected_into_the_bundle_slot(self) -> None:
        config = (
            "guardrails:\n  policies:\n    shipcrew_owned_paths:\n      function:\n"
            "        path: omnigent.shipcrew.policies.owned_paths\n        arguments:\n"
            "          owned_paths: []  # @task.owned_paths\n"
            '          root: ""  # @task.root\n'
            "          other_tasks: []  # @task.other_tasks\n"
        )
        text = inject_task_contract(
            config, owned_paths=["app/polls/**"], root=ROOT,
            other_tasks=[*OTHERS, {"title": "Empty", "owned_paths": []}],
        )  # fmt: skip
        args = yaml.safe_load(text)["guardrails"]["policies"]["shipcrew_owned_paths"]["function"][
            "arguments"
        ]
        assert args["other_tasks"] == OTHERS
        policy = owned_paths(**args)
        assert policy(_write(f"{ROOT}/app/api/polls/x.ts"), {})["result"] == "DENY"
        # a bundle without the slot is unchanged there (it keeps asking)
        old = config.replace("          other_tasks: []  # @task.other_tasks\n", "")
        assert "other_tasks" not in inject_task_contract(
            old, owned_paths=["a/**"], root=ROOT, other_tasks=OTHERS
        )

    async def test_start_passes_the_other_active_tasks(
        self, service: ShipcrewService, sessions: FakeSessions
    ) -> None:
        mission = await service.create_mission("M", "/repo", None, OWNER)
        specs = {
            "Polls API": ("ready", ["app/api/polls/**"]),
            "Results": ("review", ["app/results/**"]),
            "Done": ("merged", ["app/done/**"]),
            "Later": ("backlog", ["app/later/**"]),
            "No contract": ("running", []),
        }
        for title, (status, owned) in specs.items():
            t = await service.create_task(mission.id, title=title, owned_paths=owned)
            await asyncio.to_thread(service.store.update_task, t.id, status=status)
        me = await service.create_task(mission.id, title="Poll page", owned_paths=["app/polls/**"])
        await service.start_task(me.id, OWNER)
        (req,) = sessions.created
        assert sorted(o["title"] for o in req.other_tasks) == ["Polls API", "Results"]
        assert {"title": "Polls API", "owned_paths": ["app/api/polls/**"]} in req.other_tasks


# ── 3. inherited tests ────────────────────────────────────────────


FILES = {
    "app/api/polls/route.ts": "export async function GET() {}\n",
    "lib/db.ts": "export const db = 1\n",
    "lib/types.ts": "export type Poll = {}\n",
    "app/page.tsx": "export default function Home() {}\n",
}


class TestInheritedTests:
    def test_import_specifiers(self) -> None:
        src = (
            'import { GET } from "@/app/api/polls/route";\n'
            "import type { Poll } from '../lib/types'\n"
            'import {\n  a,\n  b,\n} from "./x";\n'
            'import "side-effect";\n'
            'export * from "../y";\n'
            'const z = require("z");\n'
            'const w = await import("@/lib/w");\n'
            'import { test, expect } from "@playwright/test";\n'
        )
        assert import_specifiers(src) == [
            "@/app/api/polls/route", "../lib/types", "./x", "side-effect", "../y", "z",
            "@/lib/w", "@playwright/test",
        ]  # fmt: skip

    def test_grants_a_test_of_owned_modules_only(self) -> None:
        tests = {
            # only the route this task owns (plus packages): granted
            "test/foundation-api.test.ts": (
                'import { describe, it } from "vitest";\n'
                'import { GET } from "@/app/api/polls/route";\n'
            ),
            "app/api/polls/route.test.ts": 'import { GET } from "./route";\n',
            # also imports a shared contract module: not granted
            "test/api-and-db.test.ts": (
                'import { GET } from "@/app/api/polls/route";\nimport { db } from "@/lib/db";\n'
            ),
            # no app import at all (an e2e spec visiting URLs): not granted
            "e2e/foundation.spec.ts": 'import { test } from "@playwright/test";\n',
            # a module of another page: not granted
            "test/home.test.tsx": 'import Home from "../app/page";\n',
            # owned by another active task: skipped
            "test/polls-other.test.ts": 'import { GET } from "@/app/api/polls/route";\n',
            # a bare repo-dir import resolves too
            "tests/bare.test.ts": 'import { GET } from "app/api/polls/route";\n',
            # not a test file
            "app/api/polls/helper.ts": 'import { GET } from "./route";\n',
        }
        granted = grantable_tests(
            tests,
            ["app/api/polls/**"],
            ["test/polls-other*"],
            all_files=[*FILES, *tests],
        )
        assert granted == ["test/foundation-api.test.ts", "tests/bare.test.ts"]

    def test_module_not_created_yet_matches_by_glob(self) -> None:
        tests = {"test/cart.test.ts": 'import { add } from "@/lib/cart";\n'}
        assert grantable_tests(tests, ["lib/cart.ts"], all_files=list(tests)) == [
            "test/cart.test.ts"
        ]
        assert grantable_tests(tests, ["lib/other.ts"], all_files=list(tests)) == []

    def test_scans_the_base_tree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        files = {
            **FILES,
            "test/foundation-api.test.ts": 'import { GET } from "@/app/api/polls/route";\n',
            "test/db.test.ts": 'import { db } from "@/lib/db";\n',
        }
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base")
        # an uncommitted test is not on the base: not seen
        (repo / "test" / "wip.test.ts").write_text('import { GET } from "@/app/api/polls/route";')
        assert grant_inherited_tests(str(repo), "main", ["app/api/polls/**"]) == [
            "test/foundation-api.test.ts"
        ]
        assert grant_inherited_tests(str(repo), "no-such-ref", ["app/api/polls/**"]) == []
        assert grant_inherited_tests(str(repo), "main", []) == []

    async def test_start_grants_the_tests(
        self, service: ShipcrewService, sessions: FakeSessions, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "test").mkdir(parents=True)
        (repo / "test" / "foundation-api.test.ts").write_text(
            'import { GET } from "@/app/api/polls/route";\n'
        )
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base")
        mission = await service.create_mission("M", str(repo), None, OWNER)
        task = await service.create_task(mission.id, title="Polls API", owned_paths=["app/api/**"])
        started = await service.start_task(task.id, OWNER)
        assert started.owned_paths == ["app/api/**", "test/foundation-api.test.ts"]
        (req,) = sessions.created
        assert "test/foundation-api.test.ts" in req.owned_paths


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


# ── 4. accepted owned-paths asks are remembered ───────────────────


class TestApprovedPaths:
    def test_paths_from_the_reason(self) -> None:
        reason = (
            "Write needs approval: `lib/db.ts` is outside this task's owned paths (app/**). "
            "Stay inside owned_paths; a human approves shared-file edits on the card."
        )
        assert approved_write_paths(reason) == ["lib/db.ts"]
        shared = "Write needs approval: `package.json` is a shared contract file (own it...)."
        assert approved_write_paths(shared) == ["package.json"]
        assert approved_write_paths("Write needs approval: `app/*.ts` may match a shared") == []
        assert approved_write_paths("`npm i` is not on the builder shell allowlist.") == []
        assert approved_write_paths(None) == []

    async def test_hook_records_on_the_root_task(
        self,
        app_and_service: tuple[FastAPI, ShipcrewService],
        sessions: FakeSessions,
    ) -> None:
        app, service = app_and_service
        mission = await service.create_mission("M", "/repo", None, OWNER)
        task = await service.create_task(mission.id, title="Page", owned_paths=["app/**"])
        started = await service.start_task(task.id, OWNER)
        hook = getattr(app.state, APP_STATE_HOOK)

        class _Req:
            def __init__(self, app_: FastAPI) -> None:
                self.app = app_

        reason = "Write needs approval: `lib/db.ts` is outside this task's owned paths (app/**)."
        _notify_shipcrew_approval(_Req(app), started.root_session_id or "", reason)  # type: ignore[arg-type]
        hook(started.root_session_id, "`ls` is not on the builder shell allowlist.")
        for _ in range(50):
            current = await service.require_task(task.id)
            if current.approved_paths:
                break
            await asyncio.sleep(0.02)
        assert current.approved_paths == ["lib/db.ts"]
        # repeated approvals do not duplicate
        await service.record_approved_write(started.root_session_id or "", reason)
        assert (await service.require_task(task.id)).approved_paths == ["lib/db.ts"]

    def test_notify_without_a_hook_is_a_no_op(self) -> None:
        class _Req:
            app = FastAPI()

        _notify_shipcrew_approval(_Req(), "s", "x")  # type: ignore[arg-type]


# ── 5. package-manager output flags ───────────────────────────────


class TestPackageManagerFlags:
    @pytest.mark.parametrize(
        ("command", "plain"),
        [
            ("pnpm -s lint", "pnpm lint"),
            ("pnpm --silent lint", "pnpm lint"),
            ("npm run --silent typecheck", "npm run typecheck"),
            ("npm run -s test", "npm run test"),
            ("pnpm -s run test", "pnpm run test"),
            ("npm --loglevel warn run lint", "npm run lint"),
            ("pnpm --loglevel=error --no-color test", "pnpm test"),
            ("pnpm --reporter=silent -w lint", "pnpm lint"),
            ("pnpm -s add zod", "pnpm add zod"),
            ("pnpm -s exec vitest run", "npx vitest run"),
            ("npm run lint -- -s", "npm run lint -- -s"),  # the script's own args stay
            ("pnpm -w add zod", "pnpm -w add zod"),  # -w picks the root manifest there
        ],
    )
    def test_normalized(self, command: str, plain: str) -> None:
        assert _normalize_program(command.split()) == plain.split()

    def test_matches_the_plain_entries(self) -> None:
        fn = shell_allowlist(allow=["pnpm lint", "npm run typecheck", "pnpm run test"])
        for cmd in ("pnpm -s lint", "npm run --silent typecheck", "pnpm -s run test"):
            assert fn(_bash(cmd), {})["result"] == "ALLOW", cmd
        assert fn(_bash("pnpm -s build"), {})["result"] == "ASK"

    def test_add_keeps_the_verdict_of_the_plain_add(self) -> None:
        bans = "!-g !--global !-w !--workspace* !--filter*"
        allow = ["cat", f"pnpm add {bans}"]
        feature = [shell_allowlist(allow=allow), owned_paths(owned_paths=["app/**"], root=ROOT)]
        foundation = [
            shell_allowlist(allow=allow),
            owned_paths(owned_paths=["**", "package.json"], root=ROOT),
        ]
        for fns in (feature, foundation):
            assert _worst(fns, _bash("pnpm -s add zod")) == _worst(fns, _bash("pnpm add zod"))
        assert _worst(foundation, _bash("pnpm -s add zod"))["result"] == "ALLOW"
        assert _worst(feature, _bash("pnpm -s add zod"))["result"] == "ASK"
        assert _worst(foundation, _bash("pnpm -s -w add zod"))["result"] == "ASK"
        assert _worst(foundation, _bash("pnpm -s add -g zod"))["result"] == "ASK"
