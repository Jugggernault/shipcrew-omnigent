"""Round 5: the live run's approval cards for safe work are gone, the protections stay.

Every live command that asked is here as ALLOW, next to the negative cases
that prove the gates behind it (banned options, secrets, owned paths,
workflows, verify roles) still hold. The allowlist groups mirror the
shipcrew bundles (``agents/_shared/policies/allowlists``).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.shipcrew.policies import (
    _has_expansion,
    _perl_in_place,
    _safe_perl_substitution,
    _safe_sed_script,
    _sed_in_place,
    compile_pattern,
    owned_paths,
    parse_command,
    paths_outside_owned,
    shell_allowlist,
    shell_write_targets,
    workflows_guard,
)
from omnigent.shipcrew.policies import (
    test_writes_only as verify_writes_policy,
)

ROOT = "/wt/task"

READ_ONLY = [
    "ls", "cat", "head", "tail", "wc", "rg !--pre*", "grep", "echo", "printf !-v*", "test",
    "[", "pwd", "uniq", "wait", "sort !-o* !--output*", "cd",
    "find !-delete !-exec !-execdir !-ok !-okdir !-fprint* !-fls !-fprintf",
    r"re:^sed -n (-[Er] )?(\d+(,(\d+|\$))?p|\$p|/[^/]*/(,/[^/]*/)?p)( [^ ]+)*$",
]  # fmt: skip
GIT_READ = ["git status", "git diff !--output*", "git log !--output*", "git rev-parse",
            "git diff-tree !--output*"]  # fmt: skip
GIT_WRITE = ["git add", "git commit", "git mv", "git rm", "git checkout"]
FS_WRITE = ["mkdir", "touch", "cp", "mv", "rm", "tee"]
DEV_TOOLS = ["npm test", "npm run *", "npm ci", "pnpm test", "npx vitest", "npx playwright test",
             "npx @google/design.md lint", "pnpm install --frozen-lockfile"]  # fmt: skip
_NPM_BANS = "!-g !--global !--prefix* !--location* !-w !--workspace* !--include-workspace-root*"
_PNPM_BANS = (
    "!-g !--global !-C !--dir* !-w !--workspace* !-r !--recursive !-F !--filter* !--prefix*"
)
DEPS = [f"npm install {_NPM_BANS}", f"npm i {_NPM_BANS}", f"pnpm add {_PNPM_BANS}",
        f"pnpm install {_PNPM_BANS}"]  # fmt: skip
FS_EDIT = ["@sed:sed_in_place", "@perl:perl_in_place"]

BUILDER_ALLOW = [*READ_ONLY, *GIT_READ, *GIT_WRITE, *FS_WRITE, *DEV_TOOLS, *DEPS, *FS_EDIT]
READER_ALLOW = [*READ_ONLY, *GIT_READ, "npm test"]

FOUNDATION = ["**", "package.json"]
FEATURE = ["app/cart/**", "e2e/cart.spec.ts"]


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


def _write(path: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Write", "arguments": {"file_path": path}}}


_RANK = {"ALLOW": 0, "ASK": 1, "DENY": 2}


def _builder(owned: list[str]) -> list[Any]:
    return [
        shell_allowlist(allow=BUILDER_ALLOW, read_only=[*READ_ONLY, *GIT_READ], role="builder"),
        owned_paths(owned_paths=owned, root=ROOT),
        workflows_guard(),
    ]


def _reader() -> list[Any]:
    return [
        shell_allowlist(allow=READER_ALLOW, read_only=[*READ_ONLY, *GIT_READ], shell_writes=False),
        workflows_guard(),
    ]


def _verifier(owned: list[str]) -> list[Any]:
    return [
        shell_allowlist(allow=BUILDER_ALLOW, read_only=[*READ_ONLY, *GIT_READ], role="qa"),
        owned_paths(owned_paths=owned, root=ROOT),
        verify_writes_policy(role="qa", root=ROOT),
        workflows_guard(),
    ]


def _verdict(policies: list[Any], event: dict[str, Any]) -> str:
    worst = "ALLOW"
    for fn in policies:
        result = fn(event, {})["result"]
        worst = result if _RANK[result] > _RANK[worst] else worst
    return worst


LIVE_INSTALL = (
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -D --no-audit --no-fund vitest "
    '@playwright/test @faker-js/faker 2>&1 | grep -v "^npm warn install" | head -30'
)
LIVE_RENAME = (
    "git mv -f vitest.config.ts vitest.config.mts 2>/dev/null || mv vitest.config.ts "
    'vitest.config.mts; sed -i \'s|"test": "vitest"|"test": "vitest run"|\' package.json; '
    'npm run typecheck 2>&1 | tail -5; npm test 2>&1 | grep -E "warn|Tests"'
)
LIVE_BACKGROUND = (
    "npm ci --prefer-offline --no-audit --no-fund 2>&1 | tail -3 &\n"
    "cat DESIGN.md app/api/polls/\\[id\\]/route.ts app/page.tsx; "
    'grep -n "export\\|seed" lib/db.ts | head -40; wait'
)


class TestLiveCommandsNowAllowed:
    @pytest.mark.parametrize(
        "command",
        [
            "npx -y @google/design.md lint DESIGN.md; echo EXIT=$?",
            "echo CHROMIUM=$CHROMIUM_PATH PORT=$PORT",
            "S=/tmp/sc/scratchpad/app; cat $S/package.json",
            LIVE_INSTALL,
            LIVE_RENAME,
            LIVE_BACKGROUND,
            "PORT=${PORT:-3000} CI=1 npx playwright test",
            "echo pid=$$ args=$# last=$! status=${?}",
        ],
    )
    def test_foundation_builder(self, command: str) -> None:
        assert _verdict(_builder(FOUNDATION), _bash(command)) == "ALLOW"

    @pytest.mark.parametrize(
        "command",
        [
            "echo CHROMIUM=$CHROMIUM_PATH PORT=$PORT",
            "S=/tmp/sc/scratchpad/app; cat $S/package.json | head -5",
            'git log --oneline -3 && echo "done in $PWD" && cat "$HOME/.gitconfig"',
            "npm test 2>&1 | tail -5; echo EXIT=$?",
        ],
    )
    def test_reader(self, command: str) -> None:
        assert _verdict(_reader(), _bash(command)) == "ALLOW"


class TestProtectionsHold:
    @pytest.mark.parametrize(
        "command",
        [
            "X=--output=f; git diff $X",  # an expansion becomes a banned option
            "CI=--output=/tmp/x; git diff $CI",
            "git diff ${X}",
            "echo $DATABASE_URL",  # would print a secret
            "cat $SECRET_FILE",
            "PATH=/tmp/evil; cat README.md",  # runs /tmp/evil/cat
            "GIT_EXTERNAL_DIFF=/tmp/x; git diff",
            "S=/tmp/x; npm test",  # runs after an unvetted assignment
            "npm test -- $ARGS",
            "printf -v PATH %s /tmp/evil; npm test",
            "rg --pre /tmp/x.sh TODO",
            "git diff-tree --output=/tmp/x -p HEAD",
            "echo x > $OUT",
            "cd $DIR && rm -rf build",
            "NODE_OPTIONS=--require=/tmp/x.js npm test",
            "echo ${X/a/b}",  # a pattern substitution: not modelled
            "echo $1",
            "npm install -g vercel",
            "pnpm add --filter web zod",
            "npm i --prefix /tmp/x lodash",
            "$CMD --version",
        ],
    )
    def test_builder_asks(self, command: str) -> None:
        assert _verdict(_builder(FOUNDATION), _bash(command)) == "ASK"

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/w /tmp/x' app/page.tsx",
            "sed -i '1e touch /tmp/x' app/page.tsx",
            "sed -i.bak 's/a/b/' app/page.tsx",
            "sed -i -f script.sed app/page.tsx",
            "perl -pi -e 'system(1)' app/page.tsx",
            "perl -pi -e 's/a/@{[system(1)]}/' app/page.tsx",
            "perl -pi -e 's/a/b/e' app/page.tsx",
            "perl -pie 's/a/b/' app/page.tsx",
        ],
    )
    def test_builder_complex_in_place_edit_is_denied(self, command: str) -> None:
        # Round 7: the agent picked the wrong tool; the refusal says to use Edit.
        assert _verdict(_builder(FOUNDATION), _bash(command)) == "DENY"

    @pytest.mark.parametrize(
        "command",
        [
            "npm install -D vitest",  # package.json not owned by a feature task
            "pnpm add zod",
            "sed -i 's/a/b/' src/other.ts",  # outside owned paths
            "git mv app/cart/a.ts lib/a.ts",
            LIVE_INSTALL,
        ],
    )
    def test_feature_task_asks(self, command: str) -> None:
        assert _verdict(_builder(FEATURE), _bash(command)) == "ASK"

    def test_feature_task_edits_its_own_files(self) -> None:
        fn = _builder(FEATURE)
        assert _verdict(fn, _bash("sed -i 's|a|b|g' app/cart/page.tsx")) == "ALLOW"
        assert _verdict(fn, _bash("perl -pi -e 's/a/$1b/g' app/cart/page.tsx")) == "ALLOW"

    def test_ci_workflow_writes_still_ask(self) -> None:
        fn = _builder(FOUNDATION)
        assert _verdict(fn, _write(f"{ROOT}/.github/workflows/ci.yml")) == "ASK"
        assert _verdict(fn, _bash("sed -i 's/npm/pnpm/' .github/workflows/ci.yml")) == "ASK"
        assert _verdict(fn, _bash("cat .github/workflows/ci.yml; echo EXIT=$?")) == "ALLOW"

    def test_reader_stays_read_only(self) -> None:
        fn = _reader()
        assert _verdict(fn, _bash("sed -i 's/a/b/' app/page.tsx")) == "ASK"
        assert _verdict(fn, _bash("npm install -D vitest")) == "ASK"
        assert _verdict(fn, _bash("uniq a.txt b.txt")) == "ASK"  # uniq IN OUT writes
        assert _verdict(fn, _bash("S=/tmp/x; git mv a b")) == "ASK"

    def test_verifier_writes_tests_only(self) -> None:
        fn = _verifier(FOUNDATION)
        assert _verdict(fn, _bash("npm install -D vitest")) == "DENY"
        assert _verdict(fn, _bash("sed -i 's/a/b/' app/page.tsx")) == "DENY"
        assert _verdict(fn, _bash("sed -i 's/a/b/' e2e/cart.spec.ts")) == "ALLOW"


class TestExpansionScanner:
    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("echo $?", False),
            ("echo $$ $# $! ${?}", False),
            ("echo '$HOME'", False),
            ("echo $HOME", True),
            ('echo "${PORT:-3000}"', True),
            ("echo $'\\x41'", True),
            ("git reset --{ha,}rd", True),
        ],
    )
    def test_has_expansion(self, text: str, kind: bool) -> None:
        assert _has_expansion(text) is kind

    def test_params_are_parsed_only_on_request(self) -> None:
        assert parse_command("cat $S/a") is None
        segs = parse_command("S=/x; cat $S/a '$LIT'", params=True)
        assert segs is not None
        assert [s.expanded for s in segs] == [[], ["S"]]
        assert segs[1].raw == ["cat", "$S/a", "$LIT"]
        assert parse_command("S=/x; cat $S", strict=False) is not None

    def test_newline_after_an_operator(self) -> None:
        segs = parse_command("npm ci &\nls |\nwc -l", params=True)
        assert segs is not None
        assert [s.argv for s in segs] == [["npm", "ci"], ["ls"], ["wc", "-l"]]

    def test_expansion_safe_entries(self) -> None:
        assert compile_pattern("cat").expansion_safe
        assert compile_pattern("git status").expansion_safe
        assert not compile_pattern("git diff !--output*").expansion_safe
        assert not compile_pattern("npm run *").expansion_safe
        assert not compile_pattern("cd").expansion_safe
        assert not compile_pattern("re:^sed -n .*").expansion_safe


class TestInPlaceEditors:
    @pytest.mark.parametrize(
        ("script", "ok"),
        [
            ("s/a/b/", True),
            ('s|"test": "vitest"|"test": "vitest run"|g', True),
            ("1,3s/a/b/2", True),
            ("1,3s/a/b/; $s/x/y/2", False),  # several commands (round 7)
            ("s/a\\/b/c/I", True),
            ("s/a/b/w out", False),
            ("s/a/b/e", False),
            ("1e rm -rf ~", False),
            ("r /etc/passwd", False),
            ("s/a/b", False),
            ("", False),
        ],
    )
    def test_sed_scripts(self, script: str, ok: bool) -> None:
        assert _safe_sed_script(script) is ok

    def test_sed_argv(self) -> None:
        assert _sed_in_place(["-i", "s/a/b/", "f"])
        assert _sed_in_place(["-i", "-E", "-e", "s/a/b/", "f", "g"])
        assert not _sed_in_place(["-i", "-e", "s/a/b/", "-e", "s/c/d/", "f"])  # two commands
        assert not _sed_in_place(["s/a/b/", "f"])  # not in place
        assert not _sed_in_place(["-i", "s/a/b/"])  # no file
        assert not _sed_in_place(["-ni", "s/a/b/p", "f"])

    @pytest.mark.parametrize(
        ("script", "ok"),
        [
            ("s/a/b/g", True),
            ("s/foo$/bar/", True),
            ("s/(a)/$1-x/g", True),
            ("s|a|b|", True),
            ("s/a/b/e", False),
            ("s/a/@{[ system 1 ]}/", False),
            ("s/a/${\\ system 1}/", False),
            ("s/$x/b/", False),
            ("s/(?{ system 1 })a/b/", False),
            ("system('id')", False),
            ("s{a}{b}", False),
        ],
    )
    def test_perl_substitutions(self, script: str, ok: bool) -> None:
        assert _safe_perl_substitution(script) is ok

    def test_perl_argv(self) -> None:
        assert _perl_in_place(["-pi", "-e", "s/a/b/g", "f"])
        assert _perl_in_place(["-i", "-pe", "s/a/b/g", "f"])
        assert not _perl_in_place(["-pie", "s/a/b/", "f"])  # -i with backup suffix "e"
        assert not _perl_in_place(["-pi", "-e", "s/a/b/g"])

    @pytest.mark.parametrize(
        ("argv", "targets"),
        [
            (["sed", "-i", "s/a/b/", "f", "g"], ["f", "g"]),
            (["sed", "-ni", "s/a/b/p", "f"], ["f"]),
            (["sed", "-i", "--expression=s/a/b/", "f"], ["f"]),
            (["perl", "-pi", "-e", "s/a/b/", "f"], ["f"]),
            (["perl", "-i", "-n", "-e", "print", "f"], ["f"]),  # not modelled: every file
            (["uniq", "a", "b"], ["b"]),
            (["uniq", "-c", "a"], []),
        ],
    )
    def test_write_targets(self, argv: list[str], targets: list[str]) -> None:
        assert shell_write_targets(argv) == targets


class TestManifestOwnsItsLockfiles:
    def test_owning_package_json_owns_the_lockfiles_next_to_it(self) -> None:
        fn = owned_paths(owned_paths=["app/**", "package.json"], root=ROOT)
        for lock in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            assert fn(_write(f"{ROOT}/{lock}"), {})["result"] == "ALLOW"
        assert fn(_write(f"{ROOT}/apps/web/pnpm-lock.yaml"), {})["result"] == "ASK"
        assert fn(_bash("pnpm add zod"), {})["result"] == "ALLOW"

    def test_diff_check_agrees(self) -> None:
        changed = ["package.json", "pnpm-lock.yaml", "apps/web/yarn.lock"]
        assert paths_outside_owned(changed, ["**", "package.json"]) == ["apps/web/yarn.lock"]
