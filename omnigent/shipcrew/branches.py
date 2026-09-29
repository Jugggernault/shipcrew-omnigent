"""The one task branch naming scheme: ``shipcrew/<first 8 chars of id>-<slug of title>``.

Server worktrees, the PR loop and the role guardrails all rely on it, so it
lives in one place. A task keeps the branch computed at its first start
(stored on ``Task.branch``) even if its title is edited later.
"""

from __future__ import annotations

import re

BRANCH_PREFIX = "shipcrew/"
_SLUG_MAX = 40
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(title: str, max_len: int = _SLUG_MAX) -> str:
    """Lowercase ASCII slug of ``title``, e.g. ``"Add login!"`` -> ``"add-login"``."""
    slug = _NON_ALNUM.sub("-", title.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "task"


def task_branch(task_id: str, title: str) -> str:
    """Branch name for a task, e.g. ``"shipcrew/1a2b3c4d-add-login"``."""
    return f"{BRANCH_PREFIX}{task_id[:8]}-{slugify(title)}"
