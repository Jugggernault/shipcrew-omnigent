# shipcrew resources: what a session costs, and how to fit more on one VPS

Measured on 2026-09-30 on this machine (Arch Linux, 22 CPUs, 15 GiB RAM, a
desktop running next to the stack), Claude Code 2.1.285 on the Claude
subscription login. Each run used an isolated stack (`scripts/shipcrew_stack.sh`,
port 16791, its own state dir) and the real `developer` bundle of shipcrew
`v3-headless`. The harness scripts are in
`/home/jugggernault/Work/Projects/omnigent-wt/headless-sandbox/` (`measure.py`,
`hostmon.py`, `treemon.py`, `probe.py`, `board_e2e.py`).

**Decision.** Worker sessions run headless (`claude-sdk`) by default for the
developer, reviewer, integrator and devops roles, and stay on `claude-native`
for designer, scaffolder, qa and security. The setting is
`SHIPCREW_WORKER_HARNESS=auto`. Under this default the developer, the role with
the most sessions in parallel, uses **~300 MB instead of ~660 MB** per session,
**half the CPU per turn**, **under half the idle CPU**, and **~40 % less
subscription quota** per turn. Every shipcrew guarantee holds on `claude-sdk`
except three: the role's own MCP servers, Claude Task sub-agents, and attaching
to the live Claude TUI. The roles that need those stay on native, and
`SHIPCREW_WORKER_HARNESS=native` restores the old behaviour.

## 1. Agent sessions: claude-native vs claude-sdk

The task was the same small coding job for every run. The repo was a tiny
CommonJS project with `src/sum.js` and a node:test suite. The prompt was: add
`mul(a, b)` in `src/math.js` plus a test in `test/math.test.js`, run
`npm test` until it passes, commit, and end with `PASS`. The task contract
(owned paths `src/math.js` and `test/math.test.js`) was injected exactly as the
board injects it. Every run committed the change.

The numbers below cover the whole process tree of a session:

- the runner forked from the zygote;
- `claude`;
- for native, the MCP bridge, the per-call hook processes and tmux;
- the MCP servers;
- the commands the agent ran.

PSS is read from `/proc/<pid>/smaps_rollup`, which counts shared pages once,
split across the processes that share them. CPU is utime+stime, sampled every
0.25 s. "native" is the bundle as authored, and it loads the shadcn MCP server.
"native, no MCP" is the same bundle without `mcp_config`, which makes it
comparable to sdk.

### One session at a time

| | claude-sdk | claude-native, no MCP | claude-native (shadcn MCP) |
|---|---|---|---|
| runs | 3 | 2 | 2 |
| PSS peak during the turn | 376-476 MB | 510-516 MB | 776-777 MB |
| PSS idle after the turn | 378-379 MB | 492-503 MB | 755-770 MB |
| `claude` process (idle PSS) | 200 MB | 252-264 MB | 251-267 MB |
| runner (zygote fork) | 175 MB | 174 MB | 174 MB |
| MCP bridge / hooks | none | 63 MB bridge + one python hook per tool call | same |
| shadcn MCP (`npx shadcn mcp`) | not loaded | not loaded | **265 MB** |
| tmux | 3 MB (placeholder pane) | 2 MB | 2 MB |
| CPU per turn | 5.8-6.6 s | 10.2-11.2 s | 13.1-13.6 s |
| idle CPU (% of one core) | 1.9-2.9 % | 6.1-6.8 % | 6.2-6.8 % |
| server + host CPU per turn | 2.4-2.9 s | 2.1-2.5 s | 2.3-2.5 s |
| wall (create to verdict) | 30-42 s | 24-30 s | 25-33 s |
| cost per turn (`total_cost_usd`) | $0.074-0.081 | $0.14 (one cache-miss run: $0.39) | $0.13 |
| tool calls | ToolSearch + `sys_os_*` | Bash / Write | Bash / Write |

### Three sessions in parallel (the VPS case)

Measured as the PSS of the whole host tree, sampled every second. The baseline
(host + zygote, no session) was 278 MB.

