# shipcrew on a VPS: ArgoCD previews + the crew on one machine

One Linux VPS runs the whole thing: the omnigent server + host (the crew, its
board), and a single-node k3s cluster where ArgoCD keeps every project's
preview in sync with its GitHub repo. Deploy target: `SHIPCREW_DEPLOY_TARGET=argocd`
(`omnigent/shipcrew/deploy_targets/argocd.py`).

## Architecture

```
                    GitHub                                        VPS (Ubuntu 24.04 / Debian 12)
 ┌──────────────────────────────────────┐        ┌──────────────────────────────────────────────────────────┐
 │ repo acme/shop                       │        │ systemd                                                    │
 │  main  ── push ──► Actions gitops.yml│        │  shipcrew-server  127.0.0.1:16767  (board + API)           │
 │   deploy/k8s/base      (Deploy,Svc,  │        │  shipcrew-host    runs claude sessions (tmux, worktrees)   │
 │                         Ingress)     │        │  caddy :8787      basic auth ─► 127.0.0.1:16767            │
 │   deploy/k8s/overlays/main           │        │  shipcrew-post-login.timer  gh token ─► k3s / ArgoCD       │
 │  gitops/main = main + "deploy: <sha>"│◄─push──┤        ▲ git push / gh (agents, PR loop)                  │
 │  PR #7 (head sha)                    │        │        │                                                   │
 │ ghcr.io/acme/shop:<sha> ◄── build ───┘        │  k3s ──┼───────────────────────────────────────────────── │
 └──────────▲───────────────▲───────────┘        │  Traefik :80/:443 ◄── https://*.<ip>.sslip.io (wildcard DNS)│
            │ poll 60 s     │ pull images        │   ├─ shop.<ip>.sslip.io        ─► ns shop        (main)     │
            │               │                    │   ├─ pr-7.shop.<ip>.sslip.io   ─► ns shop-pr-7   (PR #7)    │
            │               │                    │   └─ board.<ip>.sslip.io       ─► caddy :8787 ─► board      │
            │               │                    │  cert-manager: ClusterIssuer letsencrypt (HTTP-01 via Traefik)│
            │               └────────────────────┤  containerd: registries.yaml (ghcr.io token)               │
            └────────────────────────────────────┤  ArgoCD core: Application shop (gitops/main, prune+selfHeal)│
                                                 │              ApplicationSet shop-pr (Pull Request generator)│
                                                 └──────────────────────────────────────────────────────────┘
```

Flow for one mission:

1. First task start: the server commits `.github/workflows/ci.yml` (existing) and,
   with the argocd target, one `chore: shipcrew gitops` commit
   (`gitops_install.py`): `deploy/k8s/base/{kustomization,deployment,service,ingress}.yaml`,
   `deploy/k8s/overlays/main/kustomization.yaml`, `.github/workflows/gitops.yml`.
   Six small files, plain kustomize, no Helm. Files already there are kept.
2. Every push (main or a PR): `gitops.yml` builds the image (the repo's `Dockerfile`,
   else a generic Node one: install, `build`, `npm start` on `$PORT=3000`) and pushes
   `ghcr.io/<owner>/<repo>:<sha>` with the built-in `GITHUB_TOKEN`; no secret to add.
3. On main, the same job force-pushes `gitops/main` = main + one commit setting
   `newTag: "<sha>"` in the overlay.
4. ArgoCD (polling every 60 s) syncs `gitops/main` into namespace `<app>`
   (`https://<app>.<ip>.sslip.io`), and the ApplicationSet renders `deploy/k8s/base`
   at each open PR's head SHA with that SHA's image into `<app>-pr-<n>`
   (`https://pr-<n>.<app>.<ip>.sslip.io`). A closed PR's preview is deleted.
5. Ship: `ArgoCDTarget.deploy()` installs the files if they are missing, applies the
   Application + ApplicationSet (`kubectl apply`, idempotent), and waits (bounded,
   `SHIPCREW_ARGOCD_WAIT_S`, default 900 s) for Synced + Healthy **on the image of the
   current `origin/main` SHA** (`status.summary.images`), then returns the URL.
   `teardown(mission)` deletes both by label (the ArgoCD finalizer removes the
   resources) and the namespaces.

### Why a `gitops/main` branch and not Image Updater or a commit on main

- A bot commit on **main** is refused: main is protected with the required `ci`
  check (`gh.protect_main`), and `GITHUB_TOKEN` is not an admin. Even if allowed,
  every open agent PR would become "out of date" (strict checks) after each merge.
- **ArgoCD Image Updater** is one more controller, needs GHCR list credentials, and
  its git write-back needs a push token anyway (or its non-declarative "argocd"
  write-back, which a re-apply of the Application erases).
- `gitops/main` needs nothing but the workflow's own token, is fully declarative
  (git is the truth, `git log gitops/main` is the deploy history), cannot loop
  (`GITHUB_TOKEN` pushes trigger no workflow, and `ci.yml`/`gitops.yml` only run on
  pushes to main), and PR previews need no bump at all: the ApplicationSet uses
  `{{.head_sha}}` for both the revision and the image tag.

## Install

```bash
ssh root@<vps>
curl -fsSL https://raw.githubusercontent.com/Jugggernault/shipcrew-omnigent/main/deploy/vps/install.sh -o install.sh
sudo bash install.sh        # ~10 min; re-run any time (idempotent)
```

