"""Round 8: the last live-run-4 interventions.

* a read-only grep chain with an ANSI-C string (``grep -c $'\\u00a0' f``) is
  ALLOW for read-only roles (it asked "substitutions, heredocs ...");
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.shipcrew.policies import parse_command, shell_allowlist


def _bash(command: str) -> dict[str, Any]:
    return {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": command}}}


# ── 5. ANSI-C strings in read-only chains ─────────────────────────

LIVE_REVIEWER_CHAIN = (
    'cat components/ui/progress.tsx; grep -n "result-\\|--radius" app/globals.css; '
    'grep -n "className=" components/ui/card.tsx | head -3; '
    "grep -c $' ' lib/results.ts; "
    'grep -rn "\\.skip" components/poll e2e/vote* lib/*.test.ts'
)


def _reader() -> Any:
    allow = ["cat", "head", "grep"]
    return shell_allowlist(allow=allow, read_only=allow, shell_writes=False, role="reviewer")


class TestAnsiC:
    @pytest.mark.parametrize(
        "command",
        [
            LIVE_REVIEWER_CHAIN,
            "grep -c $'\\u00a0' lib/results.ts",
            "grep -n $'\\t' app/page.tsx",
            "grep -c $'\\xc2\\xa0' a.ts",
        ],
    )
    def test_read_only_chain_is_allowed(self, command: str) -> None:
        assert _reader()(_bash(command), {})["result"] == "ALLOW", command

    def test_decoded_value_is_what_the_allowlist_sees(self) -> None:
        segments = parse_command("grep -c $'\\u00a0' lib/x.ts", params=True)
        assert segments is not None and segments[0].argv == ["grep", "-c", "\xa0", "lib/x.ts"]
        # a refused option spelled as an ANSI-C string is still refused
        refused = parse_command("git diff $'--output=/tmp/x'", params=True)
        assert refused is not None and refused[0].argv == ["git", "diff", "--output=/tmp/x"]

    @pytest.mark.parametrize(
        "command", ["echo $'\\cA'", "echo $'it\\'s'", "echo $'\\0'", "echo $'open"]
    )
    def test_undecodable_ansi_c_still_asks(self, command: str) -> None:
        assert parse_command(command, params=True) is None
        assert _reader()(_bash(command), {})["result"] == "ASK"
