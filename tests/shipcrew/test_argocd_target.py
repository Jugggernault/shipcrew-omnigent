"""The argocd deploy target: repo templates, ArgoCD manifests, a fake kubectl, the installer.

Real git with a local bare ``origin``; ``kubectl`` is a fake script
(``SHIPCREW_KUBECTL``) that logs its calls and answers from a JSON state file.
``kubectl kustomize`` and ``shellcheck`` are used when installed, else skipped.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnigent.shipcrew import tools
from omnigent.shipcrew.deploy_targets._base_stub import DeployContext, DeployError
from omnigent.shipcrew.deploy_targets.argocd import ArgoCDTarget, ArgoConfig, manifests
from omnigent.shipcrew.gitops_install import (
    GITOPS_COMMIT_MESSAGE,
    app_name,
    ensure_for_mission,
    ensure_gitops_files,
    github_slug,
    image_repo,
    render_repo_files,
)
from omnigent.shipcrew.store import Mission

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "deploy" / "vps" / "install.sh"
PROOF = ROOT / "deploy" / "vps" / "k3d-proof.sh"
DOMAIN = "203.0.113.7.sslip.io"
REPO_URL = "https://github.com/Acme/My_Shop.git"
IMAGE = "ghcr.io/acme/my_shop"
EXPECTED_FILES = {
    ".github/workflows/gitops.yml",
    "deploy/k8s/base/kustomization.yaml",
    "deploy/k8s/base/deployment.yaml",
    "deploy/k8s/base/service.yaml",
    "deploy/k8s/base/ingress.yaml",
    "deploy/k8s/overlays/main/kustomization.yaml",
}


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout of main with a local bare origin."""
    origin = tmp_path / "origin.git"
    run(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "my_shop"
    run(tmp_path, "git", "clone", "-q", str(origin), str(work))
    run(work, "git", "config", "user.name", "t")
    run(work, "git", "config", "user.email", "t@example.com")
    run(work, "git", "checkout", "-q", "-b", "main")
    (work / "package.json").write_text("{}\n")
    run(work, "git", "add", "-A")
    run(work, "git", "commit", "-qm", "init")
    run(work, "git", "push", "-q", "origin", "main")
    return work


def _mission(repo: Path, repo_url: str | None = REPO_URL) -> Mission:
    return Mission(
        id="m-1234",
        title="shop",
        repo_path=str(repo),
        repo_url=repo_url,
        status="active",
        created_at=0,
    )


# ── names ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/Acme/My_Shop.git", ("Acme", "My_Shop")),
        ("git@github.com:acme/shop.git", ("acme", "shop")),
        ("https://github.com/acme/shop/", ("acme", "shop")),
        ("https://gitlab.com/acme/shop", None),
        (None, None),
    ],
)
def test_github_slug(url: str | None, slug: tuple[str, str] | None) -> None:
    assert github_slug(url) == slug


def test_app_name_is_a_short_dns_label() -> None:
    assert app_name(REPO_URL, "/x") == "my-shop"
    assert app_name(None, "/tmp/Some Repo__") == "some-repo"
    long = app_name("https://github.com/a/" + "x" * 90, "/x")
    assert len(long) <= 40 and long.isalnum()
    assert image_repo("Acme", "My_Shop") == IMAGE


# ── repo templates ──────────────────────────────────────────────


def test_repo_files_are_minimal_and_filled() -> None:
    files = render_repo_files(IMAGE, f"my-shop.{DOMAIN}")
    assert set(files) == EXPECTED_FILES
    assert not [p for p, text in files.items() if "__IMAGE__" in text or "__HOST__" in text]
    overlay = yaml.safe_load(files["deploy/k8s/overlays/main/kustomization.yaml"])
    assert overlay["images"] == [{"name": "app", "newName": IMAGE, "newTag": "latest"}]
    assert f"my-shop.{DOMAIN}" in overlay["patches"][0]["patch"]
    deploy = yaml.safe_load(files["deploy/k8s/base/deployment.yaml"])
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    }
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/", "port": "http"}


def test_workflow_uses_only_the_builtin_token() -> None:
    text = render_repo_files(IMAGE, "h")[".github/workflows/gitops.yml"]
    wf = yaml.safe_load(text)
    assert wf["permissions"] == {"contents": "write", "packages": "write"}
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}
    steps = wf["jobs"]["image"]["steps"]
    push = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action"))
    assert push["with"]["tags"] == "${{ steps.img.outputs.name }}:${{ env.SHA }}"
    bump = steps[-1]["run"]
    assert "git push -qf origin gitops/main" in bump
    assert "origin main" not in bump  # never commits to the protected main


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)


