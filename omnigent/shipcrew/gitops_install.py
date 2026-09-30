"""The server installs the per-project GitOps files for the ``argocd`` deploy target.

Like :mod:`omnigent.shipcrew.ci_install`, and with the same git plumbing (a
temporary index on ``origin/<base>``, ``commit-tree``, a fast-forward push; the
main checkout is never touched), one ``chore: shipcrew gitops`` commit adds:

- ``deploy/k8s/base/`` - Deployment (small requests/limits, readiness and
  liveness on ``/``), Service, Ingress. No placeholder: every name is ``app``,
  the namespace isolates projects and previews.
- ``deploy/k8s/overlays/main/kustomization.yaml`` - the production preview:
  image ``ghcr.io/<owner>/<repo>`` and host ``<app>.<base domain>``.
- ``.github/workflows/gitops.yml`` - builds and pushes the image to GHCR with
  the built-in ``GITHUB_TOKEN`` and moves the ``gitops/main`` branch.

PR previews are not files in the repo: the ApplicationSet (cluster side, see
:mod:`omnigent.shipcrew.deploy_targets.argocd`) renders ``deploy/k8s/base``
with the PR's image and host. Files already on the base are left alone, so a
project may own its manifests after the first install.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnigent.shipcrew.ci_install import (
    _PUSH_TIMEOUT_S,
    CiInstall,
    _fast_forward_local_base,
    _git,
    _identity_env,
    _out,
)

_logger = logging.getLogger(__name__)

GITOPS_COMMIT_MESSAGE = "chore: shipcrew gitops"
GITOPS_TEMPLATES = Path(__file__).parent / "templates" / "gitops"
MAIN_OVERLAY = "deploy/k8s/overlays/main"
BASE_PATH = "deploy/k8s/base"
DEPLOY_BRANCH = "gitops/main"
# Leaves room for "-pr-<number>" in a 63-char DNS label.
_APP_MAX = 40
_GITHUB = re.compile(
    r"github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def github_slug(repo_url: str | None) -> tuple[str, str] | None:
    """``(owner, repo)`` of a GitHub URL (https or ssh), else ``None``."""
    match = _GITHUB.search((repo_url or "").strip())
    return (match.group("owner"), match.group("repo")) if match else None


def app_name(repo_url: str | None, repo_path: str | Path) -> str:
    """A DNS label for the project: namespace, Application name, host prefix."""
    slug = github_slug(repo_url)
    raw = slug[1] if slug else Path(repo_path).name
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    name = re.sub(r"-{2,}", "-", name).strip("-")[:_APP_MAX].strip("-")
    return name or "app"


def image_repo(owner: str, repo: str) -> str:
    """GHCR image name; GHCR (and the workflow's ``${GITHUB_REPOSITORY,,}``) is lowercase."""
    return f"ghcr.io/{owner.lower()}/{repo.lower()}"


def main_host(app: str, base_domain: str) -> str:
    return f"{app}.{base_domain}"


def render_repo_files(image: str, host: str) -> dict[str, str]:
    """Repo path -> content of every GitOps file, placeholders filled in."""
    files: dict[str, str] = {}
    for src in sorted(GITOPS_TEMPLATES.rglob("*")):
        if src.is_file():
            text = src.read_text(encoding="utf-8")
            text = text.replace("__IMAGE__", image).replace("__HOST__", host)
            files[src.relative_to(GITOPS_TEMPLATES).as_posix()] = text
    return files


def _commit_with_files(repo: Path, parent: str, files: Mapping[str, str]) -> str | None:
    with tempfile.TemporaryDirectory(prefix="shipcrew-gitops-") as tmp:
        index = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        if _git(["read-tree", parent], repo, env=index).returncode:
            return None
        for path, content in files.items():
            blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=content)
            if blob.returncode:
                return None
            info = f"100644,{blob.stdout.strip()},{path}"
            if _git(["update-index", "--add", "--cacheinfo", info], repo, env=index).returncode:
                return None
        tree = _git(["write-tree"], repo, env=index)
    if tree.returncode:
        return None
    commit = _git(
        ["commit-tree", tree.stdout.strip(), "-p", parent, "-m", GITOPS_COMMIT_MESSAGE],
        repo,
        env=_identity_env(repo),
    )
    return (commit.stdout.strip() or None) if commit.returncode == 0 else None


