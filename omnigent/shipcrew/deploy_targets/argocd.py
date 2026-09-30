"""The ``argocd`` deploy target: a VPS (k3s + ArgoCD) keeps the previews in sync.

``SHIPCREW_DEPLOY_TARGET=argocd``. Nothing is built or pushed by the server:

- the app repo carries ``deploy/k8s`` and ``.github/workflows/gitops.yml``
  (:mod:`omnigent.shipcrew.gitops_install`); GitHub Actions pushes
  ``ghcr.io/<owner>/<repo>:<sha>`` and moves ``gitops/main``;
- per mission this target applies two ArgoCD objects (:func:`manifests`):
  an ``Application`` ``<app>`` for ``gitops/main`` (automated sync, prune,
  selfHeal) served at ``https://<app>.<base domain>``, and an
  ``ApplicationSet`` ``<app>-pr`` whose Pull Request generator gives every
  open PR ``https://pr-<number>.<app>.<base domain>`` from ``deploy/k8s/base``
  at the PR head, with the PR's image;
- :meth:`ArgoCDTarget.deploy` then waits (bounded) until the Application is
  Synced + Healthy on the image of the current ``origin/<base>`` SHA.

Cluster-side config lives in the VPS installer (``deploy/vps/install.sh``):
ArgoCD core, the GitHub token secret (``gh auth token``) used by the PR
generator and for private repos, GHCR pull credentials, Traefik, cert-manager
and the ``letsencrypt`` ClusterIssuer. ``SHIPCREW_BASE_DOMAIN`` is
``<vps-ip>.sslip.io`` there.

``kubectl`` is resolved like every tool (``SHIPCREW_KUBECTL``, tools.json,
PATH), so tests point it at a fake. Blocking: async callers use a thread.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnigent.shipcrew import tools

# INTEGRATION: import these from .base once deploy_targets/base.py lands.
from omnigent.shipcrew.deploy_targets._base_stub import DeployContext, DeployError, DeployResult
from omnigent.shipcrew.gitops_install import (
    BASE_PATH,
    DEPLOY_BRANCH,
    MAIN_OVERLAY,
    app_name,
    ensure_gitops_files,
    github_slug,
    image_repo,
    main_host,
)
from omnigent.shipcrew.store import Mission

_logger = logging.getLogger(__name__)

APP_LABEL = "shipcrew.app"
MISSION_LABEL = "shipcrew.mission"
FINALIZER = "resources-finalizer.argocd.argoproj.io"
_CRDS = ("applications.argoproj.io", "applicationsets.argoproj.io")
_KUBECTL_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ArgoConfig:
    """Everything cluster-specific, from the environment (the installer writes it).

    :param base_domain: ``SHIPCREW_BASE_DOMAIN``, e.g. ``203.0.113.7.sslip.io``.
    :param namespace: Where ArgoCD runs (``SHIPCREW_ARGOCD_NAMESPACE``).
    :param github_secret: Secret (key ``token``) the PR generator reads.
    :param scheme: ``https`` (cert-manager) or ``http`` (local proof).
    :param pr_previews: Whether to apply the PR ApplicationSet.
    :param wait_s: How long :meth:`ArgoCDTarget.deploy` waits for Synced + Healthy.
    """

    base_domain: str = ""
    namespace: str = "argocd"
    github_secret: str = "shipcrew-github"
    scheme: str = "https"
    pr_previews: bool = True
    wait_s: float = 900.0
    poll_s: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ArgoConfig:
        env = os.environ if env is None else env
        return cls(
            base_domain=env.get("SHIPCREW_BASE_DOMAIN", "").strip().strip("."),
            namespace=env.get("SHIPCREW_ARGOCD_NAMESPACE", "argocd").strip() or "argocd",
            github_secret=env.get("SHIPCREW_ARGOCD_GITHUB_SECRET", "shipcrew-github").strip(),
            scheme="http" if env.get("SHIPCREW_ARGOCD_TLS", "1").strip() == "0" else "https",
            pr_previews=env.get("SHIPCREW_ARGOCD_PR_PREVIEWS", "1").strip() != "0",
            wait_s=float(env.get("SHIPCREW_ARGOCD_WAIT_S", "900")),
        )


def pr_host(app: str, base_domain: str) -> str:
    """The host template of a PR preview (``{{.number}}`` filled in by ArgoCD)."""
    return f"pr-{{{{.number}}}}.{app}.{base_domain}"


def _host_patch(host: str) -> str:
    return json.dumps(
        [
            {"op": "replace", "path": "/spec/rules/0/host", "value": host},
            {"op": "replace", "path": "/spec/tls/0/hosts/0", "value": host},
        ]
    )


def _sync_policy(app: str) -> dict[str, Any]:
    return {
        "automated": {"prune": True, "selfHeal": True},
        "syncOptions": ["CreateNamespace=true"],
        # Labels the namespaces ArgoCD creates, so teardown finds them.
        "managedNamespaceMetadata": {"labels": {APP_LABEL: app}},
        "retry": {"limit": 5, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"}},
    }


def manifests(
    *,
    app: str,
    repo_url: str,
    owner: str,
    repo: str,
    config: ArgoConfig,
    mission_id: str = "",
    revision: str = DEPLOY_BRANCH,
) -> list[dict[str, Any]]:
    """The ArgoCD ``Application`` (main) and ``ApplicationSet`` (PR previews)."""
    labels = {APP_LABEL: app, **({MISSION_LABEL: mission_id[:63]} if mission_id else {})}
    application: dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": app,
            "namespace": config.namespace,
            "labels": labels,
            "finalizers": [FINALIZER],
        },
        "spec": {
            "project": "default",
            "source": {"repoURL": repo_url, "targetRevision": revision, "path": MAIN_OVERLAY},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": app},
            "syncPolicy": _sync_policy(app),
        },
    }
    out = [application]
    if not config.pr_previews:
        return out
    pr_name = f"{app}-pr-{{{{.number}}}}"
    out.append(
        {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "ApplicationSet",
            "metadata": {"name": f"{app}-pr", "namespace": config.namespace, "labels": labels},
            "spec": {
                "goTemplate": True,
                "goTemplateOptions": ["missingkey=error"],
                "generators": [
                    {
                        "pullRequest": {
                            "github": {
                                "owner": owner,
                                "repo": repo,
                                "tokenRef": {"secretName": config.github_secret, "key": "token"},
                            },
                            "requeueAfterSeconds": 120,
                        }
                    }
                ],
                "template": {
                    "metadata": {"name": pr_name, "labels": labels, "finalizers": [FINALIZER]},
                    "spec": {
                        "project": "default",
                        "source": {
                            "repoURL": repo_url,
                            "targetRevision": "{{.head_sha}}",
                            "path": BASE_PATH,
                            "kustomize": {
                                "images": [f"app={image_repo(owner, repo)}:{{{{.head_sha}}}}"],
                                "patches": [
                                    {
                                        "target": {"kind": "Ingress", "name": "app"},
                                        "patch": _host_patch(pr_host(app, config.base_domain)),
                                    }
                                ],
                            },
                        },
                        "destination": {
                            "server": "https://kubernetes.default.svc",
                            "namespace": pr_name,
                        },
                        "syncPolicy": _sync_policy(app),
                    },
                },
            },
        }
    )
    return out


def _kubectl_tool() -> tools.Tool:
    return next(t for t in tools.registry() if t.key == "KUBECTL")


class ArgoCDTarget:
    """``DeployTarget`` for a k3s + ArgoCD VPS (see the module docstring)."""

    name = "argocd"

    def __init__(
        self,
        config: ArgoConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or ArgoConfig.from_env()
        self._sleep = sleep
        self._clock = clock

    # ── kubectl ─────────────────────────────────────────────────

    def _kubectl(
        self, args: Sequence[str], *, stdin: str | None = None, timeout: float = _KUBECTL_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        path = tools.resolve(_kubectl_tool()) or "kubectl"
        try:
            return subprocess.run(
                [path, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=tools.session_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess([path, *args], 1, "", f"kubectl: {exc}")

    @staticmethod
    def _err(r: subprocess.CompletedProcess[str]) -> str:
        return (r.stderr or r.stdout).strip()[-400:]

    # ── DeployTarget ────────────────────────────────────────────

    def preflight(self) -> str | None:
        """Why this machine cannot deploy through ArgoCD, or ``None``."""
        if tools.resolve(_kubectl_tool()) is None:
            return "kubectl not found: run deploy/vps/install.sh (or set SHIPCREW_KUBECTL)"
        if not self.config.base_domain:
            return (
                "SHIPCREW_BASE_DOMAIN is not set (e.g. <vps-ip>.sslip.io, "
                "see docs/shipcrew/VPS.md)"
            )
        crds = self._kubectl(["get", "crd", *_CRDS, "-o", "name"])
        if crds.returncode:
            return f"ArgoCD is not installed or the cluster is unreachable: {self._err(crds)}"
        ns = self.config.namespace
        ctrl = self._kubectl(
            ["-n", ns, "get", "statefulset", "argocd-application-controller", "-o", "name"]
        )
        if ctrl.returncode:
            return f"the ArgoCD application controller is missing in {ns}: {self._err(ctrl)}"
        if self.config.pr_previews:
            secret = self._kubectl(["-n", ns, "get", "secret", self.config.github_secret])
            if secret.returncode:
                return (
                    f"secret {ns}/{self.config.github_secret} (GitHub token for PR previews) "
                    "is missing: re-run deploy/vps/install.sh after `gh auth login`, "
                    "or SHIPCREW_ARGOCD_PR_PREVIEWS=0"
                )
        return None

    def _names(self, mission: Mission) -> tuple[str, str, str]:
        slug = github_slug(mission.repo_url)
        if slug is None:
            raise DeployError(
                f"the argocd target needs a GitHub repo (GHCR image, PR previews): "
                f"{mission.repo_url or mission.repo_path}"
            )
        return app_name(mission.repo_url, mission.repo_path), *slug

    def url(self, mission: Mission) -> str:
        app = app_name(mission.repo_url, mission.repo_path)
        return f"{self.config.scheme}://{main_host(app, self.config.base_domain)}"

    def apply(self, objects: Sequence[dict[str, Any]]) -> None:
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)})
        r = self._kubectl(["apply", "-f", "-"], stdin=body)
        if r.returncode:
            raise DeployError(f"kubectl apply failed: {self._err(r)}")

    def deploy(self, ctx: DeployContext) -> DeployResult:
        """Ensure the repo files and ArgoCD objects exist, wait for Synced + Healthy."""
        mission = ctx.mission
        app, owner, repo = self._names(mission)
        image = image_repo(owner, repo)
        host = main_host(app, self.config.base_domain)
        note: str | None = None
        files = ensure_gitops_files(mission.repo_path, ctx.base, image=image, host=host)
        if files.status == "failed":
            raise DeployError(f"could not install the gitops files: {files.detail}")
        if files.status == "installed":
            note = "gitops files were installed at ship time: the first image build ran now"
        repo_url = f"https://github.com/{owner}/{repo}.git"
        self.apply(
            manifests(
                app=app,
                repo_url=repo_url,
                owner=owner,
                repo=repo,
                config=self.config,
                mission_id=mission.id,
            )
        )
        sha = self._remote_sha(mission.repo_path, ctx.base)
        self.wait_healthy(app, expected_image=f"{image}:{sha}" if sha else None)
        return DeployResult(url=self.url(mission), note=note)

    def _remote_sha(self, repo_path: str, base: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "ls-remote", "origin", f"refs/heads/{base}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=_KUBECTL_TIMEOUT_S,
                env={**tools.session_env(), "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        first = r.stdout.split()
        return first[0] if r.returncode == 0 and first else None

    def status(self, app: str) -> dict[str, Any] | None:
        r = self._kubectl(["-n", self.config.namespace, "get", "application", app, "-o", "json"])
        if r.returncode:
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return data.get("status") or {}

    def _refresh(self, app: str) -> None:
        self._kubectl(
            [
                "-n",
                self.config.namespace,
                "annotate",
                "application",
                app,
                "argocd.argoproj.io/refresh=normal",
                "--overwrite",
            ]
        )

    def wait_healthy(self, app: str, *, expected_image: str | None = None) -> dict[str, Any]:
        """Block until ``app`` is Synced + Healthy (on ``expected_image`` when given).

        :raises DeployError: After ``config.wait_s``, with the last state seen.
        """
        deadline = self._clock() + self.config.wait_s
        last_refresh = -1e18
        seen = "no status yet"
        while True:
            now = self._clock()
            if now - last_refresh >= 60:
                self._refresh(app)
                last_refresh = now
            st = self.status(app)
            if st is not None:
                sync = (st.get("sync") or {}).get("status", "Unknown")
                health = (st.get("health") or {}).get("status", "Unknown")
                images = (st.get("summary") or {}).get("images") or []
                on_image = expected_image is None or expected_image in images
                if sync == "Synced" and health == "Healthy" and on_image:
                    return st
                conditions = "; ".join(
                    str(c.get("message", ""))[:200] for c in st.get("conditions") or []
                )
                seen = f"sync={sync} health={health} images={images}" + (
                    f" conditions: {conditions}" if conditions else ""
                )
                if expected_image and not on_image:
                    seen += f" (waiting for {expected_image})"
            if self._clock() >= deadline:
                raise DeployError(
                    f"ArgoCD app {app} not Synced+Healthy after {self.config.wait_s:.0f}s: {seen}"
                )
            self._sleep(self.config.poll_s)

    def teardown(self, mission: Mission) -> None:
        """Delete the Application, the ApplicationSet (and so every preview) and namespaces."""
        app = app_name(mission.repo_url, mission.repo_path)
        ns = self.config.namespace
        selector = f"{APP_LABEL}={app}"
        # The finalizer makes ArgoCD delete each app's resources before the app goes.
        wait = ["--wait=true", "--timeout=180s"]
        steps = [
            ["-n", ns, "delete", "applicationset", "-l", selector, *wait],
            ["-n", ns, "delete", "application", "-l", selector, *wait],
            ["delete", "namespace", "-l", selector, "--wait=false"],
        ]
        for step in steps:
            r = self._kubectl(step, timeout=240)
            if r.returncode:
                _logger.warning("shipcrew: argocd teardown %s: %s", step[2:4], self._err(r))


def create() -> ArgoCDTarget:
    """Registry entry for ``SHIPCREW_DEPLOY_TARGET=argocd``."""
    return ArgoCDTarget()