def _kustomize(path: Path) -> list[dict[str, Any]]:
    kubectl = tools.resolve(next(t for t in tools.registry() if t.key == "KUBECTL"))
    if kubectl is None:
        pytest.skip("kubectl not installed")
    out = subprocess.run(
        [kubectl, "kustomize", str(path)], check=True, capture_output=True, text=True
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def test_kustomize_main_overlay_renders(tmp_path: Path) -> None:
    _write_tree(tmp_path, render_repo_files(IMAGE, f"my-shop.{DOMAIN}"))
    docs = {d["kind"]: d for d in _kustomize(tmp_path / "deploy/k8s/overlays/main")}
    assert set(docs) == {"Deployment", "Service", "Ingress"}
    image = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"{IMAGE}:latest"
    ingress = docs["Ingress"]["spec"]
    assert ingress["rules"][0]["host"] == ingress["tls"][0]["hosts"][0] == f"my-shop.{DOMAIN}"


def test_kustomize_pr_preview_matches_the_applicationset(tmp_path: Path) -> None:
    """What ArgoCD renders for PR 7: the base + the ApplicationSet's kustomize images/patch."""
    _write_tree(tmp_path, render_repo_files(IMAGE, "unused"))
    appset = manifests(
        app="my-shop", repo_url=REPO_URL, owner="Acme", repo="My_Shop", config=_cfg()
    )[1]
    kz = appset["spec"]["template"]["spec"]["source"]["kustomize"]
    fill = {"{{.number}}": "7", "{{.head_sha}}": "abc123"}

    def sub(s: str) -> str:
        for k, v in fill.items():
            s = s.replace(k, v)
        return s

    name, _, tagged = sub(kz["images"][0]).partition("=")
    new_name, _, tag = tagged.rpartition(":")
    pr = tmp_path / "pr"
    pr.mkdir()
    (pr / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "resources": ["../deploy/k8s/base"],
                "images": [{"name": name, "newName": new_name, "newTag": tag}],
                "patches": [
                    {"target": kz["patches"][0]["target"], "patch": sub(kz["patches"][0]["patch"])}
                ],
            }
        )
    )
    docs = {d["kind"]: d for d in _kustomize(pr)}
    image = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"{IMAGE}:abc123"
    assert docs["Ingress"]["spec"]["rules"][0]["host"] == f"pr-7.my-shop.{DOMAIN}"


# ── ArgoCD manifests ────────────────────────────────────────────


def _cfg(**kw: Any) -> ArgoConfig:
    return ArgoConfig(**{"base_domain": DOMAIN, "wait_s": 30, "poll_s": 1, **kw})


