"""``test_writes_only``: verify roles (qa, security) write test files and their report only."""

from __future__ import annotations

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
        ("rm tests/*.snap", "ALLOW"),
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
