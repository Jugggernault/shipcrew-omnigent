"""Writes outside owned paths that a human accepted during the build.

The owned-paths guardrail asks before a write outside the task's
``owned_paths`` (``Write needs approval: `lib/db.ts` is outside this task's
owned paths (...)``). When the human accepts that ask on the card, the
claude-native hook route calls the app's ``shipcrew_approval_hook`` (see
``omnigent/server/routes/sessions/routes_hooks.py``, shipcrew fork) with the
session id and the policy reason (claude-sdk: the ``approval`` event, through
:func:`notify_accepted_relay_ask`); :func:`approved_write_paths` reads the
paths back from the reason and the service records them on the task
(``approved_paths``). The PR loop's merge gate then does not hold the PR a
second time for those same paths.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

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


def notify_accepted_relay_ask(app: Any, session_id: str, data: dict[str, Any]) -> None:
    """Call the app's approval hook for an accepted relay-path policy ASK (never raises).

    claude-native ASKs reach the hook from the hook route; claude-sdk (and
    every other relay harness) tool calls park their ASK as a runner-owned
    elicitation whose verdict arrives as an ``approval`` event. The deciding
    policy's reason is stashed with the elicitation
    (``_PendingPolicyAskWrites.policy_reason``, shipcrew fork); read it here,
    before the pending entry is consumed.
    """
    if data.get("action") != "accept":
        return
    hook = getattr(getattr(app, "state", None), APP_STATE_HOOK, None)
    if hook is None:
        return
    try:
        from omnigent.server.routes._sessions.common import _pending_policy_ask_writes

        pending = _pending_policy_ask_writes.get(str(data.get("elicitation_id") or ""))
        reason = getattr(pending, "policy_reason", None)
        if reason:
            hook(session_id, reason)
    except Exception:  # noqa: BLE001 - the approval itself must go through
        _logger.debug("shipcrew relay approval hook failed", exc_info=True)
