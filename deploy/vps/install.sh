#!/usr/bin/env bash
# shipcrew VPS installer: one machine that runs the crew AND hosts its previews.
#
#   curl -fsSL <raw url>/deploy/vps/install.sh | sudo bash      (or: sudo ./install.sh)
#
# Ubuntu 24.04 / Debian 12, amd64 or arm64, root. Idempotent: every step checks
# before it acts, config files are rewritten, k8s objects are `kubectl apply`d,
# so re-running it upgrades nothing by surprise and repairs what drifted.
#
# Installs: docker, k3s (Traefik), cert-manager + a Let's Encrypt ClusterIssuer
# (HTTP-01, no account), ArgoCD core, gh, claude, uv, node 22 + pnpm, cloudflared,
# caddy, a headless Chromium; a `shipcrew` user with the omnigent fork and the
# shipcrew bundles; systemd units for the omnigent server + host on 127.0.0.1;
# the board at https://board.<ip>.sslip.io behind basic auth.
# The operator logs in ONCE afterwards (claude, gh); a timer does the rest.
# See docs/shipcrew/VPS.md.
set -euo pipefail

SC_USER="${SC_USER:-shipcrew}"
SC_HOME="/home/$SC_USER"
OMNIGENT_REPO="${OMNIGENT_REPO:-https://github.com/Jugggernault/shipcrew-omnigent.git}"
OMNIGENT_REF="${OMNIGENT_REF:-main}"
BUNDLES_REPO="${BUNDLES_REPO:-https://github.com/Jugggernault/crewship.git}"
BUNDLES_REF="${BUNDLES_REF:-main}"
SERVER_PORT="${SERVER_PORT:-16767}"
BOARD_PORT="${BOARD_PORT:-8787}"    # caddy (basic auth) -> 127.0.0.1:$SERVER_PORT
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.3}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.21.2}"
ACME_SERVER="${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
ACME_EMAIL="${ACME_EMAIL:-}"
POD_CIDR="10.42.0.0/16"             # k3s default
ETC=/etc/shipcrew
LIB=/usr/local/lib/shipcrew

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# Run as the shipcrew user with its login PATH (~/.local/bin).
as_user() { sudo -u "$SC_USER" -H env PATH="$SC_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
user_have() { [[ -x "$SC_HOME/.local/bin/$1" ]] || have "$1"; }
kc() { k3s kubectl "$@"; }
apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@"; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in ubuntu | debian) ;; *) die "Ubuntu 24.04 or Debian 12 only (found ${ID:-?})" ;; esac
ARCH="$(dpkg --print-architecture)"   # amd64 | arm64

PUBLIC_IP="${PUBLIC_IP:-$(curl -fsS4 --max-time 10 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')}"
BASE_DOMAIN="${BASE_DOMAIN:-$PUBLIC_IP.sslip.io}"
MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
# ~1.2 GB per agent (claude ~450 MB PSS + its pnpm/build/test peaks), ~3 GB for
# the OS, k3s, ArgoCD, cert-manager, the server/host and two previews.
MAX_PARALLEL="${MAX_PARALLEL:-$(( MEM_MB > 4200 ? (MEM_MB - 3000) / 1200 : 1 ))}"

packages() {
  log "base packages"
  apt-get update -qq
  apt_install ca-certificates curl git jq gnupg tmux unzip sudo iptables openssl
}

swap() {
  # Builds and parallel agents spike RAM: a swapfile turns an OOM kill into a slowdown.
  if ! swapon --show | grep -q . && [[ ! -f /swapfile ]]; then
    log "swapfile (4 GB)"
    fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
  fi
  swapon /swapfile 2>/dev/null || true
}

user() {
  id "$SC_USER" >/dev/null 2>&1 || { log "user $SC_USER"; useradd -m -s /bin/bash "$SC_USER"; }
  loginctl enable-linger "$SC_USER" 2>/dev/null || true
}

docker_engine() {
  have docker || { log "docker"; curl -fsSL https://get.docker.com | sh; }
  systemctl enable --now docker >/dev/null
  usermod -aG docker "$SC_USER"
}

node_pnpm() {
  if ! have node || [[ "$(node -p 'process.versions.node.split(".")[0]')" -lt 22 ]]; then
    log "node 22"
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
    apt_install nodejs
  fi
  have pnpm || { corepack enable && corepack prepare pnpm@latest --activate >/dev/null; }
  have pnpm || npm i -g pnpm >/dev/null
}

github_cli() {
  if ! have gh; then
    log "gh"
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      >/etc/apt/sources.list.d/github-cli.list
    apt-get update -qq && apt_install gh
  fi
}

claude_cli() {
  have claude || { log "claude"; npm i -g @anthropic-ai/claude-code >/dev/null; }
}

uv_user() {
  user_have uv || { log "uv"; as_user sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null; }
}

cloudflared_cli() {
  have cloudflared || {
    log "cloudflared"
    curl -fsSL -o /tmp/cloudflared.deb \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb"
    dpkg -i /tmp/cloudflared.deb >/dev/null && rm -f /tmp/cloudflared.deb
  }
}

caddy_proxy() {
  have caddy || { log "caddy"; apt_install caddy; }
}

chromium_browser() {
  # Playwright and the chrome-devtools MCP use it (CHROMIUM_PATH), never a download.
  if [[ -z "$(chromium_path)" ]]; then
    log "headless browser"
    if [[ $ID == debian ]]; then
      apt_install chromium fonts-liberation
    elif [[ $ARCH == amd64 ]]; then
      curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
      apt_install /tmp/chrome.deb fonts-liberation && rm -f /tmp/chrome.deb
    else
      # Ubuntu arm64: chromium is a snap only; Playwright's build + its apt deps.
      as_user npx -y playwright@latest install chromium >/dev/null
      npx -y playwright@latest install-deps chromium >/dev/null
    fi
  fi
}

chromium_path() {
  local p
  for p in /usr/bin/chromium /usr/bin/google-chrome-stable; do [[ -x $p ]] && { echo "$p"; return; }; done
  find "$SC_HOME/.cache/ms-playwright" -path '*chrome-linux/chrome' -type f 2>/dev/null | head -1
}

k3s_cluster() {
  if ! have k3s; then
    log "k3s (with Traefik)"
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --write-kubeconfig-mode=0600" sh -
  fi
  systemctl enable --now k3s >/dev/null
  for _ in $(seq 1 60); do kc get nodes >/dev/null 2>&1 && break; sleep 2; done
  kc wait --for=condition=Ready node --all --timeout=180s >/dev/null
  install -d -o "$SC_USER" -g "$SC_USER" -m 700 "$SC_HOME/.kube"
  install -o "$SC_USER" -g "$SC_USER" -m 600 /etc/rancher/k3s/k3s.yaml "$SC_HOME/.kube/config"
  have kubectl || ln -sf "$(command -v k3s)" /usr/local/bin/kubectl
}

cert_manager() {
  log "cert-manager $CERT_MANAGER_VERSION + ClusterIssuer letsencrypt"
  kc apply -f "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.yaml" >/dev/null
  kc -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s >/dev/null
  # The webhook answers a few seconds after it is Ready: retry the issuer.
  local email_line=""
  [[ -n $ACME_EMAIL ]] && email_line="    email: $ACME_EMAIL"
  for _ in $(seq 1 20); do
    kc apply -f - >/dev/null 2>&1 <<EOF && break
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt
spec:
  acme:
    server: $ACME_SERVER
$email_line
    privateKeySecretRef: { name: letsencrypt-account }
    solvers:
      - http01:
          ingress: { ingressClassName: traefik }
EOF
    sleep 5
  done
  kc get clusterissuer letsencrypt >/dev/null || die "could not create the letsencrypt ClusterIssuer"
}

argocd_core() {
  log "ArgoCD core $ARGOCD_VERSION (no UI/API server: the board is the UI)"
  kc create namespace argocd --dry-run=client -o yaml | kc apply -f - >/dev/null
  # Server-side: the ApplicationSet CRD is too big for a client-side apply annotation.
  kc apply -n argocd --server-side --force-conflicts \
    -f "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/core-install.yaml" >/dev/null
  # The API server (not in core) is what normally creates the `default` project.
  # Right after the apply a CRD may have no status yet: `wait` errors, so retry.
  for _ in $(seq 1 60); do
    kc wait --for=condition=Established --timeout=10s crd/appprojects.argoproj.io \
      crd/applications.argoproj.io crd/applicationsets.argoproj.io >/dev/null 2>&1 && break
    sleep 2
  done
  kc apply -f - >/dev/null <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata: { name: default, namespace: argocd }
spec:
  sourceRepos: ["*"]
  destinations: [{ namespace: "*", server: "*" }]
  clusterResourceWhitelist: [{ group: "*", kind: "*" }]
EOF
  # No webhook without the API server: poll git every 60 s instead of 180 s.
  if [[ "$(kc -n argocd get cm argocd-cm -o jsonpath='{.data.timeout\.reconciliation}' 2>/dev/null)" != 60s ]]; then
    kc -n argocd patch cm argocd-cm --type merge -p '{"data":{"timeout.reconciliation":"60s"}}' >/dev/null
    kc -n argocd rollout restart statefulset argocd-application-controller >/dev/null
  fi
  kc -n argocd rollout status statefulset argocd-application-controller --timeout=300s >/dev/null
  kc -n argocd rollout status deploy argocd-applicationset-controller --timeout=300s >/dev/null
}

checkout() {  # checkout <repo> <ref> <dir>: clone once, then fast-forward only
  local repo="$1" ref="$2" dir="$3"
  if [[ ! -d $dir/.git ]]; then
    as_user git clone -q --branch "$ref" "$repo" "$dir"
  else
    as_user git -C "$dir" fetch -q origin "$ref" && as_user git -C "$dir" merge -q --ff-only FETCH_HEAD \
      || echo "  $dir has local changes: left as is"
  fi
}

shipcrew_code() {
  log "omnigent fork + shipcrew bundles"
  checkout "$OMNIGENT_REPO" "$OMNIGENT_REF" "$SC_HOME/omnigent"
  checkout "$BUNDLES_REPO" "$BUNDLES_REF" "$SC_HOME/shipcrew"
  as_user sh -c "cd '$SC_HOME/omnigent' && uv sync --extra all -q"
  as_user sh -c "cd '$SC_HOME/omnigent' && pnpm install --frozen-lockfile --filter web --silent && pnpm --filter web build >/dev/null"
}

env_file() {
  install -d -m 755 "$ETC" "$LIB"
  local state="$SC_HOME/.local/share/shipcrew-state"
  as_user mkdir -p "$state/data" "$state/config" "$state/logs"
  grep -qs '^telemetry: false' "$state/config/config.yaml" || as_user sh -c "printf 'telemetry: false\n' >>'$state/config/config.yaml'"
  cat >"$ETC/shipcrew.env" <<EOF
# Written by deploy/vps/install.sh (re-run to regenerate). Read by shipcrew-*.service.
PATH=$SC_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
KUBECONFIG=$SC_HOME/.kube/config
OMNIGENT_DATA_DIR=$state/data
OMNIGENT_CONFIG_HOME=$state/config
OMNIGENT_DATABASE_URI=sqlite:///$state/data/chat.db
OMNIGENT_URL=http://127.0.0.1:$SERVER_PORT
OMNIGENT_ANALYTICS=0
OMNIGENT_DISABLE_TELEMETRY=true
OMNIGENT_NO_UPDATE_CHECK=1
DO_NOT_TRACK=1
SHIPCREW_AGENTS_DIR=$SC_HOME/shipcrew/agents
SHIPCREW_MAX_PARALLEL=$MAX_PARALLEL
SHIPCREW_DEPLOY_TARGET=argocd
SHIPCREW_BASE_DOMAIN=$BASE_DOMAIN
SHIPCREW_KUBECTL=/usr/local/bin/kubectl
CHROMIUM_PATH=$(chromium_path)
EOF
}

units() {
  log "systemd: shipcrew-server, shipcrew-host (127.0.0.1:$SERVER_PORT)"
  local state="$SC_HOME/.local/share/shipcrew-state" uv="$SC_HOME/.local/bin/uv"
  cat >/etc/systemd/system/shipcrew-server.service <<EOF
[Unit]
Description=shipcrew: omnigent server (board + API, localhost only)
After=network-online.target k3s.service
Wants=network-online.target

[Service]
User=$SC_USER
WorkingDirectory=$SC_HOME/omnigent
EnvironmentFile=$ETC/shipcrew.env
ExecStart=$uv run --no-sync omnigent --log-to-stderr server --no-open --host 127.0.0.1 --port $SERVER_PORT --database-uri sqlite:///$state/data/chat.db --artifact-location $state/data/artifacts
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  cat >/etc/systemd/system/shipcrew-host.service <<EOF
[Unit]
Description=shipcrew: omnigent host (runs the agent sessions)
After=shipcrew-server.service
Requires=shipcrew-server.service

[Service]
User=$SC_USER
WorkingDirectory=$SC_HOME
EnvironmentFile=$ETC/shipcrew.env
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 180); do curl -fsS http://127.0.0.1:$SERVER_PORT/health >/dev/null && exit 0; sleep 1; done; exit 1'
ExecStart=$uv run --no-sync --project $SC_HOME/omnigent omnigent --log-to-stderr host --server http://127.0.0.1:$SERVER_PORT --no-open --non-interactive
Restart=on-failure
RestartSec=5
# Agent sessions live in this unit's cgroup: a stop ends them.
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable shipcrew-server shipcrew-host >/dev/null
  systemctl restart shipcrew-server shipcrew-host
}

board() {
  log "board: https://board.$BASE_DOMAIN (basic auth, Traefik -> caddy -> 127.0.0.1:$SERVER_PORT)"
  local pass_file="$ETC/board-password"
  [[ -s $pass_file ]] || { openssl rand -base64 18 | tr -d '/+=' >"$pass_file"; chmod 600 "$pass_file"; }
  local hash
  hash="$(caddy hash-password --plaintext "$(cat "$pass_file")")"
  # Caddy never takes 80/443 (Traefik has them): a plain listener that only pods reach.
  # `basicauth`: the spelling of the apt caddy 2.6, still accepted by 2.8+.
  cat >/etc/caddy/Caddyfile <<EOF
{
	auto_https off
	admin off
}
http://:$BOARD_PORT {
	basicauth {
		$SC_USER $hash
	}
	reverse_proxy 127.0.0.1:$SERVER_PORT
}
EOF
  systemctl enable caddy >/dev/null && systemctl restart caddy
  # Only pods (Traefik) and loopback may reach the caddy port.
  cat >"$LIB/board-firewall.sh" <<EOF
#!/bin/sh
iptables -C INPUT -p tcp --dport $BOARD_PORT ! -i lo ! -s $POD_CIDR -j DROP 2>/dev/null \
  || iptables -I INPUT -p tcp --dport $BOARD_PORT ! -i lo ! -s $POD_CIDR -j DROP
EOF
  chmod 755 "$LIB/board-firewall.sh"
  cat >/etc/systemd/system/shipcrew-board-firewall.service <<EOF
[Unit]
Description=shipcrew: board port reachable from pods only
After=network-online.target

[Service]
Type=oneshot
ExecStart=$LIB/board-firewall.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload && systemctl enable --now shipcrew-board-firewall >/dev/null
  local node_ip
  node_ip="$(kc get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  kc apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata: { name: shipcrew }
---
apiVersion: v1
kind: Service
metadata: { name: board, namespace: shipcrew }
spec:
  ports: [{ name: http, port: 80, targetPort: $BOARD_PORT }]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: board-host
  namespace: shipcrew
  labels: { kubernetes.io/service-name: board }
addressType: IPv4
ports: [{ name: http, port: $BOARD_PORT, protocol: TCP }]
endpoints: [{ addresses: ["$node_ip"] }]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: board
  namespace: shipcrew
  annotations: { cert-manager.io/cluster-issuer: letsencrypt }
spec:
  tls: [{ hosts: [board.$BASE_DOMAIN], secretName: board-tls }]
  rules:
    - host: board.$BASE_DOMAIN
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: board, port: { name: http } } }
EOF
}

