#!/usr/bin/env python3
"""A fake ``gh`` for shipcrew tests and the local e2e. Never talks to GitHub.

State lives in a JSON file (``SHIPCREW_FAKE_GH_STATE``); the "GitHub repo" is
a local bare git repository, so pushes are real pushes, ``pr merge --squash``
is a real squash merge into the bare repo's base branch, and
``pr update-branch`` really merges the base into the PR branch.

CI is emulated by running ``ci.command`` (a shell command, e.g. the repo's
test script) in a fresh checkout of the PR head, once per head SHA. A failing
command is a red check whose output ``gh run view <id> --log-failed`` returns,
so red -> fix -> green can be exercised. ``ci.async`` (default true) runs it
in the background and reports the check pending until it finishes;
``ci: null`` means "no checks reported".

Create a state file::

    scripts/shipcrew_fake_gh.py init --state /tmp/gh.json --remote /tmp/origin.git \\
        --ci-command 'sh ci.sh'

then point shipcrew at it: ``SHIPCREW_GH=scripts/shipcrew-fake-gh
SHIPCREW_FAKE_GH_STATE=/tmp/gh.json``.

Supported: ``auth status``, ``repo view``, ``label create``, ``api`` (no-op),
``pr create|view|list|ready|checks|merge|update-branch|close``,
``run view [--log-failed]``, ``issue create|view|list|edit|close|reopen|comment``.
``--repo``/``-R`` (``OWNER/REPO`` or a URL) is accepted on every command and
names the repository in the URLs it prints. Every invocation is appended to
``calls`` in the state file.

State keys (tests may seed or edit them directly)::

    {"authenticated": true,          # false: `auth status` exits 1
     "remote": "/path/origin.git",   # optional; git-backed commands need it
     "repo": "owner/name", "base": "main", "next": 1,
     "labels": {"name": "color"},
     "issues": {"1": {"number", "title", "body", "state": "OPEN|CLOSED",
                      "url", "labels": ["name"], "assignees": ["login"]}},
     "prs": {"7": {"number", "title", "body", "head", "base",
                   "state": "OPEN|CLOSED|MERGED", "isDraft", "url"}},
     "ci": {"command": "sh ci.sh", "async": true} | null,
     "calls": [[argv...]]}
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

STATE_ENV = "SHIPCREW_FAKE_GH_STATE"
_GIT_ID = ["-c", "user.name=shipcrew-fake-gh", "-c", "user.email=fake-gh@example.invalid"]
_EXIT_PENDING = 8


class Fail(Exception):
    """Exit with ``code`` after printing ``message`` to stderr."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# ── state ───────────────────────────────────────────────────────


def _state_path() -> Path:
    raw = os.environ.get(STATE_ENV)
    if not raw:
        raise Fail(f"fake gh: set {STATE_ENV} to the state file")
    return Path(raw)


