# shipcrew on omnigent: status

Branch `shipcrew` of this fork. Design: `shipcrew/PROPOSAL-v3.md`. Role bundles
live in the separate `shipcrew` repo (`agents/`, branch `v0.2`). Round 2 (PR
loop, planner import, issue sync, worker permissions, board UI) is merged. Last
live end-to-end run: 2026-09-29.

![Board after the e2e: both cards merged](board-merged.png)
![Intervention card: a guardrail ask during a CI fix turn](intervention.png)
![Drawer of a merged card: PR, CI fix 1/3, reviewer findings](drawer-merged.png)
![Card held for a human merge approval (APPROVALS.md)](board-approval.png)
![Drawer with the Approve merge action](drawer-approval.png)

## Round 4: verify roles and speed (2026-09-29)

- **Verify roles write tests only** (`omnigent/shipcrew/policies.py`
  `test_writes_only`, registered): qa and security may write `test/**`,
  `tests/**`, `e2e/**` (top level), `**/__tests__/**`, `**/__snapshots__/**`,
  `**/*.test.*`, `**/*.spec.*` and their report (`.shipcrew/qa.json`,
  `.shipcrew/security.md`); any other write (write tools and shell targets) is
  DENY. `owned_paths` gained `extra_free_paths` (the report file) and still asks
  for a test outside the task. The PR loop re-checks the diff: a non-test file
  in a verify PR needs approval.
