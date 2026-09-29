#!/usr/bin/env python3
"""A fake ``vercel`` CLI for shipcrew ship-stage tests and local e2e runs.

Never touches Vercel. Put it first on ``PATH`` as ``vercel`` (a symlink named
``vercel`` in a temp bin dir) and/or point ``SHIPCREW_VERCEL`` at it.

Settings come from ``fake-vercel.json`` next to the invoked path (the temp bin
dir: agent sessions do not inherit the server's environment, only ``PATH``),
else from the environment:

* ``url`` / ``SHIPCREW_FAKE_VERCEL_URL``: the production URL ``deploy`` prints
  (default ``https://<project>.vercel.app``).
* ``log`` / ``SHIPCREW_FAKE_VERCEL_LOG``: append one JSON line per call (argv, cwd).
* ``logged_out`` / ``SHIPCREW_FAKE_VERCEL_LOGGED_OUT=1``: ``whoami`` fails like
  a logged-out CLI.
* ``fail_deploy`` / ``SHIPCREW_FAKE_VERCEL_FAIL_DEPLOY=<message>``: ``deploy``
  fails with it.

Supported: ``whoami``, ``link --yes --project <name>``, ``deploy --prod --yes``,
``ls``, ``inspect <url> [--logs]``, ``logs <url>``, ``--version``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _settings() -> dict[str, Any]:
    config = Path(sys.argv[0]).parent / "fake-vercel.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    env: dict[str, Any] = {
        "url": os.environ.get("SHIPCREW_FAKE_VERCEL_URL"),
        "log": os.environ.get("SHIPCREW_FAKE_VERCEL_LOG"),
        "logged_out": os.environ.get("SHIPCREW_FAKE_VERCEL_LOGGED_OUT") == "1",
        "fail_deploy": os.environ.get("SHIPCREW_FAKE_VERCEL_FAIL_DEPLOY"),
    }
    merged = {k: v for k, v in env.items() if v}
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if v})
    return merged


SETTINGS = _settings()


def _log(argv: list[str]) -> None:
    path = SETTINGS.get("log")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "at": time.time()}) + "\n")


def _project() -> str:
    try:
        data = json.loads(Path(".vercel/project.json").read_text(encoding="utf-8"))
        return str(data.get("projectName") or "app")
    except (OSError, ValueError):
        return Path.cwd().name.lower()


def main(argv: list[str]) -> int:
    _log(argv)
    if not argv or argv[0] in ("--version", "-v"):
        print("Vercel CLI 51.0.0 (shipcrew fake)")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "whoami":
        if SETTINGS.get("logged_out"):
            print("Error: No existing credentials found. Run `vercel login`.", file=sys.stderr)
            return 1
        print("shipcrew-fake")
        return 0
    if cmd == "link":
        if "--project" not in rest:
            print("Error: --project is required in this fake", file=sys.stderr)
            return 1
        name = rest[rest.index("--project") + 1]
        Path(".vercel").mkdir(exist_ok=True)
        Path(".vercel/project.json").write_text(
            json.dumps({"projectId": "prj_fake", "orgId": "team_fake", "projectName": name})
        )
        print(f"✅  Linked to shipcrew-fake/{name} (created .vercel)")
        return 0
    if cmd == "deploy":
        failure = SETTINGS.get("fail_deploy")
        if failure:
            print(f"Error: {failure}", file=sys.stderr)
            return 1
        project = _project()
        url = SETTINGS.get("url") or f"https://{project}.vercel.app"
        # A configured URL stands for both, so the server's URL check can reach it.
        unique = SETTINGS.get("url") or f"https://{project}-abc123-shipcrew-fake.vercel.app"
        print(f"🔍  Inspect: https://vercel.com/shipcrew-fake/{project}/abc123 [1s]")
        print(f"✅  Production: {unique} [3s]")
        print(f"🔗  Aliased: {url} [3s]")
        return 0
    if cmd in ("ls", "list"):
        print("  Age  Deployment  Status  Environment")
        print(f"  1m   https://{_project()}-abc123.vercel.app  ● Ready  Production")
        return 0
    if cmd in ("inspect", "logs"):
        print("status  ● Ready\nBuild completed (fake)")
        return 0
    print(f"Error: fake vercel does not support `{' '.join(argv)}`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