def ensure_gitops_files(repo_path: str | Path, base: str, *, image: str, host: str) -> CiInstall:
    """Commit the missing GitOps files to ``origin/<base>`` (idempotent).

    :param image: ``ghcr.io/<owner>/<repo>`` (see :func:`image_repo`).
    :param host: The production preview host (see :func:`main_host`).
    :returns: ``installed`` / ``present`` / ``skipped`` / ``failed``, as for CI.
    """
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return CiInstall("skipped", f"{repo} is not a git checkout")
    if _git(["remote", "get-url", "origin"], repo).returncode:
        return CiInstall("skipped", "the repository has no origin remote")
    wanted = render_repo_files(image, host)
    last = ""
    for _attempt in range(2):
        listed = _git(
            ["ls-remote", "--exit-code", "--heads", "origin", base], repo, timeout=_PUSH_TIMEOUT_S
        )
        if listed.returncode == 2:
            return CiInstall("skipped", f"origin has no {base} branch yet")
        fetched = _git(["fetch", "--quiet", "origin", base], repo, timeout=_PUSH_TIMEOUT_S)
        if fetched.returncode:
            return CiInstall("failed", f"git fetch origin {base}: {_out(fetched)}")
        head = _git(["rev-parse", "--verify", "--quiet", f"origin/{base}^{{commit}}"], repo)
        parent = head.stdout.strip()
        if head.returncode or not parent:
            return CiInstall("skipped", f"origin has no {base} branch yet")
        missing = {
            path: text
            for path, text in wanted.items()
            if _git(["cat-file", "-e", f"{parent}:{path}"], repo).returncode
        }
        if not missing:
            return CiInstall("present", f"origin/{base} already has the gitops files")
        commit = _commit_with_files(repo, parent, missing)
        if commit is None:
            return CiInstall("failed", "could not build the gitops commit")
        pushed = _git(
            ["push", "--quiet", "origin", f"{commit}:refs/heads/{base}"],
            repo,
            timeout=_PUSH_TIMEOUT_S,
        )
        if pushed.returncode == 0:
            _git(["fetch", "--quiet", "origin", base], repo, timeout=_PUSH_TIMEOUT_S)
            _fast_forward_local_base(repo, base)
            _logger.info("shipcrew: installed gitops files on %s (%s)", base, commit)
            return CiInstall("installed", f"pushed {GITOPS_COMMIT_MESSAGE} to {base}", commit)
        last = _out(pushed)
    return CiInstall("failed", f"git push origin {base}: {last}")


def ensure_for_mission(
    repo_url: str | None, repo_path: str | Path, base: str, env: Mapping[str, str] | None = None
) -> CiInstall | None:
    """At the first task start (next to the CI install), when the target is ``argocd``.

    Installing early gives every agent PR its preview from the start. ``None``
    when the target is not ``argocd``; ``skipped`` without a GitHub repo or
    ``SHIPCREW_BASE_DOMAIN``. The ``argocd`` target's deploy installs them too.
    """
    env = os.environ if env is None else env
    if env.get("SHIPCREW_DEPLOY_TARGET", "").strip().lower() != "argocd":
        return None
    slug = github_slug(repo_url)
    domain = env.get("SHIPCREW_BASE_DOMAIN", "").strip().strip(".")
    if slug is None or not domain:
        return CiInstall("skipped", "no GitHub repo or no SHIPCREW_BASE_DOMAIN")
    return ensure_gitops_files(
        repo_path,
        base,
        image=image_repo(*slug),
        host=main_host(app_name(repo_url, repo_path), domain),
    )
