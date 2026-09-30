#!/usr/bin/env bash
# Local proof of the argocd deploy target on a throwaway k3d cluster.
# No sudo, no cloud, no GitHub: kubectl/k3d come from ~/.local/bin (tools registry),
# the "GitHub repo" is a bare repo served by git-daemon inside the cluster, and a
# fake GitHub API answers the ApplicationSet Pull Request generator.
#
#   deploy/vps/k3d-proof.sh            # KEEP=1 keeps the cluster for poking around
#
# Proves: ArgoCD core installs; the shipcrew Application (from manifests())
# syncs the repo's deploy/k8s/overlays/main; the app answers through Traefik at
# demo.127.0.0.1.sslip.io; a git push (image bump like gitops.yml, replica count)
# is applied automatically; selfHeal reverts a manual change; the PR generator
# gives pr-7.demo.127.0.0.1.sslip.io; teardown removes everything.
# Not covered here: GHCR, GitHub Actions, cert-manager/Let's Encrypt (see VPS.md).
set -euo pipefail

CLUSTER="${CLUSTER:-sc-gitops-proof}"
PORT="${PORT:-18080}"
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.3}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
BIN="$HOME/.local/bin"
export PATH="$BIN:$PATH"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sc-proof.XXXXXX")"
export KUBECONFIG="$WORK/kubeconfig"
export SHIPCREW_KUBECTL="$BIN/kubectl"
DOMAIN="127.0.0.1.sslip.io"
IMAGE="ghcr.io/acme/demo"
APP=demo

step() { printf '\n--- %s\n' "$*"; }
cleanup() {
  local rc=$?
  if [[ ${KEEP:-0} != 1 ]]; then
    k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
  [[ $rc == 0 ]] && echo "PROOF OK" || echo "PROOF FAILED (rc=$rc)"
}
trap cleanup EXIT
# until <seconds> <cmd...>: retry every 3 s, bounded.
until_ok() {
  local end=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < end)) || { echo "timeout: $*"; return 1; }
    sleep 3
  done
}
page() { curl -fsS --max-time 5 --resolve "$1:$PORT:127.0.0.1" "http://$1:$PORT/"; }
page_has() { page "$1" | grep -q "$2"; }

step "tools (installed via omnigent.shipcrew.tools when missing)"
"$PY" - <<'EOF'
from omnigent.shipcrew import tools
for t in tools.registry():
    if t.key in ("KUBECTL", "K3D"):
        if not tools.resolve(t):
            tools.install(t)
        ok, why = tools.check(t)
        assert ok, (t.key, why)
        print(t.key, tools.resolve(t))
EOF

step "images: one tiny image = demo app, git-daemon, fake GitHub API"
cat >"$WORK/server.py" <<'EOF'
import json, os, socket
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if os.environ.get("MODE") == "github":
            body = os.environ["PULLS"].encode(); ctype = "application/json"
        else:
            body = f"demo {os.environ.get('MESSAGE', '?')} on {socket.gethostname()}\n".encode()
            ctype = "text/plain"
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "3000"))), H).serve_forever()
EOF
cat >"$WORK/Dockerfile" <<'EOF'
FROM python:3.12-alpine
RUN apk add --no-cache git git-daemon
ARG MESSAGE=v1
ENV MESSAGE=$MESSAGE
COPY server.py /server.py
CMD ["python", "/server.py"]
EOF
docker build -q -t scproof:v1 --build-arg MESSAGE=v1 "$WORK" >/dev/null
docker build -q -t scproof:v2 --build-arg MESSAGE=v2 "$WORK" >/dev/null

step "git repo: the real shipcrew templates, main + gitops/main + a PR branch"
mkdir -p "$WORK/git"
git init -q --bare -b main "$WORK/git/demo.git"
git -C "$WORK/git/demo.git" config uploadpack.allowReachableSHA1InWant true
git clone -q "$WORK/git/demo.git" "$WORK/repo" 2>/dev/null
g() { git -C "$WORK/repo" -c user.name=proof -c user.email=proof@example.com "$@"; }
g checkout -q -b main
"$PY" - "$WORK/repo" "$IMAGE" "$APP.$DOMAIN" <<'EOF'
import sys
from pathlib import Path
from omnigent.shipcrew.gitops_install import render_repo_files
for path, text in render_repo_files(sys.argv[2], sys.argv[3]).items():
    dest = Path(sys.argv[1]) / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