| | claude-sdk | claude-native, no MCP | claude-native (shadcn MCP) |
|---|---|---|---|
| host tree peak, 3 sessions | 1175 MB | 1526 MB | 2255 MB |
| **per session** | **~300 MB** | **~415 MB** | **~660 MB** |
| CPU per session per turn | 6.2 s | 11.8 s | 13.7 s |
| idle CPU per session | 3.1 % | ~7 % | 7.1 % |
| wall per session | 33-34 s | 29-41 s | 29-32 s |
| after `stop_session` | back to 278 MB | back to 279 MB | back to 279 MB |

The runner's pages are shared with the zygote, so the per-session cost drops in
parallel. `stop_session` frees everything: the host tree goes back to its
baseline.

Fixed cost of the stack: the server tree is ~200 MB PSS and the host tree
(host + zygote) is ~250-280 MB, so **about 0.5 GB**.

Why sdk is lighter:

- no TUI redraw loop (that is the idle CPU of native);
- no MCP bridge process;
- no Python hook process spawned for every tool call;
- a smaller `claude` process, because the tool set is slimmer: Skill, ToolSearch
  and omnigent's `sys_os_*`, against the ~40 built-in tools of the TUI;
- the smaller system prompt is also why a turn costs less quota.

sdk is 10-30 % slower in wall time per turn, because each OS call is a round
trip through the server's policy path. That is small next to the model's time.

### Guarantees on claude-sdk (checked live and in `validate_agents.py`)

| guarantee | on claude-sdk | evidence |
|---|---|---|
| Guardrail DENY with a hint | **kept** | live: `sed -i '/mul/,+1d'` came back as "Denied by policy: … Edit files with the Edit tool …"; an edit to another task's file came back as "`src/sum.js` belongs to task 'Sum refactor' (in progress) …". The agent read the hint and moved on. |
| Guardrail ASK becomes an approval card | **kept** | live: a write to `README.md` outside the owned paths produced a pending elicitation (`shipcrew_owned_paths`, with a preview). Once accepted, the write ran. |
| Same verdicts as native | **kept** | `validate_agents.py` renders each worker bundle the way the server does and runs 250 guardrail cases per bundle as `sys_os_shell` / `sys_os_write` / `sys_os_edit` / `sys_os_read`: every verdict equals native. |
| Owned-paths contract injection | **kept** | the same `# @task.*` slots, injected before the harness rewrite (unit test + live). |
| Merge-gate memory of an accepted ASK | **kept (new)** | the relay-path approval now calls the board hook (`approvals.notify_accepted_relay_ask`). Live on the board path: `approved_paths == ["README.md"]`. |
| Strict MCP per role | **strict kept; role servers dropped** | the `claude` argv has `--strict-mcp-config`, so no host-user or claude.ai servers load. But the bundle's `mcp_config` (shadcn, chrome-devtools) is **not** loaded by the SDK path, so under `auto` the roles that use one stay native. The developer on sdk uses `npx shadcn add` / the Foundation's components instead of the shadcn MCP. |
| Setting-sources isolation | **kept** | argv has `--setting-sources=project,local`. The skills are the bundle's own (`reviewer:design-lock`) plus Claude's built-ins (`code-review`, …), with no user plugins. |
| Board tree / child visibility | **kept** | loop children (reviewer, integrator) are created through the API and listed in `child_sessions` on either harness. |
| Claude Task sub-agents | **not available** | the SDK harness exposes no Task / Agent tool, so a worker cannot fan out. That is a capability loss, not a hidden-agent risk. |
| Cost reporting | **kept** | `total_cost_usd` per session feeds the card's cost and the budget gate. |
| Interrupt / stop | **kept** | an interrupt goes idle in 1.5 s (native: 0.6 s). `stop_session` kills the process tree, and the next message relaunches the session with its conversation (checked on both harnesses). |
| Human attaches to the live Claude TUI | **native only** | an sdk session's terminal is omnigent's view of the session, not Claude Code's TUI. For a live terminal, use `SHIPCREW_WORKER_HARNESS=native` (or `developer=native`). |

## 2. Headless Chromium: chrome-headless-shell vs full Chromium

