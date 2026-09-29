"""``test_writes_only``: verify roles (qa, security) write test files and their report only."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnigent.shipcrew import policies
from omnigent.shipcrew.policies import POLICY_REGISTRY, is_test_path, owned_paths

# Not imported by name: pytest would collect the factory as a test.
make_policy = policies.test_writes_only

ROOT = "/work/mission"


def _write(path: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Write", "arguments": {"file_path": path}}}


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


@pytest.fixture
def policy() -> Any:
    return make_policy(role="qa", extra_paths=[".shipcrew/qa.json"], root=ROOT)


@pytest.mark.parametrize(
    ("path", "result"),
    [
        (f"{ROOT}/tests/cart.test.ts", "ALLOW"),
        (f"{ROOT}/e2e/cart.spec.ts", "ALLOW"),
        (f"{ROOT}/app/cart/page.test.tsx", "ALLOW"),
        (f"{ROOT}/src/__tests__/x.ts", "ALLOW"),
        (f"{ROOT}/.shipcrew/qa.json", "ALLOW"),
        ("tests/relative.test.ts", "ALLOW"),
        ("/tmp/shot.png", "ALLOW"),
        (f"{ROOT}/node_modules/.cache/x", "ALLOW"),
        (f"{ROOT}/test-results/a.json", "ALLOW"),
        (f"{ROOT}/src/cart.ts", "DENY"),
        (f"{ROOT}/package.json", "DENY"),
        (f"{ROOT}/src/tests/helper.ts", "DENY"),
        (f"{ROOT}/tests/../src/cart.ts", "DENY"),
        ("/home/user/.bashrc", "DENY"),
        ("$HOME/x.test.ts", "DENY"),
    ],
)
def test_write_tools(policy: Any, path: str, result: str) -> None:
    assert policy(_write(path), {})["result"] == result


@pytest.mark.parametrize(
    ("command", "result"),
    [
        ("mkdir -p e2e/cart", "ALLOW"),
        ("npm test > /tmp/log 2>&1", "ALLOW"),
        ("git add tests && git commit -m 'test: cart'", "ALLOW"),
        ("npx prettier --write tests/cart.test.ts", "ALLOW"),
        ("cp tests/a.test.ts tests/b.test.ts", "ALLOW"),
        # No git repo at ROOT: what a removal deletes cannot be checked (add-only rule).
        ("rm tests/*.snap", "DENY"),
        ("echo x > src/cart.ts", "DENY"),
        ("cd src && touch x.ts", "DENY"),
        ("cp tests/a.test.ts src/a.ts", "DENY"),
        ("git checkout HEAD~1 src/cart.ts", "DENY"),
        ("npx prettier --write .", "DENY"),
        ("npm install lodash", "DENY"),
        ("rm src/*.ts", "DENY"),
        ("cat src/cart.ts", "ALLOW"),
    ],
)
def test_shell_targets(policy: Any, command: str, result: str) -> None:
    assert policy(_bash(command), {})["result"] == result


def test_reads_and_other_tools_pass(policy: Any) -> None:
    read = {"type": "tool_call", "data": {"name": "Read", "arguments": {"file_path": "/etc/x"}}}
    assert policy(read, {})["result"] == "ALLOW"


def test_refusal_says_what_to_do_instead(policy: Any) -> None:
    out = policy(_write(f"{ROOT}/src/cart.ts"), {})
    assert "test files only" in out["reason"] and "fix task" in out["reason"]


def test_without_a_task_contract_paths_are_judged_by_their_tail() -> None:
    policy = make_policy(role="qa")
    assert policy(_write("/any/where/e2e/a.spec.ts"), {})["result"] == "ALLOW"
    assert policy(_write("/any/where/src/a.ts"), {})["result"] == "DENY"


def test_owned_paths_still_asks_for_a_test_outside_the_task() -> None:
    # The two policies combine (most restrictive wins): a test file is writable
    # only inside the task's owned paths.
    owned = owned_paths(owned_paths=["e2e/cart.spec.ts"], root=ROOT)
    tests = make_policy(role="qa", root=ROOT)
    inside, outside = _write(f"{ROOT}/e2e/cart.spec.ts"), _write(f"{ROOT}/e2e/login.spec.ts")
    assert (owned(inside, {})["result"], tests(inside, {})["result"]) == ("ALLOW", "ALLOW")
    assert (owned(outside, {})["result"], tests(outside, {})["result"]) == ("ASK", "ALLOW")


def test_registered_for_uploaded_bundles() -> None:
    handlers = {entry["handler"] for entry in POLICY_REGISTRY}
    assert "omnigent.shipcrew.policies.test_writes_only" in handlers


def test_is_test_path_custom_globs() -> None:
    assert is_test_path("spec/a_spec.rb", ["spec/**"])
    assert not is_test_path("", ["**"])


def test_owned_paths_extra_free_paths_frees_the_report_file() -> None:
    plain = owned_paths(owned_paths=["e2e/**"], root=ROOT)
    qa = owned_paths(owned_paths=["e2e/**"], root=ROOT, extra_free_paths=[".shipcrew/qa.json"])
    report = _write(f"{ROOT}/.shipcrew/qa.json")
    assert (plain(report, {})["result"], qa(report, {})["result"]) == ("ASK", "ALLOW")
    assert qa(_write(f"{ROOT}/node_modules/x"), {})["result"] == "ALLOW"  # defaults kept


class TestAddOnly:
    """Verify roles add tests; other tasks' tests (on the base branch) are never removed."""

    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid",
                           "GIT_CONFIG_GLOBAL": str(tmp_path / "gc"),
                           "GIT_CONFIG_NOSYSTEM": "1"}.items():  # fmt: skip
            monkeypatch.setenv(key, value)
        repo = tmp_path / "wt"
        (repo / "e2e").mkdir(parents=True)
        (repo / "tests" / "__snapshots__").mkdir(parents=True)
        (repo / "e2e" / "home-page.spec.ts").write_text("test('home')\n")
        (repo / "tests" / "__snapshots__" / "a.snap").write_text("x\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"]):  # fmt: skip
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo / "e2e" / "mine.spec.ts").write_text("test('mine')\n")  # this task's new test
        return repo

    @pytest.mark.parametrize(
        ("command", "result"),
        [
            ("git rm -q e2e/home-page.spec.ts", "DENY"),  # seen live: "redundant"
            ("rm e2e/home-page.spec.ts", "DENY"),
            ("rm -rf e2e", "DENY"),
            ("mv e2e/home-page.spec.ts e2e/old.spec.ts", "DENY"),
            ("git mv e2e/home-page.spec.ts e2e/x.spec.ts", "DENY"),
            ("echo '' > e2e/home-page.spec.ts", "DENY"),
            ("truncate -s 0 e2e/home-page.spec.ts", "DENY"),
            ("rm tests/__snapshots__/*.snap", "DENY"),
            ("echo \"test('more')\" >> e2e/home-page.spec.ts", "ALLOW"),  # adding is fine
            ("rm e2e/mine.spec.ts", "ALLOW"),  # its own new test
            ("mv e2e/mine.spec.ts e2e/mine2.spec.ts", "ALLOW"),
            ("rm e2e/*.tmp.spec.ts", "ALLOW"),  # matches nothing at the base
            ("rm -rf test-results node_modules/.cache", "ALLOW"),
        ],
    )
    def test_shell(self, repo: Path, command: str, result: str) -> None:
        policy = make_policy(role="qa", root=str(repo))
        assert policy(_bash(command), {})["result"] == result

    def test_write_tools(self, repo: Path) -> None:
        policy = make_policy(role="qa", root=str(repo))
        existing = str(repo / "e2e" / "home-page.spec.ts")
        out = policy(_write(existing), {})
        assert out["result"] == "DENY" and "only adds tests" in out["reason"]
        edit = {"type": "tool_call", "data": {"name": "Edit", "arguments": {
            "file_path": existing, "old_string": "a", "new_string": "b"}}}  # fmt: skip
        assert policy(edit, {})["result"] == "ALLOW"
        assert policy(_write(str(repo / "e2e" / "mine.spec.ts")), {})["result"] == "ALLOW"
        assert policy(_write(str(repo / "e2e" / "new.spec.ts")), {})["result"] == "ALLOW"