post_login_timer() {
  log "timer: GitHub token -> ArgoCD + GHCR pulls, bundle setup (after the one-time logins)"
  cat >"$LIB/post-login.sh" <<EOF
#!/usr/bin/env bash
# Every 2 min (shipcrew-post-login.timer). Does nothing until the operator logged in.
set -euo pipefail
kc() { k3s kubectl "\$@"; }
as_user() { sudo -u $SC_USER -H env PATH=$SC_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin "\$@"; }
if tok="\$(as_user gh auth token 2>/dev/null)" && [[ -n \$tok ]]; then
  login="\$(as_user gh api user --jq .login)"
  # PR generator token + credentials for every github.com repo (private ones included).
  kc -n argocd create secret generic shipcrew-github --from-literal=token="\$tok" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc -n argocd create secret generic shipcrew-github-repos \
    --from-literal=type=git --from-literal=url=https://github.com/ \
    --from-literal=username="\$login" --from-literal=password="\$tok" \
    --dry-run=client -o yaml | kc label --local -f - argocd.argoproj.io/secret-type=repo-creds -o yaml \
    | kc apply -f - >/dev/null
  # GHCR pulls for every namespace (previews are created on the fly): node-wide.
  want="\$(printf 'configs:\n  ghcr.io:\n    auth:\n      username: %s\n      password: %s\n' "\$login" "\$tok")"
  if [[ "\$(cat /etc/rancher/k3s/registries.yaml 2>/dev/null)" != "\$want" ]]; then
    umask 077; printf '%s\n' "\$want" >/etc/rancher/k3s/registries.yaml
    systemctl restart k3s   # containerd reads it at start; running pods keep running
  fi
fi
# The bundles' skills/plugins/MCP servers, once claude is logged in.
if [[ -f $SC_HOME/.claude/.credentials.json && ! -f $SC_HOME/.shipcrew-setup-done ]]; then
  as_user bash -c "cd $SC_HOME/shipcrew && ./setup.sh" && as_user touch $SC_HOME/.shipcrew-setup-done
fi
EOF
  chmod 755 "$LIB/post-login.sh"
  cat >/etc/systemd/system/shipcrew-post-login.service <<EOF
[Unit]
Description=shipcrew: sync the GitHub token into k3s/ArgoCD, finish bundle setup

[Service]
Type=oneshot
ExecStart=$LIB/post-login.sh
EOF
  cat >/etc/systemd/system/shipcrew-post-login.timer <<EOF
[Unit]
Description=shipcrew: post-login sync every 2 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload && systemctl enable --now shipcrew-post-login.timer >/dev/null
}