The e2e suite of the generated Next.js polls app (`shipcrew-demo-web-4`,
5 Playwright tests) ran against a fresh `next start` production server. The
number is the peak PSS of the browser processes, from `playwright test`'s tree.
Full Chromium is `/usr/bin/chromium` 152, which Playwright launches with
`--headless` (the new headless mode). The shell is Playwright's
`chrome-headless-shell` 151 from `~/.cache/ms-playwright`.

| Playwright workers | browser | browser PSS peak | test tree PSS peak | CPU | wall |
|---|---|---|---|---|---|
| 2 | full Chromium | 754-769 MB | 1288-1309 MB | 11.9-12.4 s | 5.2-5.4 s |
| 2 | **chrome-headless-shell** | **325-326 MB** | 867-885 MB | 9.0 s | 4.5-4.6 s |
| 1 | full Chromium | 483-495 MB | 920-926 MB | 10.2-10.6 s | 7.7-7.8 s |
| 1 | **chrome-headless-shell** | **220-222 MB** | 651-655 MB | 7.1-7.2 s | 6.3-6.5 s |
| default (11 on 22 CPUs) | full Chromium | 1186-1366 MB | 1924-2154 MB | 15-18 s | 5.7-8.2 s |
| default (11 on 22 CPUs) | chrome-headless-shell | 494-508 MB | 1269-1277 MB | 10.5-10.8 s | 7.7-7.9 s |

The default-workers rows reused one server across runs, so 1 of the 5 tests failed on state the earlier runs had left behind; that does not change the memory picture. The other rows got a fresh server per run and all 5 tests passed.

The headless shell uses **55-58 % less browser memory**, 25-30 % less CPU, and
is slightly faster. Playwright already passes `--headless`,
`--disable-dev-shm-usage` and swiftshader (no GPU) to Chromium by default, so
no flags are needed in `playwright.config`. Playwright's default worker count
is half the CPUs. On a small VPS that is 1-2 workers, which is the cheapest
setting anyway.

What is wired now (none of it downloads a browser):