EOF
g add -A && g commit -qm "chore: shipcrew gitops"
g push -q origin main
MAIN_SHA="$(g rev-parse HEAD)"
# Exactly what .github/workflows/gitops.yml does after pushing the image.
bump() {
  g checkout -q -B gitops/main
  sed -i "s|^\( *newTag:\).*|\1 \"$1\"|" "$WORK/repo/deploy/k8s/overlays/main/kustomization.yaml"
  g commit -qam "deploy: $1"
  g push -qf origin gitops/main
}
bump "$MAIN_SHA"
g checkout -q -b feat main
echo "pr" >"$WORK/repo/PR.md" && g add -A && g commit -qm "feat: a PR"
g push -q origin feat
PR_SHA="$(g rev-parse HEAD)"
docker tag scproof:v1 "$IMAGE:$MAIN_SHA"
docker tag scproof:v2 "$IMAGE:$PR_SHA"

step "k3d cluster $CLUSTER (Traefik on 127.0.0.1:$PORT)"
k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
k3d cluster create "$CLUSTER" --agents 0 -p "$PORT:80@loadbalancer" \
  -v "$WORK/git:/git@server:0" --kubeconfig-update-default=false --wait --timeout 240s >/dev/null
k3d kubeconfig get "$CLUSTER" >"$KUBECONFIG"
k3d image import -c "$CLUSTER" scproof:v1 "$IMAGE:$MAIN_SHA" "$IMAGE:$PR_SHA" \
  "quay.io/argoproj/argocd:$ARGOCD_VERSION" public.ecr.aws/docker/library/redis:8.2.3-alpine >/dev/null 2>&1 \
  || k3d image import -c "$CLUSTER" scproof:v1 "$IMAGE:$MAIN_SHA" "$IMAGE:$PR_SHA" >/dev/null

step "git-daemon + fake GitHub API in namespace git"
PULLS="$(printf '[{"number":7,"title":"a PR","state":"open","head":{"ref":"feat","sha":"%s","repo":{"full_name":"acme/demo"}},"base":{"ref":"main"},"labels":[],"user":{"login":"proof"}}]' "$PR_SHA")"
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata: { name: git }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: git, namespace: git }
spec:
  selector: { matchLabels: { app: git } }
  template:
    metadata: { labels: { app: git } }
    spec:
      containers:
        - name: git
          image: scproof:v1
          imagePullPolicy: Never
          command: [git, daemon, --reuseaddr, --export-all, --base-path=/git, /git]
          ports: [{ containerPort: 9418 }]
          volumeMounts: [{ name: git, mountPath: /git }]
        - name: github
          image: scproof:v1
          imagePullPolicy: Never
          env:
            - { name: MODE, value: github }
            - { name: PORT, value: "8000" }
            - name: PULLS
              value: '$PULLS'
      volumes: [{ name: git, hostPath: { path: /git } }]
---
apiVersion: v1
kind: Service
metadata: { name: git, namespace: git }
spec:
  selector: { app: git }
  ports: [{ name: git, port: 9418 }, { name: api, port: 8000 }]
EOF
kubectl -n git rollout status deploy/git --timeout=120s >/dev/null

step "ArgoCD core $ARGOCD_VERSION (as the VPS installer does)"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/core-install.yaml" >/dev/null
kubectl -n argocd patch cm argocd-cm --type merge -p '{"data":{"timeout.reconciliation":"30s"}}' >/dev/null
kubectl -n argocd rollout restart statefulset argocd-application-controller >/dev/null
kubectl -n argocd rollout status statefulset argocd-application-controller --timeout=300s >/dev/null
kubectl -n argocd rollout status deploy argocd-applicationset-controller --timeout=300s >/dev/null
kubectl -n argocd rollout status deploy argocd-repo-server --timeout=300s >/dev/null
kubectl -n argocd create secret generic shipcrew-github --from-literal=token=fake \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

