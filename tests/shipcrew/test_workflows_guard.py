"""``workflows_guard``: writes to .github/workflows ask, reads pass, even in a chain."""

from __future__ import annotations

import pytest

from omnigent.shipcrew.policies import POLICY_REGISTRY, workflows_guard

CASES = {
    # the live false positive of the old CEL regex (a checkout, then a read of workflows)
    "git checkout shipcrew-tests/aa-1 -- test/cart.test.js && cat src/cart.js; "
    "ls .github/workflows 2>/dev/null && cat .github/workflows/ci.yml": "ALLOW",
    "git fetch -q origin; ls .github/workflows": "ALLOW",
    "yq '.jobs' .github/workflows/ci.yml": "ALLOW",
    "grep -i node .github/workflows/ci.yml": "ALLOW",
    "npm test": "ALLOW",
    "sed -i 's/npm/pnpm/' .github/workflows/ci.yml": "ASK",
    "cat ci.yml > .github/workflows/ci.yml": "ASK",
    "ls .github/workflows && sed -i 's/a/b/' .github/workflows/ci.yml": "ASK",
    "cat $(sed -i 's/a/b/' .github/workflows/ci.yml)": "ASK",
    "ls `sed -i 's/a/b/' .github/workflows/ci.yml`": "ASK",
    "cat .github/workflows/ci.yml\nsed -i 's/a/b/' .github/workflows/ci.yml": "ASK",
    "yq -i '.on = \"push\"' .github/workflows/ci.yml": "ASK",
    "git diff --output=.github/workflows/ci.yml": "ASK",
    "cd .github && sed -i s/a/b/ workflows/ci.yml": "ASK",
    "cp /tmp/ci.yml .github/workflows/": "ASK",
    "python3 fix.py .github/workflows/ci.yml": "ASK",
    "git checkout HEAD~1 -- .github/workflows/ci.yml": "ASK",
}


@pytest.mark.parametrize(("command", "result"), list(CASES.items()))
def test_shell(command: str, result: str) -> None:
    event = {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}
    assert workflows_guard()(event, {})["result"] == result


@pytest.mark.parametrize(
    ("path", "result"),
    [
        ("/r/.github/workflows/ci.yml", "ASK"),
        ("/r/.github/CODEOWNERS", "ALLOW"),
        ("a.ts", "ALLOW"),
    ],
)
def test_write_tools(path: str, result: str) -> None:
    event = {"type": "tool_call", "data": {"name": "Edit", "arguments": {"file_path": path}}}
    assert workflows_guard()(event, {})["result"] == result


def test_registered() -> None:
    assert "omnigent.shipcrew.policies.workflows_guard" in {e["handler"] for e in POLICY_REGISTRY}