- `scripts/shipcrew_stack.sh` exports `CHROMIUM_PATH` for every agent session.
  It uses `chrome-headless-shell` from PATH, else the newest one in
  `$PLAYWRIGHT_BROWSERS_PATH` / `~/.cache/ms-playwright`, else nothing (the
  apps' default `/usr/bin/chromium`). The qa / security chrome-devtools MCP
  (`--headless --isolated --executablePath ${CHROMIUM_PATH:-…}`) picks it up
  the same way.
- `omnigent/shipcrew/tools.py` `session_env()` does the same for server-side
  subprocesses. The order is `SHIPCREW_CHROMIUM`, then the headless shell, then
  the resolved Chromium.
- The CI template has a "headless browser" step. A runner that has the shell
  (a self-hosted VPS) uses it; GitHub-hosted runners keep
  `/usr/bin/google-chrome`, headless.
- COMMON: Playwright is always headless. Never use `headless: false`,
  `--headed`, `--ui`, `--debug` or `page.pause()`.

## 3. A generated app: shipcrew-demo-web-4 (Next.js 16 polls)

Measured on a local clone. The app repo was not changed.

| | value |
|---|---|
| `node_modules` | 661 MB on disk (549 MB apparent), hard links into the shared pnpm store |
| largest packages | next 197 MB, @next/swc 92 MB, lucide-react 39 MB, typescript 23 MB, rolldown 18 MB, @base-ui/react 18 MB, sharp-libvips 17 MB, playwright-core 12 MB |
| install (warm store) | 2.1 s, 7.6 CPU s |
| `next build` | 12.6 s wall, 40.8 CPU s, **peak tree PSS 1.44 GB** (RSS 2.3 GB; 9 page-data workers on 22 CPUs) |
| `.next` | 60 MB (cache 48 MB, server 9.8 MB, static 0.9 MB) |
| client JS, all chunks | 647 KB (204 KB gzip) |
| **first-load JS of `/`** | 9 chunks, **626 KB (196 KB gzip)** + 29 KB CSS; ~490 KB of it is React / Next runtime |
| `pnpm exec next start`, idle | 238 MB PSS (next-server 125 MB + the pnpm wrapper 113 MB) |
| `output: "standalone"` | **41 MB** to deploy (35 MB `node_modules`); `node server.js` **93 MB** PSS idle, 118 MB after 100 requests |
| e2e (5 tests, headless shell, 2 workers) | 867-885 MB tree peak, 4.5 s |

The cheapest levers for generated apps, ordered by gain for effort. They are
recommendations; the bundle rules are owned elsewhere.

1. **`output: "standalone"`** in `next.config`: deploy 41 MB instead of
   661 MB, and run `node .next/standalone/server.js` (93 MB) instead of
   `pnpm exec next start` (238 MB, of which 113 MB is the pnpm wrapper). For
   `next start` in e2e, call `node_modules/.bin/next start` directly rather
   than through `pnpm exec`.
2. **Dependency pruning.**
   - `shadcn` (the CLI) and `@faker-js/faker` are runtime `dependencies` of
     the demo. The CLI belongs in `devDependencies` or nowhere, since the
     components are copied source.
   - The seed data could be a static JSON instead of faker at runtime.
   - `cn` duplicates the `clsx` / `tailwind-merge` helper that shadcn already
     writes into `lib/utils.ts`.
   - `lucide-react` is 39 MB installed. Only the imported icons ship, so this
     costs disk, not JS.
3. **Server components by default.**
   - 5 of the 14 components are `"use client"`.
   - Lists and read-only views (e.g. the poll list) can render on the server
     and pass data down. Only the vote form needs the client.
   - The framework floor is ~160 KB gzip, so the app's own share is small, but
     every client component adds to it.
4. **Build memory.**
   - `next build` peaks at ~1.4 GB with one worker per CPU.
   - On a small VPS, set `SHIPCREW_NODE_HEAP_MB=2048` (V8 heap cap through
     `NODE_OPTIONS`) and keep the reserve (below) at 2 GB or more.
   - Next's `experimental.cpus` also lowers the worker count, but that is an
     app-side setting.
5. **Playwright workers.**
   - One browser per worker.
   - `workers: process.env.CI ? 2 : undefined` stays within a small VPS's memory.

## 4. Resource governance (what the server does now)

- **`SHIPCREW_MAX_PARALLEL=auto`** (or `auto:<ceiling>`; a number keeps a fixed
  cap, default 4). Every scheduler tick recomputes the cap with this formula:

  ```
  cap = running + floor((MemAvailable - reserve) / per_session)
  cap = min(cap, CPUs, ceiling), never below max(1, running)
  ```

  - `MemAvailable` comes from `/proc/meminfo`. When the server runs inside a
    cgroup with `memory.max` (a container or a systemd scope), the cgroup's
    headroom is used when it is lower.
  - `running` is added back because the running sessions' memory is already
    used.
  - CPUs are the affinity mask.
  - `SHIPCREW_MEM_RESERVE_MB` defaults to 2048. It covers one build or e2e
    spike plus the stack.
  - `SHIPCREW_SESSION_MB` defaults to 350 when the developer runs on sdk and
    700 on native.
  - Memory pressure stops new starts and never stops a running session.
  - The gate is `resources.auto_max_parallel`, a pure function covered by
    `tests/shipcrew/test_resources.py`.
- **Idle sessions are stopped promptly.** Checked in the code and tests:
  - the reviewer after its verdict or skip;
  - the integrator after its outcome;
  - devops (ship) as soon as it replies, before the URL check;
  - the planner after the import;
  - the developer before an integrator takes its worktree, and at merge.
- **Parking the idle developer** (new, `SHIPCREW_PARK_IDLE_WORKERS`, default
  on). The developer is now also stopped once its PR is open and after each
  fix push, so a task waiting on CI and review holds no process: 300-700 MB
  and 3-7 % of a core saved per card in review.
  - The next message (CI logs, review feedback, a human's request) relaunches
    it with its conversation. This was checked live on both harnesses.
  - Set it to `0` to keep a native terminal open for watching.
- **Memory limits per session** are optional and not wired. The host launches
  the runners, so a per-session `systemd-run --scope` would have to wrap the
  runner launch upstream. What works today is capping the whole stack, and the
  auto gate reads that cap:

  ```bash
  systemd-run --user --scope -p MemoryMax=6G -p MemoryHigh=5500M \
      scripts/shipcrew_stack.sh start "$STATE" "$PORT"
  ```

  Checked: inside such a scope `resources.cgroup_limit_mb()` reports the
  headroom (3 GiB scope -> 3065 MB). Only the process's own cgroup
  `memory.max` is read, not its parents'.

### Other cheap wins, what already existed and what is new

| lever | status |
|---|---|
| headless worker harness (`SHIPCREW_WORKER_HARNESS`) | new, default `auto` |
| no shadcn MCP in developer sessions (265 MB each) | new, a side effect of sdk for the developer |
| auto capacity gate | new (`SHIPCREW_MAX_PARALLEL=auto`) |
| park the idle developer during CI and review | new (`SHIPCREW_PARK_IDLE_WORKERS=1`) |
| headless-only Chromium when present | new (stack script, `session_env`, CI template) |
| `NEXT_TELEMETRY_DISABLED=1` for agents | new in the stack script (the CI template already had it) |
| V8 heap cap for node | new, opt-in: `SHIPCREW_NODE_HEAP_MB` sets `NODE_OPTIONS=--max-old-space-size` |
| shared pnpm store | already so: pnpm's per-user store, `node_modules` hard-linked |
| worktree `node_modules` seeded by reflink / hard link | existed (`deps_seed`) |
| lower reviewer effort on small diffs | existed (`review_effort`: `low` for tiny, `medium` for small diffs) |
| reviewer skipped for tests-only diffs with green CI | existed (`review_skip_reason`) |
| Next build cache shared per mission | not done. `.next/cache` is 48 MB per worktree, and sharing it between parallel builds risks corruption. The CI template caches it per lockfile. |

## 5. VPS sizing

What costs memory:

- the stack: 0.5 GB fixed;
- claude-sdk workers: ~0.3-0.35 GB each;
- native qa / security / designer / scaffolder sessions: ~0.66 GB each, with
  their MCP server;
- spikes on top: a `next build` (~1.4 GB, less with fewer CPUs) or an e2e run
  (~0.65-0.9 GB with the headless shell).

The auto gate allows at most one session per CPU. A turn averages ~0.2 of a
core on sdk and ~0.45 on native, and builds are the real CPU load. The
subscription's shared rate limit is usually what binds first.

| VPS | `auto` cap (sdk developers) | memory alone would allow | notes |
|---|---|---|---|
| 2 vCPU / 4 GB | **2** | 4 | one build at a time; set `SHIPCREW_NODE_HEAP_MB=1536` |
| 4 vCPU / 8 GB | **4** | 15 | the sweet spot: 4 developers + a build + an e2e run fit; `auto:6` if builds are rare |
| 8 vCPU / 16 GB | **8** | 39 | the Claude subscription's rate limit, not RAM, is the limit |
| 16 vCPU / 32 GB | **16** | 86 | several missions in parallel |

With every worker on native (`SHIPCREW_WORKER_HARNESS=native`,
`SHIPCREW_SESSION_MB=700`), memory binds at 2 sessions on 4 GB and at 7 on
8 GB. At the same CPU cap, 4 native sessions leave ~1.4 GB less headroom for
builds and e2e runs than 4 sdk sessions, and use about twice the CPU.

Recommended unattended VPS env:

```bash
SHIPCREW_WORKER_HARNESS=auto        # default; `native` to watch terminals live
SHIPCREW_MAX_PARALLEL=auto          # or auto:<n> to also bound it
SHIPCREW_MEM_RESERVE_MB=2048        # default
SHIPCREW_PARK_IDLE_WORKERS=1        # default
SHIPCREW_NODE_HEAP_MB=2048          # optional, small boxes
# chrome-headless-shell on PATH (or in ~/.cache/ms-playwright) -> CHROMIUM_PATH
```
