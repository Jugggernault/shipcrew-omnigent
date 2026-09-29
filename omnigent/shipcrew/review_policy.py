"""How much review a PR diff gets: skip, a quick pass, or the full reviewer.

Pure functions over the changed paths and the ``git diff --numstat`` of the PR,
used by the PR loop once CI is green:

- :func:`review_skip_reason`: a diff that only touches tests or only touches
  docs needs no reviewer (CI already ran the tests; docs cannot break the
  build). Agent instructions (``AGENTS.md``, ``CLAUDE.md``, skills) and
  ``DESIGN.md`` are not "docs": they steer future agents, so they are reviewed.
- :func:`review_effort`: a small diff gets a lower reasoning effort on the
  reviewer session (``--effort`` on claude-native), a large one the default.

Everything here only decides the reviewer's work; the approval rules
(``APPROVALS.md``, owned paths) run after it either way.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence

from omnigent.shipcrew.verify import is_test_path

# Diffs up to this many changed lines (added + deleted) get SMALL_DIFF_EFFORT.
SMALL_DIFF_LINES = 150
TINY_DIFF_LINES = 40
# Not .mdx (JSX, compiled into pages) and not .txt (requirements.txt, robots.txt).
_DOC_SUFFIXES = (".md", ".rst", ".adoc")
# Instructions and contracts for agents or the design: never "just docs".
_STEERING = re.compile(
    r"(^|/)(AGENTS|CLAUDE|GEMINI|DESIGN|APPROVALS|SKILL|ROLE|COMMON)\.md$|(^|/)\.shipcrew/|"
    r"(^|/)\.claude/|(^|/)\.cursor/|(^|/)\.github/",
    re.IGNORECASE,
)


def is_doc_path(path: str) -> bool:
    """A prose file whose change cannot alter what the app or the crew does."""
    if _STEERING.search(path):
        return False
    return path.lower().endswith(_DOC_SUFFIXES)


def review_skip_reason(changed: Sequence[str]) -> str | None:
    """Why the reviewer can be skipped for this diff, or ``None`` to review it.

    Call it only with CI green: a tests-only diff is safe to merge unreviewed
    because CI just ran those tests.
    """
    if os.environ.get("SHIPCREW_REVIEW_SKIP", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    paths = [p for p in changed if p]
    if not paths:
        return None
    if all(is_test_path(p) for p in paths):
        return "tests-only diff with CI green"
    if all(is_doc_path(p) for p in paths):
        return "docs-only diff with CI green"
    return None


def changed_lines(numstat: str) -> int:
    """Added + deleted lines from ``git diff --numstat`` output (binary files count 0)."""
    total = 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            total += sum(int(n) for n in parts[:2] if n.isdigit())
    return total


def review_effort(lines: int) -> str | None:
    """Reviewer reasoning effort for a diff of ``lines`` changed lines.

    ``SHIPCREW_REVIEW_EFFORT_TINY`` / ``SHIPCREW_REVIEW_EFFORT_SMALL`` override
    the levels (``"default"`` keeps the agent's own).

    :returns: ``"low"`` (tiny), ``"medium"`` (small) or ``None`` (the agent default).
    """

    def _level(name: str, default: str) -> str | None:
        value = (os.environ.get(name) or default).strip().lower()
        return None if value in {"", "default", "none", "off"} else value

    if lines <= TINY_DIFF_LINES:
        return _level("SHIPCREW_REVIEW_EFFORT_TINY", "low")
    if lines < SMALL_DIFF_LINES:
        return _level("SHIPCREW_REVIEW_EFFORT_SMALL", "medium")
    return None


def split_nul(names: str) -> list[str]:
    """Paths from ``git diff -z --name-only`` output."""
    return [n for n in names.split("\0") if n]


def non_test_paths(changed: Iterable[str]) -> list[str]:
    """Changed paths a verify (qa / security) task was not allowed to write."""
    return [p for p in changed if p and not is_test_path(p)]