- **Failures become fix tasks** (`omnigent/shipcrew/verify.py`, hooked at the
  PR loop's first step): verify `PASS` with no commits -> card `merged`
  (nothing to merge); `PASS` with tests -> normal PR; `FAIL` or a
  blocker/major finding -> ONE developer task `Fix: <title>` (findings with
  file:line and repro, the report, "add a regression test"; owned paths = the
  files named, fallback the verify task's; plan key `fix:<id>:<n>`), Ready. The
  verify card goes back to Ready with `depends_on += fix`, its worktree and
  branch removed so it re-runs on the merged fix. Tests it wrote stay on local
  branch `shipcrew-tests/<id8>-<n>`, named in the fix body. After 2 fix cycles:
  Intervention `verification still failing after 2 fix cycles: <reason>` (the
  session sync leaves that hold alone until a human talks to the agent).
- **Parallel starts** (`service.schedule_ready`): gates are evaluated in order
  against a view where each picked card already runs, then the picked cards
  start with `asyncio.gather`. `git worktree add` is serialized per repository
  (`OmnigentSessionService._worktree_lock`).
- **node_modules seeding** (`omnigent/shipcrew/deps_seed.py`): a new task
  worktree whose lockfile is byte-identical to the main checkout's (and whose
  `node_modules` is gitignored) gets a reflink copy, else a hardlink copy
  (`cp -al`, symlinks kept, so pnpm's layout works). Files a package manager
  rewrites in place (`.package-lock.json`, `.modules.yaml`, ...) are made
  private, tool caches are dropped. Any failure: silently nothing.
- **Cheaper reviews** (`omnigent/shipcrew/review_policy.py`): with CI green, a
  tests-only or docs-only diff (`.md/.rst/.adoc`, not `AGENTS.md`, `CLAUDE.md`,
  `DESIGN.md`, skills, `.shipcrew/`, `.github/`) skips the reviewer
  (`review.summary = "review skipped: ..."`, `SHIPCREW_REVIEW_SKIP=0` turns it
  off). Otherwise the reviewer session gets `reasoning_effort` `low` (<= 40
  changed lines) or `medium` (< 150), passed as session metadata, which
  claude-native turns into `--effort` (`SHIPCREW_REVIEW_EFFORT_TINY/SMALL`).
- **CI template**: pnpm or npm picked from the lockfile, setup-node cache,
  `--prefer-offline` installs, `.next/cache`, lint + typecheck + test in one
  parallel step (each log grouped, fails if any failed), `cancel-in-progress`.
- **Bundles** (shipcrew `v3-qaspeed`): planner (fewest, largest tasks along
  module boundaries; small PRD = at most 5 tasks + one final qa verify task with
  the security checklist; verify roles never implement), COMMON speed rules
  (unit tests on route handlers with the fake DB, one command for the whole
  suite, e2e and dev servers only when needed, batched reads, no polling,
  install once), scaffolder on pnpm with `packageManager`.

Measured on this machine (ext4, so the hardlink path; Next 15 + React 19 +
vitest + eslint + faker, 391 MB, ~13k files):

| | fresh worktree install | seed |
|---|---|---|
| npm (`npm ci --prefer-offline`, warm cache) | 5.1 s | 0.34 s |
| pnpm (`pnpm install --frozen-lockfile --prefer-offline`, warm store) | 0.68 s | 0.21 s |
| cold install (empty worktree, network) | npm 23.5 s, pnpm 33.4 s | 0.2-0.3 s |

The seed also saves the agent's install turn. Parallel starts: N starts take
the time of the slowest instead of the sum (test: 2 x 0.3 s starts in < 0.6 s).
CI: the three check scripts run side by side (test: 3 x 0.5 s in < 1.4 s).

## Round 3: run everything, MCP scoping (2026-09-29)

- **Run all tasks** (`omnigent/shipcrew/router.py`, `service.py`, `store.py`):
  `POST /missions/{id}/start-all` -> `{mission, started: [task ids]}` moves
  every backlog task that is not human-assigned to Ready in one transaction and
  emits `task.updated` for each. It only queues: the four scheduler gates
  (deps, capacity, owned paths, budget) still decide what starts. Idempotent,
  same auth and per-owner ACL (404 for someone else's mission).
- **Auto run** (`sc0003ar`, `shipcrew_missions.auto_run`, default false):
  `PATCH /missions/{id} {auto_run}` (emits `mission.updated`); the Mission
  payload carries `auto_run`. With it on, a plan import moves the imported
  tasks (only those) to Ready right after `plan.status=imported`, so the same
  scheduler tick starts them.
- **Mission commands** (`omnigent/shipcrew/commands.py`):
  `POST /missions/{id}/command {text}` maps a short order to one action with
  fixed rules, no LLM: `run all / start / lance tout / démarre` -> start-all,
  `plan / planifie` -> planner on the repo PRD, `sync / synchronise` -> GitHub
  sync, `stop all / arrête tout` -> stop every running task. Accents and case
  are ignored. The text must be one verb plus filler words, so a negation
  ("don't run all", "ne lance pas") or two verbs is refused: 400 with the
  supported list. The response has `intent`, `message` (the board toast),
  `mission` and `started` / `stopped` / `sync`. A real LLM orchestrator chat
  (free text to a mission-scoped `shipcrew` session) is a later step; the
  orchestrator bundle documents the command table.
- **Board** (English, like the rest of the board): mission header gets an
  "Ask the crew…" box (toast with the server's message, or its error), a
  primary "Run all tasks" button with the runnable backlog count (disabled at
  0) and a "Run N tasks?" confirm, and the Plan from PRD dialog a "Run
  automatically after planning" checkbox bound to `auto_run`
  (`web/src/board/MissionRun.tsx`, `MissionPlan.tsx`).
- **MCP scoped per role** (`omnigent/shipcrew/launch_args.py`): sessions used
  to inherit every MCP server of the host user (claude.ai connectors such as
  Gmail, Canva, Notion, Vercel, Figma, plus `~/.claude.json` servers and plugin
  servers). Bundles now declare `executor.config.strict_mcp_config: true` and
  optionally `mcp_config` (JSON `{"mcpServers": ...}`):
  - claude-native: `--strict-mcp-config --mcp-config <json>` launch args,
    next to the `allowed_tools` mapping in
    `_derive_terminal_launch_args_from_spec`. The bridge still appends its own
    `--mcp-config` for omnigent's relay; Claude merges repeated
    `--mcp-config` flags and strict mode keeps all of them.
  - claude-sdk: `HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG=1` (workflow spawn env)
    -> the SDK executor adds `--strict-mcp-config` to `extra_args`; the
    in-process `omnigent` server is passed through `--mcp-config` and stays.
  - Roles: developer / scaffolder / designer = shadcn, qa = chrome-devtools
    (headless, isolated, `${CHROMIUM_PATH:-/usr/bin/chromium}`, expanded by
    Claude), everyone else none (devops uses the vercel CLI).
  - Verified live (port 16780, own state dir, fake gh): the developer's
    `claude` argv had `--strict-mcp-config`, the shadcn config and the relay
    config; the agent saw exactly two MCP servers (`omnigent`, `shadcn`, deferred
    tools included) and a `sys_os_read` call through the relay worked. A plain
    `claude -p` on this machine lists 28 servers (claude.ai, plugins, user).

## Round 2 end-to-end (verified live, 2026-09-29)

Real `omnigent server` + `host` (`scripts/shipcrew_stack.sh`, port 16772, own
state dir), claude-native and claude-sdk sessions on the Claude subscription
login, the bundles of `shipcrew` v0.2, and `SHIPCREW_GH` pointing at the fake
gh (`scripts/shipcrew-fake-gh`). The mission repo was a throwaway CommonJS
project (`src/sum.js`, node:test tests, `"test": "node --test test/"`) whose
`origin` is a local bare repository. Nothing reached GitHub.

1. **Mission** created with `repo_url=https://github.com/shipcrew/e2e-math` so
   the issue sync runs (against the fake).
2. **`POST /missions/{id}/plan`** with a short PRD (two features, #2 depends on
   #1). The planner session wrote `.shipcrew/plan.json`; the server imported two
   developer tasks (`sumAll`, `average`), mapped `T01 -> task id` in
   `depends_on`, and set `plan.status=imported`, `imported_count=2`. The next
   sync opened issues #1 and #2 with labels `shipcrew` and `role:developer`.
3. **Both cards PATCHed to Ready.** `sumAll` started on branch
   `shipcrew/ce64fcc7-sumall` in its own worktree; `average` waited with
   "waiting on dependencies: sumAll".
4. **Developer, no prompts.** `npm test`, `git add` and `git commit` ran under
   the builder allowlist with no approval card; it ended with `PASS`.
5. **Push + draft PR.** The loop pushed to the bare remote and opened draft PR
   #3 ("Closes #1", acceptance checklist).
6. **CI red -> fix loop.** The fake CI ran `npm test` plus a hidden contract
   check (`sumAll("1,2")` must throw `TypeError("sumAll expects an array")`).
   The run was red; the loop sent the failing log to the developer as a new
   turn (`ci_attempts=1`). During that turn a chained `git fetch ...; ls
   .github/workflows` hit `shipcrew_workflows_approval`: the card went to
   **Intervention** with a "Guardrail ask" badge; it was approved through the
   elicitation resolve endpoint. The developer fixed the code and committed; the
   loop pushed the fix, CI went green.
7. **Reviewer child.** A `Review: sumAll` child session (parent = the root
   session, same worktree) got the diff snapshot and the contract, answered
   `APPROVE` with a fenced findings block (one `minor` finding, shown in the
   drawer). Its `npm test` asked on the read-only allowlist at the time; the
   reviewer now has a test-runner allowlist (shipcrew `7f10767`).
8. **Merge.** `gh pr ready`, `update-branch`, `merge --squash --delete-branch`:
   bare `main` got `sumAll (#3)`, issue #1 closed, the worktree and the local
   branch were removed, local `main` fast-forwarded, all sessions stopped.
9. **Task 2 started in the same tick** from the merged code, went through CI
   (green first time) and review (`APPROVE`, no findings), then hit the repo's
   `APPROVALS.md` rule `src/stats.js`: `needs_human_approval=true`, card in
   **Intervention** ("Needs approval"). "Approve merge" in the drawer called
   `POST /tasks/{id}/approve`; the loop squash-merged `average (#4)` within one
   tick. Final `npm test` on `main`: 6 pass, 0 fail. Both issues closed, both
   PRs merged, no worktree left.

## What works (verified by tests, and live where noted)

- **PR loop** (`omnigent/shipcrew/pr_loop.py`, live): push -> draft PR -> CI
  (3 fix turns, then Intervention) -> reviewer child (3rd `CHANGES` ->
  Intervention) -> `APPROVALS.md` gate (read from `origin/<base>`, defaults
  when absent) -> serialized squash merge (integrator child on conflict) ->
  cleanup. All state is re-read from the DB, the worktree and gh on each tick,
  so a restart resumes. `POST /tasks/{id}/approve` and
  `POST /tasks/{id}/request-changes` (tests; approve also live).
- **Planner import** (`omnigent/shipcrew/planner.py`, live): pydantic
  validation of `plan.json` (unique keys, known `depends_on`, no cycles, with a
  readable `plan.error`), one-transaction import keyed by plan key (re-import
  updates, never duplicates), `task.updated` per task then `mission.updated`.
- **GitHub issue sync** (`omnigent/shipcrew/issue_sync.py`, live against the
  fake): issue per task (labels, checklist), PR merged outside shipcrew ->
  merged, closed issue -> blocked, assignee or `shipcrew:human` -> human.
  Rate-limited per mission (`SHIPCREW_SYNC_INTERVAL_S`), no-op without
  `repo_url` or when gh is not logged in. `POST /missions/{id}/sync` returns
  the Mission plus an additive `sync` report that the board shows in its toast.
- **Worker permissions** (live, round 2 perms run and this e2e): per-role shell
  allowlists, owned-path guardrail, push guard on `shipcrew/<id8>-<slug>`, no
  `bypassPermissions`. See the shipcrew repo `agents/README.md`.
- **One branch scheme**: `omnigent/shipcrew/branches.py`
  (`shipcrew/<id[:8]>-<slug>`), stored on `Task.branch` at first start, used by
  worktrees, the loop, the issue sync and the guardrails.
- **One gh seam**: every call goes through `omnigent/shipcrew/gh.py` `_run`
  (`SHIPCREW_GH` via `tools.resolve`, bounded timeouts, `GhError`).
- **One fake gh**: `scripts/shipcrew_fake_gh.py` (+ `scripts/shipcrew-fake-gh`)
  backs the PR-loop tests, the issue-sync tests and the e2e: real pushes and
  squash merges into a local bare repo, CI = a shell command per head SHA,
  issues/labels, `--repo` on every command, `authenticated: false` for the
  not-logged-in path.
- **Board** (`web/src/board/`, live): Plan from PRD dialog and plan status,
  Sync GitHub, card badges (branch, issue, PR, CI fix n/3, review verdict,
  Needs approval, intervention reason), drawer sections (Approval, Pull request,
  Review findings, Request changes), `mission.updated` over SSE, six columns
  without horizontal scroll at 1600 px.
- **Schema**: Alembic lineage `sc0001 -> sc0002pr (PR loop) -> sc0002p (plan +
  issue sync) -> sc0003ar (mission auto_run)`, single head.
- Round 1 behaviour (board, drawer tree, 4 scheduler gates, session state ->
  card, folder pre-trust, ACL per owner) is unchanged.

## Round 2 review fixes (2026-09-29)

An independent review of the round-2 diff led to these fixes. Each one has a
regression test.

- **Allowlist bypasses closed** (`policies.py`):
  - A refused option also matches its getopt/git abbreviations. `git fetch
    --upload-p=<cmd>` ran a local command on a local remote, and `git reset
    --har` / `sort --out=` got past `!--hard` / `!--output*`.
  - A refused one-letter option also matches short-option clusters and stuck
    values (`git rebase -xcmd`, `sort -uo f`).
  - `$VAR` / `${..}` / `$'..'` outside single quotes, brace expansion and a
    glob in an option name make a command unanalyzable: the allowlist asks
    and the push guard denies (`CI=--output=f; git diff $CI` used to pass).
  - `git checkout <tree-ish> <path>` counts as a write.
- **Merge gates** (`pr_loop.py`, `gh.py`):
  - `gh pr merge --match-head-commit <reviewed sha>`.
  - Only a verified clean merge of main (the parents and the `git merge-tree`
    result match) keeps the review and the human approval. A foreign push to
    the PR branch, or the integrator's conflict resolution, is reviewed and
    approved again.
  - The policy diff uses `-z --no-renames` (quoted names and renames out of a
    gated path used to slip through).
  - The default approval rules always apply; `APPROVALS.md` only adds rules.
  - Changed files outside `owned_paths` need approval (the server-side
    backstop for anything the guardrail cannot see, such as code run by
    tests).
  - `APPROVE` with a `blocker` finding counts as `CHANGES`.
  - With workflow files present, "no checks reported" waits up to 180 s for
    CI to report before counting as green.
  - A PR head that is not a SHA is refused before it reaches git.
- **Untrusted CI output**: check names and failed logs reach the developer
  inside an `<untrusted-ci-output>` block. Its fence is longer than any
  backtick run in the text, and the block says to treat it as data.
- **Approve** releases only a loop hold (409 during CI or review, and for a
  guardrail ask), and clears `needs_human_approval`.
- **Leaks**: a blocked card stops its in-flight reviewer and integrator. A PR
  merged outside the loop gets the loop's cleanup (sessions, worktree,
  branch). Cleanup drops the stale `origin/<branch>` ref.
- **plan.json**:
  - `role` must be a task role (not `shipcrew`, `planner` or `reviewer`).
  - `owned_paths` must be repo-relative, with no `..`, backslash or control
    character.
  - Sizes are capped (200 tasks).
  - A re-import leaves tasks past Ready unchanged.
- **Board**:
  - The approval section only shows while the card is held.
  - The approval badge hides on merged cards.
  - A leftover `changes` verdict no longer labels a later guardrail ask as
    "review rounds exhausted".
  - Other loop holds get a "Held by the PR loop" label.
- **Accepted risks** are documented in the agents README: test runners and
  `npm run` execute arbitrary code, `npx` downloads fixed package names, and
  `kill`/`pkill` and `curl localhost` are allowed for qa and security.

## What is left / known issues

- **Command box is rule-based.** Free text to an LLM orchestrator session is
  not wired; `plan` from the box always uses the repo's `.shipcrew/prd.md`.
- **`stop all` stops Running / Intervention cards only**; Review cards and
  loop children (reviewer, integrator, planner) keep going.
- **MCP scoping trade-offs**: the designer lost the optional open-pencil brand
  board and security lost chrome-devtools (it uses curl and Playwright). Both
  are one line in `MCP_SERVERS` / the config marker to change back. The MCP
  servers run through `npx -y <pkg>@latest` (network on first use).

- **A reviewer or integrator ask does not move the card.** While a loop child
  waits on an approval card the task stays in Review (contract: Intervention);
  the ask only shows in the sidebar ("Needs response") and the Inbox. The
  reviewer test-runner allowlist removes the common case.
- **`workflows_approval` false positive**: a chain such as `git fetch -q
  origin; ls .github/workflows` asks, although it only reads.
- **Loop children never time out**: a reviewer, integrator or planner that
  never answers keeps the card in Review (or `plan.status=running`).
- **Transient status**: for one tick between review and the approval hold the
  card showed Running with `needs_human_approval=true`.
- **Declined asks**: an explicit `decline` interrupts the turn; the idle
  session then maps to Review, which the loop reads as "developer finished"
  (no PASS line -> blocked before a PR, or re-review after one). `cancel`
  refuses one call and lets the agent continue.
- **Drawer tree in headless Chromium**: the reviewer child node is in the React
  Flow graph (`child_sessions` returns it) but stays `visibility: hidden` in the
  DevTools browser, so the screenshots show an empty tree. Not reproduced in a
  normal browser yet.
- **Blocked filter** counts Ready cards waiting on dependencies and loop holds,
  because both carry `blocked_reason`.
- **Loop holds use a capacity slot** (intervention is an active status).
- **A card dragged to Merged by hand** skips the loop cleanup (worktree stays,
  PR stays open). A PR merged on GitHub and found by the sync is now cleaned
  up. The hand drag is not, because removing the worktree would drop commits
  that were never pushed.
- **Review snapshots** (`.git/shipcrew/reviews/*.diff`) are never pruned.
- **Orchestrator full mode** sub-agents get no owned-paths contract (the policy
  abstains there).
- **Local filesystem assumption**: `plan.json`, worktrees and git/gh calls run
  on the server's filesystem, like folder pre-trust.
- **Real gh never exercised**: the fake mimics `--json` shapes and exit codes;
  a first run against a real repo should watch `pr checks` and `pr create`.
- **Web UI changes need a server restart** (index.html is cached).

## How to run

```bash
cd /home/jugggernault/Work/Projects/omnigent
uv sync --extra all --group dev
pnpm install --frozen-lockfile --filter web && pnpm --filter web build   # serves /board

STATE=/tmp/shipcrew-state PORT=16767                    # non-default port
scripts/shipcrew_stack.sh start "$STATE" "$PORT"          # server + host, telemetry off
# optional: SHIPCREW_MAX_PARALLEL=4 SHIPCREW_MAX_USD=100 SHIPCREW_POLL_INTERVAL_S=5
#           SHIPCREW_AGENTS_DIR=... SHIPCREW_SCHEDULER=0 (disable the loop)
xdg-open "http://127.0.0.1:$PORT/board"
scripts/shipcrew_stack.sh stop "$STATE"
```

Local PR-loop e2e with the fake gh (never touches GitHub):

```bash
E=/tmp/shipcrew-e2e; mkdir -p $E
git init -q --bare -b main $E/origin.git        # the "GitHub" repo
# clone it to $E/repo, add a tiny npm project + APPROVALS.md, push main
scripts/shipcrew_fake_gh.py init --state $E/gh.json --remote $E/origin.git \
    --repo shipcrew/e2e-math --ci-command 'npm test'
export SHIPCREW_GH=$PWD/scripts/shipcrew-fake-gh SHIPCREW_FAKE_GH_STATE=$E/gh.json
export SHIPCREW_SYNC_INTERVAL_S=10               # optional, default 60
scripts/shipcrew_stack.sh start $E/state 16772
# board: new mission (repo path $E/repo, repo URL https://github.com/shipcrew/e2e-math),
# Plan from PRD, drag the imported cards to Ready, watch them merge.
scripts/shipcrew-fake-gh pr list --state all     # or read $E/gh.json "calls"
scripts/shipcrew_stack.sh stop $E/state 16772
```

New settings: `SHIPCREW_PR_LOOP` (default on), `SHIPCREW_PR_BASE` (default
`main`), `SHIPCREW_SYNC_INTERVAL_S` (default 60, min 5), `SHIPCREW_GH`.

API contract: `/v1/shipcrew/*`, with the same auth as the other `/v1` routes.
It is hidden from OpenAPI so the upstream `openapi.json` does not drift. The
server side is `omnigent/shipcrew/router.py` and the client side is
`web/src/board/api.ts`.

## Checks (round 3, 2026-09-29)

- `ruff check` / `ruff format --check` on `omnigent/shipcrew`, `tests/shipcrew`
  and the four touched upstream files; `pyrefly check omnigent/shipcrew` 0
  errors (the touched upstream files add none).
- `pytest tests/shipcrew tests/server/test_shipcrew_mount.py
  tests/inner/test_claude_sdk_executor.py tests/inner/test_claude_sdk_harness.py
  tests/server/routes/test_sessions_yolo_launch_args.py
  tests/runner/test_app_claude_native_launch_args.py
  tests/runtime/test_claude_sdk_spawn_env.py tests/runtime/test_spawn_env_cwd.py
  tests/policies/test_registry.py tests/server/routes/test_policy_registry.py`:
  726 passed (new: `test_run_all.py`, `test_launch_args.py`).
- Web: `pnpm lint`, `pnpm type-check`, `pnpm build`, prettier on the touched
  files, `vitest run src/board src/pages/BoardPage.test.tsx src/shell/Sidebar`:
  454 passed.
- Bundles: `build_agents.py --check`, `validate_agents.py` from this worktree:
  10 bundles valid, 117 guardrail cases each, MCP set asserted per bundle.

## Checks (integration, 2026-09-29)

- **Backend:** `ruff check` / `ruff format --check` on `omnigent/shipcrew`,
  `tests/shipcrew`, `scripts/shipcrew_fake_gh.py`; `pyrefly check
  omnigent/shipcrew` (0 errors); `pytest tests/shipcrew
  tests/server/test_shipcrew_mount.py` plus the upstream tests next to the
  touched files (`tests/policies/test_registry.py`,
  `tests/server/routes/test_policy_registry.py`,
  `tests/server/routes/test_sessions_yolo_launch_args.py`): 416 passed after
  the review fixes (376 before);
  `pre-commit run --files <changed>`.
- **Web:** `pnpm lint`, `pnpm type-check`, `pnpm build`, and the full
  `vitest run` with `NODE_OPTIONS=--no-experimental-webstorage LANG=C.UTF-8`:
  9582 passed, 1 skipped with `--maxWorkers=4`. With the default worker count
  on this machine 5-7 upstream tests (Sidebar*, streamdownCodeHighlight) time
  out under load and pass when rerun alone.
- **Bundles:** `python3 scripts/build_agents.py --check`, and `uv run python
  <shipcrew>/scripts/validate_agents.py` run from this checkout: 10 bundles
  valid, 117 guardrail cases each (7 bypass cases added by the review).

## Upstream footprint (rebase surface)

- `omnigent/server/app.py`: 4 lines that call `mount_shipcrew(...)`.
- `pyproject.toml`: 1 package-data line.
- `web/src/App.tsx`: a lazy `/board` route (+5 lines).
- `web/src/shell/Sidebar.tsx`: the Board nav item (+3 lines).
- `omnigent/policies/builtins/__init__.py`: registers `omnigent.shipcrew.policies`
  in `BUILTIN_POLICY_MODULES` (+2 lines), so uploaded bundles may use the
  shipcrew guardrails.
- `omnigent/server/routes/_sessions/helpers.py`: claude-native
  `executor.config.allowed_tools` becomes the `--allowedTools` launch flag
  (+8 lines in `_derive_terminal_launch_args_from_spec`), and
  `strict_mcp_config` / `mcp_config` become `--strict-mcp-config` /
  `--mcp-config` (+4 lines, logic in `omnigent/shipcrew/launch_args.py`).
- `omnigent/runtime/workflow.py` (+5), `omnigent/inner/claude_sdk_harness.py`
  (+3), `omnigent/inner/claude_sdk_executor.py` (+10): the claude-sdk
  `strict_mcp_config` flag (env `HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG`, then
  `extra_args["strict-mcp-config"]`).

Everything else is new: `omnigent/shipcrew/`, its own Alembic lineage
(`shipcrew_alembic_version`), `web/src/board/`, `web/src/pages/BoardPage*`,
`scripts/shipcrew_*`, `SPIKE.md` and `docs/shipcrew/`.
