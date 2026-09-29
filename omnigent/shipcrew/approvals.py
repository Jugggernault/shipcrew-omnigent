"""Writes outside owned paths that a human accepted during the build.

The owned-paths guardrail asks before a write outside the task's
``owned_paths`` (``Write needs approval: `lib/db.ts` is outside this task's
owned paths (...)``). When the human accepts that ask on the card, the
claude-native hook route calls the app's ``shipcrew_approval_hook`` (see
``omnigent/server/routes/sessions/routes_hooks.py``, shipcrew fork) with the
session id and the policy reason; :func:`approved_write_paths` reads the
paths back from the reason and the service records them on the task
(``approved_paths``). The PR loop's merge gate then does not hold the PR a
second time for those same paths.
"""

from __future__ import annotations

import re

APP_STATE_HOOK = "shipcrew_approval_hook"

_OWNED_ASK = re.compile(
    r"Write needs approval: `(?P<path>[^`]+)` is "
    r"(?:outside this task's owned paths|a shared contract file)"
)


def approved_write_paths(reason: str | None) -> list[str]:
    """Repo-relative paths an accepted owned-paths ASK *reason* names (deduplicated).

    Only the two exact-file findings count: a glob (``may match a shared
    contract file``), an unresolved or out-of-worktree path is never recorded.
    """
    if not reason:
        return []
    found = [m["path"] for m in _OWNED_ASK.finditer(reason)]
    return list(dict.fromkeys(p for p in found if p and p != "." and "*" not in p))