Options (env): `OMNIGENT_REPO/REF`, `BUNDLES_REPO/REF`, `BASE_DOMAIN` (default
`<public ip>.sslip.io`), `MAX_PARALLEL`, `ACME_EMAIL` (optional),
`ACME_SERVER` (e.g. Let's Encrypt staging), `ARGOCD_VERSION` (v3.5.3),
`CERT_MANAGER_VERSION` (v1.21.2). Open ports 80 and 443; nothing else is public
(the server and host listen on 127.0.0.1, caddy's :8787 is dropped by iptables for
anything but pods).

What it installs: docker, k3s (Traefik, metrics-server), cert-manager + ClusterIssuer
`letsencrypt` (HTTP-01, no account), ArgoCD **core** (application controller,
repo-server, applicationset controller, redis; no UI / API server / dex / notifications:
the board is the UI, `argocd --core` works for debugging), gh, claude, uv, node 22 +
pnpm, cloudflared, caddy, Chromium (Debian) or Google Chrome (Ubuntu amd64) for
Playwright/chrome-devtools, a 4 GB swapfile, user `shipcrew` with `~/omnigent` (the
fork, `uv sync`, web UI built) and `~/shipcrew` (the bundles), `/etc/shipcrew/shipcrew.env`,
units `shipcrew-server`, `shipcrew-host`, `shipcrew-board-firewall`,
`shipcrew-post-login.timer`.

## Automatic vs one-time human

| | who | when |
|---|---|---|
| everything in the installer | script | install / re-run |
| `sudo -iu shipcrew claude` (subscription login, then `/exit`) | **human, once** | after install |
| `sudo -iu shipcrew gh auth login -s repo,workflow,read:packages` | **human, once** | after install |
| GitHub token into `argocd/shipcrew-github` (PR generator), `repo-creds` for `https://github.com/` (private repos), `/etc/rancher/k3s/registries.yaml` (GHCR pulls, k3s restarted once) | timer, every 2 min | as soon as gh is logged in |
| bundles `setup.sh` (skills, plugins, MCP servers) | timer, once | as soon as claude is logged in |
| per project: gitops files, image builds, `gitops/main`, Application + ApplicationSet, TLS certs, PR previews and their cleanup | server / Actions / ArgoCD / cert-manager | every mission, no human |

The installer prints the two logins and the board password
(`/etc/shipcrew/board-password`) and nothing else.

## Sizing

Measured numbers (SPIKE.md, this branch's k3d proof):

| component | RAM |
|---|---|
| claude-native agent session (claude + runner + MCP bridge + tmux), PSS | **~420–470 MB** |
| omnigent server + host + zygote | ~345 MB (170 + 145 + 30) |
| k3s + Traefik + ArgoCD core + 3 tiny app pods (k3d node container, measured) | see "k3d proof" below |
| cert-manager (3 pods) | ~100 MB (upstream figure, not measured here) |
| one Next.js preview (limit 256 Mi, request 128 Mi) | 100–250 MB |

An agent is not only its claude process: `pnpm install`, `next build`, vitest and a
headless Chromium for e2e spike by 0.5–1 GB while they run. The installer budgets
**~1.2 GB per parallel agent** plus ~3 GB fixed (OS, k3s, ArgoCD, cert-manager,
server/host, two previews): `MAX_PARALLEL = (RAM_MB - 3000) / 1200`, written to
`SHIPCREW_MAX_PARALLEL`. The headless-mode (non-TUI) per-session number is being
measured separately; if it is lower, raise `SHIPCREW_MAX_PARALLEL`.

| VPS | vCPU | parallel agents | notes |
|---|---|---|---|
| 4 GB | 2 | 1 | works, slow builds; swap is used |
| 8 GB | 4 | 4 | the sweet spot for one mission at a time |
| 16 GB | 8 | 10 | several missions; CPU becomes the limit during builds |
| 32 GB | 16 | 20+ | builds in parallel stay fast |

CPU: a claude turn uses 40–85 % of a core (SPIKE.md), a Next.js build a full core
for 30–90 s. Disk: 40 GB minimum (images, node_modules per worktree, ArgoCD cache).

## Local proof (k3d)

`deploy/vps/k3d-proof.sh` (no sudo, no cloud, no GitHub; kubectl and k3d are fetched
into `~/.local/bin` through the tools registry). It creates a throwaway cluster,
installs ArgoCD core the way the installer does, serves the repo (rendered from the
real templates) with git-daemon inside the cluster, fakes the GitHub PR API for the
ApplicationSet, drives `ArgoCDTarget` (preflight, apply, wait, teardown) and deletes
the cluster at the end. Results: see the proof section added below.

## Configuration reference

| env | default | |
|---|---|---|
| `SHIPCREW_DEPLOY_TARGET` | | `argocd` selects this target |
| `SHIPCREW_BASE_DOMAIN` | | `<ip>.sslip.io` (required) |
| `SHIPCREW_ARGOCD_NAMESPACE` | `argocd` | |
| `SHIPCREW_ARGOCD_GITHUB_SECRET` | `shipcrew-github` | key `token` |
| `SHIPCREW_ARGOCD_PR_PREVIEWS` | `1` | `0`: no ApplicationSet |
| `SHIPCREW_ARGOCD_WAIT_S` | `900` | Synced + Healthy deadline at ship |
| `SHIPCREW_ARGOCD_TLS` | `1` | `0`: `http://` URLs (local clusters) |
| `SHIPCREW_KUBECTL` | PATH | like every tool |