@contextlib.contextmanager
def _locked_state() -> Iterator[dict[str, Any]]:
    """Read-modify-write the state file under an exclusive lock."""
    path = _state_path()
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
            yield state
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _defaults(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("repo", "shipcrew/fake")
    state.setdefault("base", "main")
    state.setdefault("next", 1)
    state.setdefault("next_run", 1000)
    for key in ("prs", "issues", "runs", "ci_by_sha", "labels"):
        state.setdefault(key, {})
    state.setdefault("calls", [])
    return state


def _remote(state: dict[str, Any]) -> str:
    if not state.get("remote"):
        raise Fail("fake gh: state has no 'remote' (path of the bare repository)")
    return str(state["remote"])


# ``--repo``/``-R`` of the current invocation (see ``_strip_repo``).
_REPO_OVERRIDE: str | None = None


def _repo_name(state: dict[str, Any]) -> str:
    return _REPO_OVERRIDE or state["repo"]


def _strip_repo(argv: list[str]) -> list[str]:
    """Drop ``--repo X``/``-R X``/``--repo=X`` (any position); remember the repo."""
    global _REPO_OVERRIDE
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--repo", "-R") and i + 1 < len(argv):
            raw, i = argv[i + 1], i + 2
        elif arg.startswith("--repo="):
            raw, i = arg.split("=", 1)[1], i + 1
        else:
            out.append(arg)
            i += 1
            continue
        for prefix in ("https://", "http://"):
            raw = raw.removeprefix(prefix)
        _REPO_OVERRIDE = raw.removeprefix("github.com/").removesuffix(".git").strip("/")
    return out


def _url(state: dict[str, Any], kind: str, number: int | str) -> str:
    return f"https://github.com/{_repo_name(state)}/{kind}/{number}"


def _next_number(state: dict[str, Any]) -> int:
    number = int(state["next"])
    state["next"] = number + 1
    return number


# ── git ─────────────────────────────────────────────────────────


def _git(*args: str, cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(
        ["git", *_GIT_ID, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and r.returncode:
        raise Fail(f"fake gh: git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _ref_sha(state: dict[str, Any], branch: str) -> str:
    if not state.get("remote"):
        return ""  # seeded PRs without a git remote
    r = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        cwd=state["remote"],
        check=False,
    )
    return r.stdout.strip()


@contextlib.contextmanager
def _clone(state: dict[str, Any]) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="fake-gh-"))
    try:
        _git("clone", "--quiet", _remote(state), str(tmp / "repo"), cwd=tmp)
        yield tmp / "repo"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── output helpers ──────────────────────────────────────────────


def _jq(value: Any, expr: str) -> list[Any]:
    """Tiny jq subset: ``.``, ``.a.b``, ``.[0]``, ``.[]``, chained."""
    values = [value]
    rest = expr.strip()
    if rest in ("", "."):
        return values
    if not rest.startswith("."):
        raise Fail(f"fake gh: unsupported -q expression {expr!r}")
    i = 0
    while i < len(rest):
        if rest[i] == ".":
            i += 1
            continue
        if rest[i] == "[":
            end = rest.index("]", i)
            inner = rest[i + 1 : end]
            i = end + 1
            nxt: list[Any] = []
            for v in values:
                if inner == "":
                    nxt.extend(v if isinstance(v, list) else [])
                elif isinstance(v, list) and -len(v) <= int(inner) < len(v):
                    nxt.append(v[int(inner)])
                else:
                    nxt.append(None)
            values = nxt
            continue
        j = i
        while j < len(rest) and rest[j] not in ".[":
            j += 1
        key = rest[i:j]
        i = j
        values = [v.get(key) if isinstance(v, dict) else None for v in values]
    return values


def _emit(data: Any, fields: str | None, query: str | None) -> None:
    if fields is not None:
        wanted = [f.strip() for f in fields.split(",") if f.strip()]

        def pick(obj: dict[str, Any]) -> dict[str, Any]:
            return {k: obj.get(k) for k in wanted}

        data = [pick(d) for d in data] if isinstance(data, list) else pick(data)
    if query is None:
        print(json.dumps(data, indent=2))
        return
    for v in _jq(data, query):
        if v is None:
            print("")  # gh prints null results as an empty line
        elif isinstance(v, str):
            print(v)
        else:
            print(json.dumps(v))


# ── pull requests ───────────────────────────────────────────────


def _pr_obj(state: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    head_sha = pr.get("mergedHeadOid") if pr["state"] == "MERGED" else None
    return {
        "number": pr["number"],
        "title": pr.get("title", ""),
        "body": pr.get("body", ""),
        "state": pr["state"],
        "isDraft": bool(pr.get("isDraft")),
        "url": pr.get("url") or _url(state, "pull", pr["number"]),
        "headRefName": pr["head"],
        "baseRefName": pr.get("base", state["base"]),
        "headRefOid": head_sha or _ref_sha(state, pr["head"]),
        "labels": [{"name": n} for n in pr.get("labels", [])],
        "mergedAt": pr.get("mergedAt"),
        "mergeCommit": {"oid": pr["mergeCommit"]} if pr.get("mergeCommit") else None,
        "closingIssuesReferences": [],
    }


def _find_pr(state: dict[str, Any], ref: str | None) -> dict[str, Any]:
    if ref is None:
        raise Fail("fake gh: a PR number is required")
    ref = ref.rstrip("/").rsplit("/", 1)[-1]
    if ref.isdigit() and ref in state["prs"]:
        return state["prs"][ref]
    for pr in state["prs"].values():
        if pr["head"] == ref and pr["state"] == "OPEN":
            return pr
    raise Fail(f"no pull requests found for {ref!r}")


def pr_create(state: dict[str, Any], a: argparse.Namespace) -> None:
    head, base = a.head, a.base or state["base"]
    if not head:
        raise Fail("fake gh: pr create needs --head")
    if not _ref_sha(state, head):
        raise Fail(
            f"pull request create failed: head branch {head!r} does not exist on the remote"
        )
    for pr in state["prs"].values():
        if pr["head"] == head and pr["state"] == "OPEN":
            raise Fail(
                f'a pull request for branch "{head}" into branch "{base}" already exists:\n'
                f"{pr['url']}"
            )
    number = _next_number(state)
    pr = {
        "number": number,
        "title": a.title or head,
        "body": a.body or "",
        "head": head,
        "base": base,
        "state": "OPEN",
        "isDraft": bool(a.draft),
        "url": _url(state, "pull", number),
        "labels": list(a.label or []),
    }
    state["prs"][str(number)] = pr
    print(pr["url"])


def pr_view(state: dict[str, Any], a: argparse.Namespace) -> None:
    pr = _find_pr(state, a.target)
    obj = _pr_obj(state, pr)
    if a.json is None and a.q is None:
        print(f"{obj['title']} #{obj['number']}\nstate:\t{obj['state']}\nurl:\t{obj['url']}")
        return
    _emit(obj, a.json, a.q)


def pr_list(state: dict[str, Any], a: argparse.Namespace) -> None:
    want = (a.state or "open").upper()
    prs = [
        _pr_obj(state, pr)
        for pr in sorted(state["prs"].values(), key=lambda p: -p["number"])
        if (want == "ALL" or pr["state"] == want) and (a.head is None or pr["head"] == a.head)
    ][: a.limit]
    _emit(prs, a.json or "number,title,headRefName,state,url", a.q)


def pr_ready(state: dict[str, Any], a: argparse.Namespace) -> None:
    pr = _find_pr(state, a.target)
    pr["isDraft"] = bool(a.undo)
    what = "a draft" if a.undo else "marked as ready for review"
    print(f"✓ Pull request #{pr['number']} is {what}")


def pr_close(state: dict[str, Any], a: argparse.Namespace) -> None:
    pr = _find_pr(state, a.target)
    pr["state"] = "CLOSED"
    print(f"✓ Closed pull request #{pr['number']}")


def _start_ci(state: dict[str, Any], sha: str, pr_number: int) -> dict[str, Any]:
    run_id = str(state["next_run"])
    state["next_run"] = int(run_id) + 1
    run = {"id": run_id, "sha": sha, "pr": pr_number, "status": "in_progress", "conclusion": None,
           "log": "", "started": time.time()}  # fmt: skip
    state["runs"][run_id] = run
    state["ci_by_sha"][sha] = run_id
    ci = state.get("ci") or {}
    if ci.get("async", True):
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "__ci-run", run_id],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run["pid"] = proc.pid
    else:
        _execute_ci(state, run)
    return run


def _execute_ci(state: dict[str, Any], run: dict[str, Any]) -> None:
    """Run ``ci.command`` in a checkout of ``run['sha']``; fill conclusion + log."""
    ci = state.get("ci") or {}
    with _clone(state) as repo:
        _git("checkout", "--quiet", "--detach", run["sha"], cwd=repo)
        try:
            r = subprocess.run(
                ci["command"],
                shell=True,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=float(ci.get("timeout", 600)),
                env={**os.environ, "CI": "1", "SHIPCREW_FAKE_CI_SHA": run["sha"]},
            )
            ok, output = r.returncode == 0, r.stdout + r.stderr
        except subprocess.TimeoutExpired as exc:
            ok, output = False, f"CI command timed out after {exc.timeout}s"
    run["status"] = "completed"
    run["conclusion"] = "success" if ok else "failure"
    run["log"] = output[-100_000:]
    run["finished"] = time.time()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    with contextlib.suppress(ChildProcessError, OSError):
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            return False
    return True


def _check_for(state: dict[str, Any], pr: dict[str, Any], *, wait: bool) -> dict[str, Any] | None:
    ci = state.get("ci")
    if not ci or not ci.get("command"):
        return None
    sha = _ref_sha(state, pr["head"])
    run_id = state["ci_by_sha"].get(sha)
    run = state["runs"].get(run_id) if run_id else None
    if run is None:
        run = _start_ci(state, sha, pr["number"])
    if run["status"] != "completed" and not _pid_alive(run.get("pid")) and "pid" in run:
        run.update(status="completed", conclusion="failure", log="fake CI runner died")
    if run["status"] != "completed" and wait:
        _execute_ci(state, run)
    conclusion = run.get("conclusion")
    bucket = {"success": "pass", "failure": "fail"}.get(conclusion or "", "pending")
    return {
        "name": ci.get("name", "ci"),
        "state": {"pass": "SUCCESS", "fail": "FAILURE"}.get(bucket, "IN_PROGRESS"),
        "bucket": bucket,
        "link": f"https://github.com/{_repo_name(state)}/actions/runs/{run['id']}/job/{run['id']}",
        "workflow": ci.get("name", "ci"),
        "description": "",
        "event": "pull_request",
        "startedAt": run.get("started"),
        "completedAt": run.get("finished"),
    }


def pr_checks(state: dict[str, Any], a: argparse.Namespace) -> int:
    pr = _find_pr(state, a.target)
    check = _check_for(state, pr, wait=bool(a.watch))
    if check is None:
        raise Fail(f"no checks reported on the '{pr['head']}' branch")
    if a.json is not None:
        _emit([check], a.json, a.q)
    else:
        print(f"{check['name']}\t{check['bucket']}\t0s\t{check['link']}")
    return {"pass": 0, "fail": 1}.get(check["bucket"], _EXIT_PENDING)


def pr_update_branch(state: dict[str, Any], a: argparse.Namespace) -> None:
    pr = _find_pr(state, a.target)
    with _clone(state) as repo:
        _git("checkout", "--quiet", pr["head"], cwd=repo)
        base = f"origin/{pr['base']}"
        if (
            _git("merge-base", "--is-ancestor", base, "HEAD", cwd=repo, check=False).returncode
            == 0
        ):
            print("✓ PR branch already up-to-date")
            return
        merged = _git("merge", "--no-edit", "--quiet", base, cwd=repo, check=False)
        if merged.returncode:
            raise Fail("Cannot update PR branch due to conflicts")
        _git("push", "--quiet", "origin", f"HEAD:refs/heads/{pr['head']}", cwd=repo)
    print("✓ PR branch updated")


def pr_merge(state: dict[str, Any], a: argparse.Namespace) -> None:
    pr = _find_pr(state, a.target)
    if pr["state"] != "OPEN":
        raise Fail(f"Pull request #{pr['number']} is not open")
    if pr["isDraft"]:
        raise Fail(f"Pull request #{pr['number']} is still a draft")
    if not a.squash:
        raise Fail("fake gh: only --squash merges are supported")
    head_sha = _ref_sha(state, pr["head"])
    if a.match_head_commit and a.match_head_commit != head_sha:
        raise Fail(
            f"Pull request #{pr['number']} head is {head_sha}, "
            f"not the expected {a.match_head_commit}"
        )
    with _clone(state) as repo:
        _git("checkout", "--quiet", pr["base"], cwd=repo)
        squashed = _git("merge", "--squash", f"origin/{pr['head']}", cwd=repo, check=False)
        if squashed.returncode:
            raise Fail(
                f"Pull request #{pr['number']} is not mergeable: "
                "the merge commit cannot be cleanly created."
            )
        _git("commit", "--quiet", "--allow-empty", "-m", f"{pr['title']} (#{pr['number']})",
             cwd=repo)  # fmt: skip
        merge_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _git("push", "--quiet", "origin", f"HEAD:refs/heads/{pr['base']}", cwd=repo)
        if a.delete_branch:
            _git("push", "--quiet", "origin", "--delete", pr["head"], cwd=repo)
    pr.update(state="MERGED", mergedAt=time.time(), mergeCommit=merge_sha, mergedHeadOid=head_sha)
    for word in ("Closes", "Fixes", "Resolves"):
        for line in pr["body"].splitlines():
            if line.strip().startswith(f"{word} #"):
                number = line.strip().split("#", 1)[1].split()[0]
                if number in state["issues"]:
                    state["issues"][number]["state"] = "CLOSED"
    print(f"✓ Squashed and merged pull request #{pr['number']} ({pr['title']})")


# ── runs ────────────────────────────────────────────────────────


def run_view(state: dict[str, Any], a: argparse.Namespace) -> None:
    run = state["runs"].get(str(a.run_id))
    if run is None:
        raise Fail(f"could not find any workflow run with ID {a.run_id}")
    if a.log_failed:
        if run.get("conclusion") != "failure":
            return
        for line in (run.get("log") or "").splitlines():
            print(f"ci\tRun ci\t{line}")
        return
    obj = {
        "databaseId": int(run["id"]),
        "headSha": run["sha"],
        "status": run["status"],
        "conclusion": run.get("conclusion") or "",
        "url": f"https://github.com/{_repo_name(state)}/actions/runs/{run['id']}",
    }
    _emit(obj, a.json, a.q) if (a.json or a.q) else print(json.dumps(obj))


# ── issues ──────────────────────────────────────────────────────


def _issue_obj(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "url": issue["url"],
        "labels": [{"name": n} for n in issue["labels"]],
        "assignees": [{"login": n} for n in issue["assignees"]],
        "comments": [{"body": c} for c in issue.get("comments", [])],
    }


def _find_issue(state: dict[str, Any], ref: str) -> dict[str, Any]:
    ref = ref.rstrip("/").rsplit("/", 1)[-1].lstrip("#")
    if ref not in state["issues"]:
        raise Fail(f"Could not resolve to an issue or pull request with the number of {ref}.")
    return state["issues"][ref]


def _split_labels(values: list[str] | None) -> list[str]:
    return [p.strip() for v in values or [] for p in v.split(",") if p.strip()]


def issue_create(state: dict[str, Any], a: argparse.Namespace) -> None:
    number = _next_number(state)
    issue = {
        "number": number,
        "title": a.title or "",
        "body": a.body or "",
        "state": "OPEN",
        "url": _url(state, "issues", number),
        "labels": _split_labels(a.label),
        "assignees": _split_labels(a.assignee),
    }
    state["issues"][str(number)] = issue
    print(issue["url"])


def issue_view(state: dict[str, Any], a: argparse.Namespace) -> None:
    obj = _issue_obj(_find_issue(state, a.target))
    if a.json is None and a.q is None:
        print(f"{obj['title']} #{obj['number']}\nstate:\t{obj['state']}\n\n{obj['body']}")
        return
    _emit(obj, a.json, a.q)


def issue_list(state: dict[str, Any], a: argparse.Namespace) -> None:
    want = (a.state or "open").upper()
    labels = set(_split_labels(a.label))
    issues = [
        _issue_obj(i)
        for i in sorted(state["issues"].values(), key=lambda i: -i["number"])
        if (want == "ALL" or i["state"] == want) and labels <= set(i["labels"])
    ][: a.limit]
    _emit(issues, a.json or "number,title,state,labels,url", a.q)


def issue_edit(state: dict[str, Any], a: argparse.Namespace) -> None:
    issue = _find_issue(state, a.target)
    if a.title is not None:
        issue["title"] = a.title
    if a.body is not None:
        issue["body"] = a.body
    for name in _split_labels(a.add_label):
        if name not in issue["labels"]:
            issue["labels"].append(name)
    drop = set(_split_labels(a.remove_label))
    issue["labels"] = [n for n in issue["labels"] if n not in drop]
    for name in _split_labels(a.add_assignee):
        if name not in issue["assignees"]:
            issue["assignees"].append(name)
    drop = set(_split_labels(a.remove_assignee))
    issue["assignees"] = [n for n in issue["assignees"] if n not in drop]
    print(issue["url"])


def issue_state(state: dict[str, Any], a: argparse.Namespace) -> None:
    issue = _find_issue(state, a.target)
    issue["state"] = "CLOSED" if a.issue_cmd == "close" else "OPEN"
    print(f"✓ {a.issue_cmd.capitalize()}d issue #{issue['number']}")


def issue_comment(state: dict[str, Any], a: argparse.Namespace) -> None:
    issue = _find_issue(state, a.target)
    issue.setdefault("comments", []).append(a.body or "")
    print(issue["url"] + "#issuecomment-1")


# ── CLI ─────────────────────────────────────────────────────────


def _json_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", default=None)
    p.add_argument("-q", "--jq", dest="q", default=None)


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gh", add_help=False)
    sub = root.add_subparsers(dest="cmd", required=True)

    auth = sub.add_parser("auth").add_subparsers(dest="auth_cmd", required=True)
    auth.add_parser("status")

    repo = sub.add_parser("repo").add_subparsers(dest="repo_cmd", required=True)
    rv = repo.add_parser("view")
    rv.add_argument("target", nargs="?")
    _json_opts(rv)

    label = sub.add_parser("label").add_subparsers(dest="label_cmd", required=True)
    lc = label.add_parser("create")
    lc.add_argument("name")
    lc.add_argument("--color")
    lc.add_argument("--description")
    lc.add_argument("--force", action="store_true")

    api = sub.add_parser("api")
    api.add_argument("rest", nargs=argparse.REMAINDER)

    pr = sub.add_parser("pr").add_subparsers(dest="pr_cmd", required=True)
    c = pr.add_parser("create")
    for opt in ("--title", "--body", "--head", "--base"):
        c.add_argument(opt)
    c.add_argument("--draft", action="store_true")
    c.add_argument("--label", action="append")
    for name in ("view", "ready", "close", "checks", "update-branch", "merge"):
        p = pr.add_parser(name)
        p.add_argument("target", nargs="?")
        _json_opts(p)
        if name == "ready":
            p.add_argument("--undo", action="store_true")
        if name == "checks":
            p.add_argument("--watch", action="store_true")
            p.add_argument("--fail-fast", action="store_true")
            p.add_argument("--interval")
            p.add_argument("--required", action="store_true")
        if name == "merge":
            p.add_argument("--squash", action="store_true")
            p.add_argument("--delete-branch", action="store_true")
            p.add_argument("--auto", action="store_true")
            p.add_argument("--match-head-commit")
        if name == "update-branch":
            p.add_argument("--rebase", action="store_true")
    pl = pr.add_parser("list")
    pl.add_argument("--head")
    pl.add_argument("--state")
    pl.add_argument("--limit", type=int, default=30)
    _json_opts(pl)

    run = sub.add_parser("run").add_subparsers(dest="run_cmd", required=True)
    rview = run.add_parser("view")
    rview.add_argument("run_id")
    rview.add_argument("--log-failed", action="store_true")
    rview.add_argument("--log", action="store_true")
    _json_opts(rview)

    issue = sub.add_parser("issue").add_subparsers(dest="issue_cmd", required=True)
    ic = issue.add_parser("create")
    ic.add_argument("--title")
    ic.add_argument("--body")
    ic.add_argument("--label", action="append")
    ic.add_argument("--assignee", action="append")
    iv = issue.add_parser("view")
    iv.add_argument("target")
    _json_opts(iv)
    il = issue.add_parser("list")
    il.add_argument("--state")
    il.add_argument("--label", action="append")
    il.add_argument("--limit", type=int, default=30)
    _json_opts(il)
    ie = issue.add_parser("edit")
    ie.add_argument("target")
    ie.add_argument("--title")
    ie.add_argument("--body")
    for opt in ("--add-label", "--remove-label", "--add-assignee", "--remove-assignee"):
        ie.add_argument(opt, action="append")
    for name in ("close", "reopen"):
        p = issue.add_parser(name)
        p.add_argument("target")
        p.add_argument("--comment")
        p.add_argument("--reason")
    icm = issue.add_parser("comment")
    icm.add_argument("target")
    icm.add_argument("--body")
    return root


def _dispatch(state: dict[str, Any], a: argparse.Namespace) -> int:
    if a.cmd == "auth":
        if state.get("authenticated") is False:
            print("You are not logged into any GitHub hosts. To log in, run: gh auth login",
                  file=sys.stderr)  # fmt: skip
            return 1
        print("github.com\n  ✓ Logged in to github.com account fake (fake gh)")
        print("  - Token scopes: 'repo', 'workflow'")
        return 0
    if a.cmd == "repo":
        name = _repo_name(state)
        obj = {"url": f"https://github.com/{name}", "nameWithOwner": name}
        _emit(obj, a.json, a.q) if (a.json or a.q) else print(obj["url"])
        return 0
    if a.cmd == "label":
        state["labels"][a.name] = a.color or "ededed"
        return 0
    if a.cmd == "api":
        return 0
    if a.cmd == "run":
        run_view(state, a)
        return 0
    if a.cmd == "pr":
        if a.pr_cmd == "checks":
            return pr_checks(state, a)
        handlers = {
            "create": pr_create,
            "view": pr_view,
            "list": pr_list,
            "ready": pr_ready,
            "close": pr_close,
            "merge": pr_merge,
            "update-branch": pr_update_branch,
        }
        handlers[a.pr_cmd](state, a)
        return 0
    if a.cmd == "issue":
        handlers = {
            "create": issue_create,
            "view": issue_view,
            "list": issue_list,
            "edit": issue_edit,
            "close": issue_state,
            "reopen": issue_state,
            "comment": issue_comment,
        }
        handlers[a.issue_cmd](state, a)
        return 0
    raise Fail(f"fake gh: unsupported command {a.cmd!r}")


def _ci_run(run_id: str) -> int:
    """Background CI job: run outside the state lock, then record the result."""
    with _locked_state() as state:
        _defaults(state)
        run = dict(state["runs"][run_id])
        snapshot = {"remote": state["remote"], "ci": state.get("ci")}
    _execute_ci(snapshot, run)
    with _locked_state() as state:
        state["runs"][run_id].update(
            status=run["status"], conclusion=run["conclusion"], log=run["log"],
            finished=run.get("finished"),
        )  # fmt: skip
    return 0


def _init(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="shipcrew_fake_gh.py init")
    p.add_argument("--state", required=True)
    p.add_argument("--remote", required=True, help="path of the bare repository")
    p.add_argument("--repo", default="shipcrew/fake", help="owner/name used in URLs")
    p.add_argument("--base", default="main")
    p.add_argument("--ci-command", default=None, help="shell command; omit for no checks")
    p.add_argument("--ci-sync", action="store_true", help="run CI inline in `pr checks`")
    a = p.parse_args(argv)
    state = {
        "remote": str(Path(a.remote).resolve()),
        "repo": a.repo,
        "base": a.base,
        "ci": {"command": a.ci_command, "async": not a.ci_sync} if a.ci_command else None,
    }
    Path(a.state).write_text(json.dumps(_defaults(state), indent=2))
    print(a.state)
    return 0


def main(argv: list[str]) -> int:
    global _REPO_OVERRIDE
    _REPO_OVERRIDE = None
    if argv[:1] == ["init"]:
        return _init(argv[1:])
    if argv[:1] == ["__ci-run"]:
        return _ci_run(argv[1])
    try:
        args = _parser().parse_args(_strip_repo(list(argv)))
    except SystemExit as exc:
        return int(exc.code or 2)
    try:
        with _locked_state() as state:
            _defaults(state)
            state["calls"].append(argv)
            return _dispatch(state, args)
    except Fail as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
