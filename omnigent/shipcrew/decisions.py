"""The ``Decisions:`` list every worker ends its final reply with.

Agents run unattended: whatever they choose without asking (a library, a data
shape, a default, a scope cut) is written down right before the verdict line::

    Decisions:
    - Stored the cart in memory (no DB in the PRD).
    - Used zod for the request validation.
    PASS

:func:`parse_decisions` reads the last such list of a reply. It is lenient on
formatting (``**Decisions:**``, ``## Decisions``, ``*`` / ``1.`` bullets,
``Decisions: none``) and returns ``[]`` when there is none: a missing list
never fails a task.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_DECISIONS = 20
MAX_DECISION_CHARS = 300
# Decisions kept per task across its turns (CI and review fix turns add some).
MAX_KEPT = 40

_HEADER = re.compile(r"^[#>\s*_]*decisions?(?:\s+made)?[\s*_]*(?::(?P<rest>.*))?$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+(?P<text>.+?)\s*$")
_NONE = re.compile(r"^(?:none|n/?a|nothing|no decisions?|-+|—)\.?$", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")


def _clean(text: str) -> str:
    text = text.strip().strip("*_").strip()
    if len(text) > MAX_DECISION_CHARS:
        text = text[: MAX_DECISION_CHARS - 1].rstrip() + "…"
    return text


def parse_decisions(text: str | None) -> list[str]:
    """The items of the last ``Decisions:`` list in ``text`` (``[]`` when absent).

    The list ends at the first line that is neither a bullet nor blank (the
    verdict line, usually). Code fences are skipped, so a ``Decisions:`` line
    inside a code sample is not read.
    """
    if not text:
        return []
    lines = text.splitlines()
    header: int | None = None
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced and _HEADER.match(line.strip()):
            header = i
    if header is None:
        return []
    match = _HEADER.match(lines[header].strip())
    assert match is not None
    items: list[str] = []
    inline = _clean(match.group("rest") or "")
    if inline and not _NONE.match(inline):
        items.append(inline)
    for line in lines[header + 1 :]:
        if not line.strip():
            if items:
                # A blank line after the items ends the list.
                break
            continue
        bullet = _BULLET.match(line)
        if bullet is None:
            break
        item = _clean(bullet.group("text"))
        if item and not _NONE.match(item):
            items.append(item)
        if len(items) >= MAX_DECISIONS:
            break
    return items[:MAX_DECISIONS]


def merge_decisions(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    """``existing`` then the unseen items of ``new``, capped at :data:`MAX_KEPT`."""
    merged = list(existing)
    seen = {d.casefold() for d in merged}
    for item in new:
        if item.casefold() not in seen:
            seen.add(item.casefold())
            merged.append(item)
    return merged[:MAX_KEPT]