summary() {
  cat <<EOF

==============================================================================
 shipcrew is installed.   board: https://board.$BASE_DOMAIN
                          user:  $SC_USER   password: $(cat "$ETC/board-password")
 previews: https://<app>.$BASE_DOMAIN and https://pr-<n>.<app>.$BASE_DOMAIN
 parallel agents: $MAX_PARALLEL (RAM ${MEM_MB} MB; SHIPCREW_MAX_PARALLEL in $ETC/shipcrew.env)

 Do this ONCE, interactively (nothing else is manual):
   sudo -iu $SC_USER claude            # log in with your Claude subscription, then /exit
   sudo -iu $SC_USER gh auth login -s repo,workflow,read:packages
 Within 2 minutes the shipcrew-post-login timer stores the GitHub token for
 ArgoCD (PR previews, private repos) and GHCR pulls, and installs the bundles'
 skills. Check: systemctl status shipcrew-server shipcrew-host shipcrew-post-login
==============================================================================
EOF
}

main() {
  packages
  swap
  user
  docker_engine
  node_pnpm
  github_cli
  claude_cli
  uv_user
  cloudflared_cli
  caddy_proxy
  chromium_browser
  k3s_cluster
  cert_manager
  argocd_core
  shipcrew_code
  env_file
  units
  board
  post_login_timer
  summary
}

main "$@"