def test_manifests_shape() -> None:
    app, appset = manifests(
        app="my-shop", repo_url=REPO_URL, owner="Acme", repo="My_Shop", config=_cfg(),
        mission_id="m-1",
    )  # fmt: skip
    assert app["kind"] == "Application" and app["metadata"]["name"] == "my-shop"
    assert app["metadata"]["labels"] == {"shipcrew.app": "my-shop", "shipcrew.mission": "m-1"}
    spec = app["spec"]
    assert spec["source"] == {
        "repoURL": REPO_URL,
        "targetRevision": "gitops/main",
        "path": "deploy/k8s/overlays/main",
    }
    assert spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}
    assert "CreateNamespace=true" in spec["syncPolicy"]["syncOptions"]
    assert spec["destination"]["namespace"] == "my-shop"

    assert appset["kind"] == "ApplicationSet" and appset["spec"]["goTemplate"] is True
    gen = appset["spec"]["generators"][0]["pullRequest"]["github"]
    assert gen == {
        "owner": "Acme",
        "repo": "My_Shop",
        "tokenRef": {"secretName": "shipcrew-github", "key": "token"},
    }
    tpl = appset["spec"]["template"]
    assert tpl["metadata"]["name"] == "my-shop-pr-{{.number}}"
    assert tpl["spec"]["source"]["targetRevision"] == "{{.head_sha}}"
    assert tpl["spec"]["source"]["path"] == "deploy/k8s/base"
    assert tpl["spec"]["destination"]["namespace"] == "my-shop-pr-{{.number}}"
    assert tpl["spec"]["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}


def test_pr_previews_can_be_turned_off() -> None:
    objs = manifests(
        app="a", repo_url=REPO_URL, owner="o", repo="r", config=_cfg(pr_previews=False)
    )
    assert [o["kind"] for o in objs] == ["Application"]


def test_config_from_env() -> None:
    cfg = ArgoConfig.from_env(
        {"SHIPCREW_BASE_DOMAIN": "1.2.3.4.sslip.io.", "SHIPCREW_ARGOCD_TLS": "0"}
    )
    assert cfg.base_domain == "1.2.3.4.sslip.io" and cfg.scheme == "http"
    assert ArgoConfig.from_env({}).scheme == "https"


# ── gitops install (real git) ───────────────────────────────────


def test_ensure_gitops_files_installs_once(repo: Path) -> None:
    first = ensure_gitops_files(repo, "main", image=IMAGE, host=f"my-shop.{DOMAIN}")
    assert first.status == "installed", first.detail
    origin = repo.parent / "origin.git"
    assert run(origin, "git", "log", "-1", "--format=%s", "main") == GITOPS_COMMIT_MESSAGE
    listed = set(run(origin, "git", "ls-tree", "-r", "--name-only", "main").splitlines())
    assert listed >= EXPECTED_FILES
    assert ensure_gitops_files(repo, "main", image=IMAGE, host="x").status == "present"
    assert run(repo, "git", "status", "--porcelain") == ""


def test_ensure_gitops_files_keeps_project_owned_files(repo: Path) -> None:
    (repo / "deploy/k8s/base").mkdir(parents=True)
    (repo / "deploy/k8s/base/deployment.yaml").write_text("# mine\n")
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-qm", "own manifests")
    run(repo, "git", "push", "-q", "origin", "main")
    assert ensure_gitops_files(repo, "main", image=IMAGE, host="h").status == "installed"
    origin = repo.parent / "origin.git"
    assert run(origin, "git", "show", "main:deploy/k8s/base/deployment.yaml") == "# mine"


def test_ensure_for_mission_only_for_argocd(repo: Path) -> None:
    assert ensure_for_mission(REPO_URL, repo, "main", env={}) is None
    env = {"SHIPCREW_DEPLOY_TARGET": "argocd"}
    assert ensure_for_mission(REPO_URL, repo, "main", env=env).status == "skipped"  # type: ignore[union-attr]
    env["SHIPCREW_BASE_DOMAIN"] = DOMAIN
    assert ensure_for_mission(REPO_URL, repo, "main", env=env).status == "installed"  # type: ignore[union-attr]


def test_kubectl_is_required_only_for_the_argocd_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def kubectl() -> tools.Tool:
        return next(t for t in tools.registry() if t.key == "KUBECTL")

    monkeypatch.delenv("SHIPCREW_DEPLOY_TARGET", raising=False)
    assert not kubectl().required
    monkeypatch.setenv("SHIPCREW_DEPLOY_TARGET", "argocd")
    assert kubectl().required
    assert {"ARGOCD", "K3D"} <= {t.key for t in tools.registry()}


# ── fake kubectl ────────────────────────────────────────────────

FAKE_KUBECTL = r"""#!PYTHON
import json, sys
from pathlib import Path
d = Path(__file__).parent
state = json.loads((d / "state.json").read_text())
args = sys.argv[1:]
stdin = sys.stdin.read() if "-f" in args and "-" in args else ""
with (d / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
def has(*words):
    return all(w in args for w in words)
if has("get", "crd"):
    sys.exit(0 if state.get("crds", True) else (print("NotFound", file=sys.stderr) or 1))
if has("get", "statefulset"):
    sys.exit(0 if state.get("controller", True) else 1)
if has("get", "secret"):
    sys.exit(0 if state.get("secret", True) else (print("secret not found", file=sys.stderr) or 1))
if has("get", "application"):
    seq = state.get("statuses", [])
    n = state.get("polled", 0)
    state["polled"] = n + 1
    (d / "state.json").write_text(json.dumps(state))
    st = seq[min(n, len(seq) - 1)] if seq else {}
    print(json.dumps({"status": st}))
    sys.exit(0)
sys.exit(0)
"""


class Kube:
    def __init__(self, root: Path) -> None:
        self.root = root

    def state(self, **kw: Any) -> None:
        (self.root / "state.json").write_text(json.dumps(kw))

    def calls(self) -> list[dict[str, Any]]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def kube(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Kube:
    root = tmp_path / "fakekube"
    root.mkdir()
    exe = root / "kubectl"
    exe.write_text(FAKE_KUBECTL.replace("#!PYTHON", f"#!{sys.executable}"))
    exe.chmod(0o755)
    monkeypatch.setenv("SHIPCREW_KUBECTL", str(exe))
    k = Kube(root)
    k.state()
    return k


def _target(**kw: Any) -> ArgoCDTarget:
    clock = [0.0]

    def sleep(s: float) -> None:
        clock[0] += s

    return ArgoCDTarget(_cfg(**kw), sleep=sleep, clock=lambda: clock[0])


def test_preflight_ok(kube: Kube) -> None:
    assert _target().preflight() is None
    assert any(c["args"][:2] == ["get", "crd"] for c in kube.calls())


@pytest.mark.parametrize(
    ("state", "cfg", "needle"),
    [
        ({"crds": False}, {}, "ArgoCD is not installed"),
        ({"controller": False}, {}, "application controller"),
        ({"secret": False}, {}, "shipcrew-github"),
        ({}, {"base_domain": ""}, "SHIPCREW_BASE_DOMAIN"),
    ],
)
def test_preflight_failures(
    kube: Kube, state: dict[str, Any], cfg: dict[str, Any], needle: str
) -> None:
    kube.state(**state)
    reason = _target(**cfg).preflight()
    assert reason is not None and needle in reason


def test_preflight_without_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "resolve", lambda _t: None)
    assert "kubectl not found" in (_target().preflight() or "")


def test_deploy_applies_and_waits_for_the_main_image(kube: Kube, repo: Path) -> None:
    target = _target()
    mission = _mission(repo)
    # The gitops commit is pushed by deploy(), so the SHA is only known after it.
    kube.state(statuses=[{"sync": {"status": "OutOfSync"}, "health": {"status": "Missing"}}])

    def healthy_once_installed() -> None:
        sha = run(repo.parent / "origin.git", "git", "rev-parse", "main")
        kube.state(
            statuses=[
                {"sync": {"status": "Synced"}, "health": {"status": "Progressing"}},
                {
                    "sync": {"status": "Synced"},
                    "health": {"status": "Healthy"},
                    "summary": {"images": [f"{IMAGE}:{sha}"]},
                },
            ]
        )

    original = target.apply

    def apply(objs: Any) -> None:
        original(objs)
        healthy_once_installed()

    target.apply = apply  # type: ignore[method-assign]
    result = target.deploy(DeployContext(mission=mission))
    assert result.url == f"https://my-shop.{DOMAIN}"
    assert result.note and "installed at ship time" in result.note
    applied = next(c for c in kube.calls() if c["args"][0] == "apply")
    body = json.loads(applied["stdin"])
    assert [i["kind"] for i in body["items"]] == ["Application", "ApplicationSet"]
    assert body["items"][0]["spec"]["source"]["repoURL"] == "https://github.com/Acme/My_Shop.git"
    polls = [c for c in kube.calls() if c["args"][2:4] == ["get", "application"]]
    assert len(polls) == 2
    assert any("argocd.argoproj.io/refresh=normal" in c["args"] for c in kube.calls())


def test_deploy_times_out_with_the_last_state(kube: Kube, repo: Path) -> None:
    kube.state(
        statuses=[
            {
                "sync": {"status": "Unknown"},
                "health": {"status": "Healthy"},
                "conditions": [{"type": "ComparisonError", "message": "gitops/main not found"}],
            }
        ]
    )
    with pytest.raises(DeployError, match="gitops/main not found"):
        _target(wait_s=10).deploy(DeployContext(mission=_mission(repo)))


def test_deploy_needs_a_github_repo(kube: Kube, repo: Path) -> None:
    with pytest.raises(DeployError, match="GitHub repo"):
        _target().deploy(DeployContext(mission=_mission(repo, repo_url=None)))
    assert not kube.calls()


def test_teardown_deletes_by_label(kube: Kube, repo: Path) -> None:
    _target().teardown(_mission(repo))
    deletes = [c["args"] for c in kube.calls() if "delete" in c["args"]]
    kinds = [a[a.index("delete") + 1] for a in deletes]
    assert kinds == ["applicationset", "application", "namespace"]
    assert all("shipcrew.app=my-shop" in a for a in deletes)


# ── scripts ─────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [INSTALLER, PROOF], ids=["install", "k3d-proof"])
def test_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    subprocess.run([shellcheck, "-S", "warning", str(script)], check=True)


def test_installer_is_idempotent_by_construction() -> None:
    """Every install is guarded, units and secrets are declarative (apply / overwrite)."""
    text = INSTALLER.read_text()
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "kubectl apply" in text and "kubectl create" not in text.replace(
        "--dry-run=client -o yaml | kubectl apply", ""
    )
    for guard in ("have docker", "have k3s", "have gh", "user_have uv", "have caddy"):
        assert guard in text, guard
