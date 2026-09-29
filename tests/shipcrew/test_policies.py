"""shipcrew guardrail policies: shell allowlist, owned paths, push guard."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.shipcrew.policies import (
    compile_pattern,
    owned_paths,
    parse_command,
    push_guard,
    shell_allowlist,
    shell_write_targets,
)

ROOT = "/wt/task"


def _bash(command: str, tool: str = "Bash") -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": tool, "arguments": {"command": command}}}


def _write(path: str, tool: str = "Write") -> dict[str, Any]:
    key = "path" if tool.startswith("sys_os") else "file_path"
    return {"type": "tool_call", "data": {"name": tool, "arguments": {key: path, "content": "x"}}}


def _result(fn: Any, event: dict[str, Any]) -> str:
    return str(fn(event, {})["result"])


class TestParse:
    def test_splits_on_operators_outside_quotes(self) -> None:
        segs = parse_command('git commit -m "a; b && c" && npm test 2>&1 | tail -5')
        assert segs is not None
        assert [s.argv for s in segs] == [
            ["git", "commit", "-m", "a; b && c"],
            ["npm", "test"],
            ["tail", "-5"],
        ]
        assert all(s.writes == [] for s in segs)

    def test_redirect_targets_and_fd_numbers(self) -> None:
        segs = parse_command("npm test >out.log 2>err.log && ls 2>/dev/null")
        assert segs is not None
        assert segs[0].writes == ["out.log", "err.log"]
        assert segs[0].argv == ["npm", "test"]
        assert segs[1].writes == ["/dev/null"]

    def test_newlines_and_comments(self) -> None:
        segs = parse_command("# setup\nls\ncat x > y")
        assert segs is not None
        assert [s.argv for s in segs] == [["ls"], ["cat", "x"]]
        assert segs[1].writes == ["y"]

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(id)",
            "ls `id`",
            "diff <(ls a) <(ls b)",
            "cat <<EOF > f\nx\nEOF",
            "echo 'unbalanced",
        ],
    )
    def test_unanalyzable(self, command: str) -> None:
        assert parse_command(command) is None

    def test_commit_heredoc_idiom_is_a_literal(self) -> None:
        segs = parse_command("git commit -m \"$(cat <<'EOF'\nfeat: x\n\nbody $(not run)\nEOF\n)\"")
        assert segs is not None
        assert [s.argv for s in segs] == [["git", "commit", "-m", "HEREDOC"]]

    def test_env_and_program_normalization(self) -> None:
        segs = parse_command(
            "CI=1 /usr/bin/git --no-pager log; pnpm exec vitest; npx -y tsc; timeout 5 npm test"
        )
        assert segs is not None
        assert segs[0].env == ["CI"]
        assert [s.argv for s in segs] == [
            ["git", "log"],
            ["npx", "vitest"],
            ["npx", "tsc"],
            ["npm", "test"],
        ]


class TestPatterns:
    def test_words_banned_and_exact(self) -> None:
        diff = compile_pattern("git diff !--output*")
        assert diff.matches(["git", "diff", "--stat"])
        assert not diff.matches(["git", "diff", "--output=x"])
        assert not diff.matches(["git", "log"])
        branch = compile_pattern("git branch $")
        assert branch.matches(["git", "branch"])
        assert not branch.matches(["git", "branch", "new"])
        run = compile_pattern("npm run *")
        assert run.matches(["npm", "run", "build", "--", "--x"])
        assert not run.matches(["npm", "run"])

    def test_regex_entry(self) -> None:
        sed = compile_pattern(r"re:^sed -n (\d+(,\d+)?p|/[^/]*/p)( [^ ]+)*$")
        assert sed.matches(["sed", "-n", "1,40p", "a.ts"])
        assert not sed.matches(["sed", "-n", "1w /tmp/x", "a.ts"])

    @pytest.mark.parametrize(
        ("args", "ok"),
        [
            (["-s", "http://localhost:3000/api"], True),
            (["-X", "POST", "-d", '{"a":1}', "127.0.0.1:3000/x"], True),
            (["http://[::1]:8080/"], True),
            (["https://example.com"], False),
            (["http://localhost@evil.com/"], False),
            (["http://localhost:3000@evil.com/"], False),
            (["-o", "/tmp/x", "http://localhost"], False),
            (["-sO", "http://localhost/f"], False),
            (["--config", "x", "http://localhost"], False),
            (["-s"], False),
        ],
    )
    def test_curl_localhost(self, args: list[str], ok: bool) -> None:
        assert compile_pattern("@curl:curl_localhost").matches(["curl", *args]) is ok

    def test_unknown_builtin_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="built-in"):
            compile_pattern("@curl:nope")


class TestShellAllowlist:
    ALLOW = ["ls", "cat", "npm test", "npm run *", "npx vitest", "git commit", "tail", "mkdir"]

    def test_allowlisted_chain_runs_without_prompt(self) -> None:
        fn = shell_allowlist(allow=self.ALLOW, role="developer")
        assert _result(fn, _bash("npm test 2>&1 | tail -40 && npm run lint")) == "ALLOW"
        assert _result(fn, _bash("CHROMIUM_PATH=/usr/bin/chromium npx vitest run")) == "ALLOW"

    def test_anything_else_asks_with_the_command_in_the_reason(self) -> None:
        fn = shell_allowlist(allow=self.ALLOW, role="developer")
        out = fn(_bash("ls && python3 -c 'print(1)'"), {})
        assert out["result"] == "ASK"
        assert "python3 -c print(1)" in out["reason"]
        assert "developer shell allowlist" in out["reason"]

    def test_unknown_env_and_unanalyzable_ask(self) -> None:
        fn = shell_allowlist(allow=self.ALLOW)
        assert _result(fn, _bash("LD_PRELOAD=/tmp/x.so ls")) == "ASK"
        assert _result(fn, _bash("ls $(cat list)")) == "ASK"

    def test_non_shell_tools_and_other_harness_names(self) -> None:
        fn = shell_allowlist(allow=self.ALLOW)
        assert _result(fn, _write("/x")) == "ALLOW"
        assert _result(fn, _bash("rm -rf x", tool="sys_os_shell")) == "ASK"

    def test_read_only_roles_ask_on_shell_writes(self) -> None:
        fn = shell_allowlist(allow=self.ALLOW, shell_writes=False)
        assert _result(fn, _bash("cat a > b")) == "ASK"
        assert _result(fn, _bash("mkdir -p out")) == "ASK"
        assert _result(fn, _bash("npm test > /tmp/log 2>&1")) == "ALLOW"
        assert _result(fn, _bash("ls > /dev/null")) == "ALLOW"


class TestShellWriteTargets:
    @pytest.mark.parametrize(
        ("argv", "targets"),
        [
            (["rm", "-rf", "a", "b"], ["a", "b"]),
            (["cp", "-r", "src", "dst"], ["dst"]),
            (["cp", "-t", "dir", "a", "b"], ["dir"]),
            (["mv", "a", "b"], ["a", "b"]),
            (["ln", "-s", "/x/y"], ["y"]),
            (["chmod", "+x", "run.sh"], ["run.sh"]),
            (["sed", "-i", "s/a/b/", "f"], ["f"]),
            (["sed", "-n", "1p", "f"], []),
            (["git", "mv", "a", "b"], ["a", "b"]),
            (["git", "checkout", "main"], []),
            (["git", "checkout", "--", "f"], ["f"]),
            (["npx", "prettier", "--write", "src"], ["src"]),
            (["prettier", "--check", "src"], []),
            (["eslint", "--fix"], ["."]),
            (["ruff", "format"], ["."]),
            (["npm", "install", "lodash"], ["package.json", "package-lock.json"]),
            (["pnpm", "install", "--frozen-lockfile"], []),
            (["npm", "ci"], []),
            (["ls", "-la"], []),
        ],
    )
    def test_targets(self, argv: list[str], targets: list[str]) -> None:
        assert shell_write_targets(argv) == targets


class TestOwnedPaths:
    OWNED = ["app/cart/**", "e2e/cart.spec.ts", "lib/cart"]

    def _fn(self, owned: list[str] | None = None) -> Any:
        return owned_paths(owned_paths=self.OWNED if owned is None else owned, root=ROOT)

    @pytest.mark.parametrize(
        ("path", "verdict"),
        [
            (f"{ROOT}/app/cart/page.tsx", "ALLOW"),
            (f"{ROOT}/app/cart", "ALLOW"),
            (f"{ROOT}/e2e/cart.spec.ts", "ALLOW"),
            (f"{ROOT}/lib/cart/api.ts", "ALLOW"),
            (f"{ROOT}/lib/db.ts", "ASK"),
            (f"{ROOT}/app/cart/../page.tsx", "ASK"),
            (f"{ROOT}/package.json", "ASK"),
            (f"{ROOT}/node_modules/x/index.js", "ALLOW"),
            (f"{ROOT}/test-results/a.png", "ALLOW"),
            ("/tmp/shot.png", "ALLOW"),
            ("/home/me/.bashrc", "ASK"),
            ("/wt/task-other/app/cart/x.ts", "ASK"),
        ],
    )
    def test_write_tools(self, path: str, verdict: str) -> None:
        assert _result(self._fn(), _write(path)) == verdict
        assert _result(self._fn(), _write(path, tool="Edit")) == verdict

    def test_relative_tool_paths_resolve_against_the_worktree(self) -> None:
        assert _result(self._fn(), _write("app/cart/x.ts", tool="sys_os_write")) == "ALLOW"
        assert _result(self._fn(), _write("lib/db.ts", tool="sys_os_write")) == "ASK"

    def test_shared_files_need_ownership_by_name(self) -> None:
        wide = self._fn(["**"])
        assert _result(wide, _write(f"{ROOT}/src/x.ts")) == "ALLOW"
        assert _result(wide, _write(f"{ROOT}/package.json")) == "ASK"
        assert _result(wide, _write(f"{ROOT}/apps/web/pnpm-lock.yaml")) == "ASK"
        named = self._fn(["**", "package.json", "package-lock.json"])
        assert _result(named, _write(f"{ROOT}/package.json")) == "ALLOW"
        assert _result(named, _bash("npm install lodash")) == "ALLOW"
        assert _result(wide, _bash("npm install lodash")) == "ASK"

    @pytest.mark.parametrize(
        ("command", "verdict"),
        [
            ("echo x > app/cart/a.ts", "ALLOW"),
            ("echo x > lib/db.ts", "ASK"),
            ("cat lib/db.ts && rg foo lib", "ALLOW"),
            ("cd app && touch cart/x.ts", "ALLOW"),
            ("cd lib && touch x.ts", "ASK"),
            ("cd && touch x", "ASK"),
            ("git -C app mv cart/a.ts cart/b.ts", "ALLOW"),
            ("git mv app/cart/a.ts lib/a.ts", "ASK"),
            ("rm -rf node_modules .next", "ALLOW"),
            ("rm app/cart/*.ts", "ALLOW"),
            ("rm lib/*.ts", "ASK"),
            ("rm *.json", "ASK"),
            ("rm -rf test-results/*", "ALLOW"),
            ("cp lib/db.ts /tmp/db.ts", "ALLOW"),
            ("cp app/cart/a.ts /etc/a", "ASK"),
            ("npx prettier --write .", "ASK"),
            ("npx prettier --write app/cart", "ALLOW"),
            ("echo x > $HOME/f", "ASK"),
            ("npm test > /tmp/log 2>&1", "ALLOW"),
            ("echo $(x) > lib/db.ts", "ALLOW"),  # unanalyzable: the allowlist asks
        ],
    )
    def test_shell_writes(self, command: str, verdict: str) -> None:
        assert _result(self._fn(), _bash(command)) == verdict

    def test_reads_are_free(self) -> None:
        args = {"file_path": "/etc/x"}
        read = {"type": "tool_call", "data": {"name": "Read", "arguments": args}}
        assert _result(self._fn(), read) == "ALLOW"

    def test_reason_names_the_path_and_the_owned_globs(self) -> None:
        out = self._fn()(_write(f"{ROOT}/lib/db.ts"), {})
        assert "`lib/db.ts`" in out["reason"]
        assert "app/cart/**" in out["reason"]

    def test_abstains_without_a_task_contract(self) -> None:
        for fn in (owned_paths(), owned_paths(owned_paths=[], root=ROOT)):
            assert _result(fn, _write("/etc/passwd")) == "ALLOW"
            assert _result(fn, _bash("rm -rf lib")) == "ALLOW"

    def test_unknown_root_asks(self) -> None:
        fn = owned_paths(owned_paths=["app/**"], root="")
        assert _result(fn, _write("/wt/x/app/a.ts")) == "ASK"
        assert _result(fn, _write("/tmp/a.png")) == "ALLOW"


class TestPushGuard:
    BRANCH = "shipcrew/1a2b3c4d-add-cart"

    @pytest.mark.parametrize(
        ("command", "verdict"),
        [
            (f"git push origin {BRANCH}", "ALLOW"),
            (f"git -C .worktrees/T02 push -u origin {BRANCH}", "ALLOW"),
            (f"git push origin {BRANCH}:{BRANCH}", "ALLOW"),
            (f"git push -o ci.skip origin {BRANCH}", "ALLOW"),
            ("git push origin shipcrew/T02-cart", "DENY"),
            ("git push origin shipcrew/1a2b3c4d-Cart", "DENY"),
            ("git push origin shipcrew/1a2b3c4-cart", "DENY"),
            ("git push", "DENY"),
            ("git push origin", "DENY"),
            ("git push origin main", "DENY"),
            (f"git push origin {BRANCH}:main", "DENY"),
            (f"git push origin +{BRANCH}", "DENY"),
            (f"git push --force origin {BRANCH}", "DENY"),
            ("git push --all origin", "DENY"),
            (f"git push origin --delete {BRANCH}", "DENY"),
            (f"git push origin {BRANCH} && git push origin feature", "DENY"),
            ("/usr/bin/git push origin main", "DENY"),
            ("git push origin $(git branch --show-current)", "DENY"),
            ("gh pr merge 12 --squash", "ASK"),
            ("gh release create v1", "ASK"),
            (f"gh pr create --draft --fill --head {BRANCH}", "ALLOW"),
            ("git status && npm test", "ALLOW"),
        ],
    )
    def test_push_guard(self, command: str, verdict: str) -> None:
        assert _result(push_guard(), _bash(command)) == verdict


def test_policies_are_registered_handlers() -> None:
    from omnigent.policies.builtins import BUILTIN_POLICY_MODULES
    from omnigent.policies.registry import is_registered_handler, load_registry

    assert "omnigent.shipcrew.policies" in BUILTIN_POLICY_MODULES
    load_registry()
    for name in ("shell_allowlist", "owned_paths", "push_guard"):
        assert is_registered_handler(f"omnigent.shipcrew.policies.{name}")


def test_claude_native_allowed_tools_become_launch_args() -> None:
    from omnigent.server.routes._sessions.helpers import _derive_terminal_launch_args_from_spec
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    def _spec(**config: str) -> AgentSpec:
        return AgentSpec(
            spec_version=1,
            name="dev",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native", **config}),
        )

    derive = _derive_terminal_launch_args_from_spec
    assert derive(_spec(permission_mode="default", allowed_tools="Bash, Edit,Write")) == [
        "--permission-mode",
        "default",
        "--allowedTools",
        "Bash,Edit,Write",
    ]
    assert derive(_spec(allowed_tools="Bash")) == ["--allowedTools", "Bash"]
    assert derive(_spec(permission_mode="auto")) == ["--permission-mode", "auto"]
    assert derive(_spec()) is None
