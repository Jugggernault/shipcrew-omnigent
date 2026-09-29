#!/usr/bin/env python3
"""A tiny stand-in for the ``gh`` CLI, for tests. Never talks to GitHub.

Point ``SHIPCREW_GH`` at it (or at a wrapper that execs it) and
``FAKE_GH_STATE`` at a JSON state file. It takes the real ``gh`` argv, so code
under test cannot tell the difference for the subcommands below:

- ``auth status``
- ``label create NAME [--color C] [--force]``
- ``issue create --title T --body B [--label L]...`` (prints the issue URL)
- ``issue list [--label L] [--state open|closed|all] [--limit N] [--json F]``
- ``issue view N [--json F]``, ``issue edit N [--add-assignee U] [--add-label L]``,
  ``issue close N``, ``issue reopen N``
- ``pr list [--state open|closed|merged|all] [--head B] [--limit N] [--json F]``,
  ``pr view N [--json F]``

``--repo``/``-R`` is accepted everywhere (``OWNER/REPO`` or a URL). Every call
is appended to ``state["calls"]``. State shape (all keys optional)::

    {"authenticated": true, "repo": "owner/repo", "next_number": 1,
     "labels": {"name": "color"},
     "issues": [{"number", "title", "body", "state", "labels": [{"name"}],
                 "assignees": [{"login"}], "url"}],
     "prs": [{"number", "state", "headRefName", "url", "title"}],
     "calls": [[argv...]]}
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_BOOL_FLAGS = {"--force", "--watch", "--fail-fast", "--draft", "--squash", "--delete-branch"}
_SHORT = {"-R": "--repo", "-q": "--jq", "-L": "--limit", "-l": "--label", "-t": "--title"}


def _parse(argv: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    positional: list[str] = []
    opts: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("-") and arg != "-":
            name, _, inline = arg.partition("=")
            name = _SHORT.get(name, name)
            if name in _BOOL_FLAGS:
                opts.setdefault(name, []).append("1")
            elif inline:
                opts.setdefault(name, []).append(inline)
            else:
                i += 1
                opts.setdefault(name, []).append(argv[i] if i < len(argv) else "")
        else:
            positional.append(arg)
        i += 1
    return positional, opts


def _one(opts: dict[str, list[str]], name: str, default: str = "") -> str:
    return opts.get(name, [default])[-1]


def _repo(state: dict[str, Any], opts: dict[str, list[str]]) -> str:
    raw = _one(opts, "--repo") or state.get("repo") or "owner/repo"
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    return raw.removesuffix(".git").strip("/")


def _pick(obj: dict[str, Any], opts: dict[str, list[str]]) -> dict[str, Any]:
    fields = _one(opts, "--json")
    if not fields:
        return obj
    return {f: obj.get(f) for f in fields.split(",") if f}


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def _find(items: list[dict[str, Any]], number: str) -> dict[str, Any] | None:
    return next((i for i in items if str(i.get("number")) == number.lstrip("#")), None)


def _issue(state: dict[str, Any], pos: list[str], opts: dict[str, list[str]]) -> int:
    issues: list[dict[str, Any]] = state.setdefault("issues", [])
    verb = pos[1] if len(pos) > 1 else ""
    if verb == "create":
        number = int(state.get("next_number") or 1)
        state["next_number"] = number + 1
        url = f"https://github.com/{_repo(state, opts)}/issues/{number}"
        issues.append(
            {
                "number": number,
                "title": _one(opts, "--title"),
                "body": _one(opts, "--body"),
                "state": "OPEN",
                "labels": [{"name": n} for n in opts.get("--label", [])],
                "assignees": [],
                "url": url,
            }
        )
        print(url)
        return 0
    if verb == "list":
        wanted = _one(opts, "--state", "open").upper()
        labels = set(opts.get("--label", []))
        out = [
            _pick(i, opts)
            for i in issues
            if (wanted == "ALL" or i.get("state") == wanted)
            and labels <= {label["name"] for label in i.get("labels", [])}
        ]
        print(json.dumps(out[: int(_one(opts, "--limit", "30"))]))
        return 0
    issue = _find(issues, pos[2]) if len(pos) > 2 else None
    if issue is None:
        return _fail("GraphQL: Could not resolve to an issue")
    if verb == "view":
        print(json.dumps(_pick(issue, opts)))
    elif verb == "edit":
        for login in opts.get("--add-assignee", []):
            issue["assignees"].append({"login": login})
        for name in opts.get("--add-label", []):
            issue["labels"].append({"name": name})
    elif verb in ("close", "reopen"):
        issue["state"] = "CLOSED" if verb == "close" else "OPEN"
    else:
        return _fail(f"unknown issue command {verb!r}")
    return 0


def _pr(state: dict[str, Any], pos: list[str], opts: dict[str, list[str]]) -> int:
    prs: list[dict[str, Any]] = state.setdefault("prs", [])
    verb = pos[1] if len(pos) > 1 else ""
    if verb == "list":
        wanted = _one(opts, "--state", "open").upper()
        head = _one(opts, "--head")
        out = [
            _pick(p, opts)
            for p in prs
            if (wanted == "ALL" or p.get("state") == wanted)
            and (not head or p.get("headRefName") == head)
        ]
        print(json.dumps(out[: int(_one(opts, "--limit", "30"))]))
        return 0
    if verb == "view":
        pr = _find(prs, pos[2]) if len(pos) > 2 else None
        if pr is None:
            return _fail("GraphQL: Could not resolve to a PullRequest")
        print(json.dumps(_pick(pr, opts)))
        return 0
    return _fail(f"unknown pr command {verb!r}")


def main(argv: list[str]) -> int:
    path = Path(os.environ.get("FAKE_GH_STATE") or "fake_gh_state.json")
    state: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault("calls", []).append(argv)
    pos, opts = _parse(argv)
    group = pos[0] if pos else ""
    if group == "auth" and pos[1:2] == ["status"]:
        code = 0 if state.get("authenticated", True) else 1
        if code:
            print("You are not logged into any GitHub hosts.", file=sys.stderr)
    elif group == "label" and pos[1:2] == ["create"] and len(pos) > 2:
        state.setdefault("labels", {})[pos[2]] = _one(opts, "--color", "ededed")
        code = 0
    elif group == "issue":
        code = _issue(state, pos, opts)
    elif group == "pr":
        code = _pr(state, pos, opts)
    else:
        code = _fail(f"fake gh: unsupported command {' '.join(argv[:2])!r}")
    path.write_text(json.dumps(state, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