step "shipcrew ArgocdTarget: preflight, apply manifests(), wait Synced+Healthy on the main image"
run_target() {
  "$PY" - "$@" <<'EOF'
import json, sys
from omnigent.shipcrew.deploy_targets.argocd import ArgoCDTarget, ArgoConfig, manifests
from omnigent.shipcrew.store import Mission
action, *args = sys.argv[1:]
cfg = ArgoConfig(base_domain="127.0.0.1.sslip.io", scheme="http", wait_s=300, poll_s=3)
t = ArgoCDTarget(cfg)
if action == "apply":
    assert t.preflight() is None, t.preflight()
    objs = manifests(app="demo", repo_url="git://git.git.svc.cluster.local/demo.git",
                     owner="acme", repo="demo", config=cfg, mission_id="proof")
    # Point the PR generator at the fake GitHub API (a real VPS uses api.github.com).
    objs[1]["spec"]["generators"][0]["pullRequest"]["github"]["api"] = "http://git.git.svc.cluster.local:8000/"
    t.apply(objs)
    print("preflight ok, applied", [o["kind"] for o in objs])
elif action == "wait":
    st = t.wait_healthy(args[0], expected_image=args[1] if len(args) > 1 else None)
    print(args[0], st["sync"]["status"], st["health"]["status"], st["summary"].get("images"))
elif action == "teardown":
    t.teardown(Mission(id="proof", title="p", repo_path="/x",
                       repo_url="https://github.com/acme/demo", status="active", created_at=0))
    print("teardown done")
EOF
}
run_target apply
run_target wait demo "$IMAGE:$MAIN_SHA"
page_has "$APP.$DOMAIN" "demo v1" && echo "ingress: $(page "$APP.$DOMAIN")"

step "auto-sync: push an image bump (v2) + replicas 2 to gitops/main, no kubectl"
docker tag scproof:v2 "$IMAGE:v2-proof"
k3d image import -c "$CLUSTER" "$IMAGE:v2-proof" >/dev/null
g checkout -q gitops/main
cat >>"$WORK/repo/deploy/k8s/overlays/main/kustomization.yaml" <<'EOF'
replicas:
  - { name: app, count: 2 }
EOF
g commit -qam "replicas 2"
bump v2-proof
t0=$SECONDS
until_ok 240 page_has "$APP.$DOMAIN" "demo v2"
until_ok 120 test "$(kubectl -n $APP get deploy app -o jsonpath='{.status.readyReplicas}')" = 2
echo "applied by ArgoCD in $((SECONDS - t0)) s: $(page "$APP.$DOMAIN"), readyReplicas=2"

step "selfHeal: a manual scale to 4 is reverted to 2"
kubectl -n $APP scale deploy app --replicas=4 >/dev/null
until_ok 120 test "$(kubectl -n $APP get deploy app -o jsonpath='{.spec.replicas}')" = 2
echo "reverted to $(kubectl -n $APP get deploy app -o jsonpath='{.spec.replicas}')"

step "PR preview from the ApplicationSet (fake GitHub API: PR #7)"
until_ok 180 kubectl -n argocd get application demo-pr-7
run_target wait demo-pr-7 "$IMAGE:$PR_SHA"
until_ok 60 page_has "pr-7.$APP.$DOMAIN" "demo v2"
echo "pr-7: $(page "pr-7.$APP.$DOMAIN")"

step "footprint (k3s + Traefik + ArgoCD core + 3 app pods)"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "k3d-$CLUSTER-server-0"
kubectl top pods -A --no-headers 2>/dev/null | sort -k4 -h -r | head -12 || true

step "teardown"
run_target teardown
until_ok 180 sh -c "! kubectl -n argocd get applications -o name | grep -q ."
until_ok 180 sh -c "! kubectl get ns -l shipcrew.app=demo -o name | grep -q ."
echo "applications and namespaces gone"
