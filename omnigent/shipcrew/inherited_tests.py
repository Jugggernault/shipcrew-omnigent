"""A task inherits the existing tests of the modules it owns (granted at task start).

A Foundation task used to write tests that pin the stub behaviour of API
routes / pages a later task replaces (``test/foundation-api.test.ts``
importing ``@/app/api/polls/route``). The later task then had to change a
test it does not own: an approval card, and a merge hold. At start the server
grants the task ownership of every existing test file whose imports from the
app are *all* modules the task owns (:func:`grantable_tests`).

Conservative by design:

* a test with no app import (an e2e spec that only visits URLs) is not granted;
* one app import the task does not own (``@/lib/db``, a shared contract type)
  and the test is not granted;
* a test another active task owns, or the task owns already, is skipped.

Imports are read statically (``import ... from``, ``import "x"``, ``export ...
from``, ``require("x")``, ``import("x")``). App modules are relative
specifiers, ``@/`` / ``~/`` aliases (the repo root or ``src/``) and bare
specifiers whose first segment is a top-level directory of the repo
(``lib/db``); anything else is a package and ignored.
"""

from __future__ import annotations

import logging
import posixpath
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence

from omnigent.shipcrew.policies import _GlobSet

_logger = logging.getLogger(__name__)

TEST_FILE_GLOBS = ("test/**", "tests/**", "e2e/**", "**/*.test.*", "**/*.spec.*")
_CODE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_SOURCE_EXTS = frozenset(_CODE_EXTS)
_IMPORT = re.compile(
    r"""(?:\bimport\s+(?:type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?|\bexport\s+[\w*{}\s,$]*?\s+from\s+"""
    r"""|\brequire\s*\(\s*|\bimport\s*\(\s*)["']([^"'\n]+)["']"""
)
_MAX_TEST_FILES = 400
_MAX_TEST_BYTES = 256 * 1024
_GIT_TIMEOUT_S = 20.0


def import_specifiers(source: str) -> list[str]:
    """Module specifiers a JS/TS source imports, in order."""
    return _IMPORT.findall(source)


def _app_module(spec: str, test_path: str, top_dirs: frozenset[str]) -> list[str] | None:
    """Repo paths *spec* may name (without extension), or ``None`` for a package."""
    if spec.startswith(("./", "../")) or spec in (".", ".."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(test_path), spec))
        return [base]
    if spec.startswith(("@/", "~/")):
        rest = spec[2:]
        return [posixpath.normpath(rest), posixpath.normpath(f"src/{rest}")]
    first = spec.split("/", 1)[0]
    if "/" in spec and first in top_dirs:
        return [posixpath.normpath(spec)]
    return None


def _candidates(base: str) -> list[str]:
    if posixpath.splitext(base)[1] in _SOURCE_EXTS:
        return [base]
    return [
        base,
        *(base + ext for ext in _CODE_EXTS),
        *(f"{base}/index{ext}" for ext in _CODE_EXTS),
    ]


def _owned_module(bases: list[str], files: frozenset[str], owned: _GlobSet) -> bool:
    """Whether the module *bases* names is owned (its file, else any spelling)."""
    spellings = [c for base in bases for c in _candidates(base)]
    existing = [c for c in spellings if c in files]
    if existing:
        return owned.match(existing[0])
    return any(owned.match(c) for c in spellings)


def is_test_file(path: str) -> bool:
    """Whether a repo-relative *path* is a JS/TS test file this module considers."""
    return posixpath.splitext(path)[1] in _SOURCE_EXTS and _GlobSet(TEST_FILE_GLOBS).match(path)


def grantable_tests(
    files: Mapping[str, str],
    owned_paths: Sequence[str],
    other_owned: Iterable[str] = (),
    *,
    all_files: Iterable[str] | None = None,
) -> list[str]:
    """Test files of *files* (``{path: source}``) the task may take over.

    :param owned_paths: The task's owned globs.
    :param other_owned: Globs owned by the mission's other active tasks.
    :param all_files: Every repo file (import resolution); default *files*.
    :returns: Sorted repo-relative paths.
    """
    owned = _GlobSet([p for p in owned_paths if p.strip()])
    others = _GlobSet([p for p in other_owned if p.strip()])
    known = frozenset(all_files if all_files is not None else files)
    top_dirs = frozenset(p.split("/", 1)[0] for p in known if "/" in p)
    granted: list[str] = []
    for path, source in files.items():
        if not is_test_file(path) or owned.match(path) or others.match(path):
            continue
        modules = [
            m
            for m in (_app_module(s, path, top_dirs) for s in import_specifiers(source))
            if m is not None
        ]
        if modules and all(_owned_module(m, known, owned) for m in modules):
            granted.append(path)
    return sorted(granted)


def _git(repo: str, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        input=input_text,
        check=True,
    )
    return result.stdout


def read_test_sources(repo: str, ref: str) -> tuple[dict[str, str], list[str]]:
    """``({test path: source}, every path)`` of the tree at *ref* in *repo*."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--name-only", ref)
    paths = [p for p in listing.split("\0") if p]
    tests = [p for p in paths if is_test_file(p)][:_MAX_TEST_FILES]
    sources: dict[str, str] = {}
    for path in tests:
        try:
            text = _git(repo, "show", f"{ref}:{path}")
        except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
            continue
        if len(text) <= _MAX_TEST_BYTES:
            sources[path] = text
    return sources, paths


def grant_inherited_tests(
    repo: str, ref: str, owned_paths: Sequence[str], other_owned: Iterable[str] = ()
) -> list[str]:
    """:func:`grantable_tests` on the tree at *ref*; ``[]`` on any git failure."""
    if not [p for p in owned_paths if p.strip()]:
        return []
    try:
        sources, paths = read_test_sources(repo, ref)
    except (subprocess.SubprocessError, OSError) as exc:
        _logger.warning("shipcrew: test grant scan failed in %s at %s: %s", repo, ref, exc)
        return []
    return grantable_tests(sources, owned_paths, other_owned, all_files=paths)
