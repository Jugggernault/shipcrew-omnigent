"""A task owns its own test files: owned-path globs added at plan import.

Planners list the source files a task changes and forget its tests, so a
feature task writing ``e2e/home-page.spec.ts`` hit the owned-paths guardrail.
:func:`with_own_tests` adds, after the task's own entries (kept, in order):

* per task, from the slug of its title (:func:`omnigent.shipcrew.branches.slugify`,
  the slug of its branch name, e.g. ``"Home page"`` -> ``home-page``):
  ``e2e/<slug>*.spec.*``, ``test/<slug>*`` and ``tests/<slug>*``;
* colocated tests, only inside what the task already owns:

  - a source file ``app/cart/page.tsx`` -> ``app/cart/page.test.*``,
    ``app/cart/page.spec.*`` (``test_<stem>.py`` for Python);
  - a glob ``app/cart/*.tsx`` -> ``app/cart/*.test.*``, ``app/cart/*.spec.*``,
    ``app/cart/__tests__/**`` (``app/cart/**/*.tsx`` -> the same under
    ``app/cart/**/``);
  - a whole subtree (``app/cart/**``, ``app/cart``) already covers its tests:
    nothing added. A glob at the repository root (``*.ts``) is never widened,
    and ``.github/`` / ``.shipcrew/`` entries get no tests.

The result is a pure function of the plan entry, so a re-import yields the
same list (no growth), and it is capped at :data:`MAX_OWNED_WITH_TESTS`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

from omnigent.shipcrew.branches import slugify

MAX_OWNED_WITH_TESTS = 150
_CODE_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".py"})
_WILDCARD = re.compile(r"[*?\[]")
_NO_TESTS_PREFIXES = (".github/", ".shipcrew/")
_TEST_DIRS = ("e2e/", "test/", "tests/")


def _is_test_glob(path: str) -> bool:
    """A test entry already (so re-applying adds nothing)."""
    name = posixpath.basename(path)
    return (
        path.startswith(_TEST_DIRS)
        or "__tests__" in path
        or ".test" in name
        or ".spec" in name
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _colocated(entry: str) -> list[str]:
    path = entry.strip()
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith(_NO_TESTS_PREFIXES) or _is_test_glob(path):
        return []
    wildcard = _WILDCARD.search(path)
    if wildcard is None:
        stem, ext = posixpath.splitext(posixpath.basename(path))
        if ext not in _CODE_EXTS or not stem:
            return []  # a directory (owned as a subtree) or not a source file
        base = posixpath.dirname(path)
        prefix = f"{base}/" if base else ""
        if ext == ".py":
            return [f"{prefix}test_{stem}.py", f"{prefix}{stem}_test.py"]
        return [f"{prefix}{stem}.test.*", f"{prefix}{stem}.spec.*"]
    literal = path[: wildcard.start()]
    if "/" not in literal:
        return []  # a root-level glob: never widened to the repository root
    directory = literal.rsplit("/", 1)[0]
    rest = path[len(directory) + 1 :]
    if rest == "**":
        return []  # the whole subtree is owned already
    scope = f"{directory}/**/" if rest.startswith("**/") else f"{directory}/"
    return [f"{scope}*.test.*", f"{scope}*.spec.*", f"{scope}__tests__/**"]


def own_test_globs(title: str, owned_paths: Sequence[str]) -> list[str]:
    """The test globs a task with *title* and *owned_paths* owns implicitly."""
    slug = slugify(title)
    globs = [f"e2e/{slug}*.spec.*", f"test/{slug}*", f"tests/{slug}*"]
    for entry in owned_paths:
        globs += _colocated(entry)
    return globs


def with_own_tests(title: str, owned_paths: Sequence[str]) -> list[str]:
    """*owned_paths* followed by the task's own test globs, deduplicated and capped.

    An empty *owned_paths* stays empty: no contract means the owned-paths
    guardrail abstains, and adding globs would create one.
    """
    owned = [p for p in owned_paths if p.strip()]
    if not owned:
        return []
    merged = list(dict.fromkeys([*owned, *own_test_globs(title, owned)]))
    return merged[: max(MAX_OWNED_WITH_TESTS, len(owned))]
