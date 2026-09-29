# shipcrew P0 spike: omnigent as the base

**Verdict: GO.** Everything P0 had to prove works through omnigent's HTTP API
with the user's Claude Code subscription login and no API key:

- 3 `claude-native` sessions ran in parallel, each in its own git worktree.
- A Claude Task sub-agent showed up in `GET /v1/sessions/{id}/child_sessions`.
- An API-created child session was attached to the same parent.
- The parent's SSE stream delivered live updates.

Two pitfalls need handling, and both have fixes: folder trust (see
[Limits and pitfalls](#limits-and-pitfalls-the-orchestrator-must-handle), item 1)
and permission prompts (item 2). The integrator should build P1 and P2 on this base.

Spike branch `shipcrew-spike`. Driver: `scripts/shipcrew_spike.py`.
Stack script: `scripts/shipcrew_stack.sh`.

## Evidence

Machine: Arch Linux, 22 cores, 15 GiB RAM. Claude Code 2.1.284, tmux 3.7c.
Harness `claude-native` (agent `claude-native-ui`), `--permission-mode acceptEdits`.
Throwaway repo: `spike-sandbox/repo` (it contains only `calc.py` with `add()`).

### Final run (report9: 3 parallel sessions, then the parent/child phase, pre-trusted, warm host)

Session timings are in seconds from `POST /v1/sessions`.

| session | runner online | claude process up | turn done | result in its worktree | cost_usd* |
|---|---|---|---|---|---|
| p1 | 1.5 | 2.0 | 23.8 | `M calc.py` (sub) | 0.29 |
| p2 | 1.7 | 2.0 | 23.7 | `M calc.py` (mul) | 0.29 |
| p3 | 1.7 | 2.0 | 18.6 | `?? test_calc.py` | 0.24 |
| parent | 1.6 | 1.9 | 21.3 | Task sub-agent answer | 0.50 |

- Wall time for the 3 parallel sessions was **23.8 s**.
- **Sub-agent tree:** `child_sessions` returned 1 entry with `kind: "sub_agent"`,
  title `general-purpose:<agent id>`, the tool description, `busy`, and
  `last_message_preview`. The forwarder posts it about 10 s after the Task starts.
- **API child:** `POST /v1/sessions {parent_session_id, agent_id, initial_items}`
  returned 201. The child is listed in `child_sessions` right away, so a reviewer
  can be attached to a task's root session this way.
- **SSE:** the parent's `GET /v1/sessions/{id}/stream` carried
  - `session.created` (the child),
  - 7× `session.child_session.updated`,
  - `response.in_progress`, `response.output_text.delta`, `response.completed`,
    `session.status`, `session.usage` and `session.terminal.activity`.

  The board can drive its live updates from this stream alone.
- \* `total_cost_usd` is the cost Claude Code computes as if the turn were billed
  at API rates. The **subscription** pays for it, so this is not money spent: it
  shows how much of the usage quota a turn uses. About $0.25–0.30 per trivial
  turn is mostly the system prompt plus tool definitions.

### Earlier runs (in `spike-sandbox/report*.json`)

| run | setup | outcome |
|---|---|---|
| report2 | 3+1, cold, prompt posted right after create | all OK; claude up at **~28 s**; 81 s wall |
| report4 | 3+1, waiting for the runner tunnel before the first prompt | all OK; claude up at **~10 s**; 54 s wall |
| report5 | 6 parallel, no pretrust | **3/6 hung** on Claude's "Accessing workspace" trust dialog |
| report7, report8 | 6 parallel, `--pretrust` | **12/12 OK**; claude up at 20–27 s; 85–113 s wall |
| report9 | 3+1, pretrust, warm host | all OK; claude up at **2 s**; 24 s wall |

In report2 the prompt went out before the runner was up, which cost about 20 s:
the server waits a fixed 10 s (`_HOST_BOUND_RUNNER_CONNECT_GRACE_S`) and then
relaunches the cold runner. Waiting on `/v1/runners/{id}/status` first removed that.

**Startup time:** 2 s to create a session and start Claude on a warm host,
10–25 s cold or under a 6-way burst. A burst is mostly the runner zygote forking
and venv imports competing for CPU.

### Footprint per session

Measured from `/proc`, sampling every 1 s.

| part | RSS | notes |
|---|---|---|
| `claude` (the Claude Code process) | 340–370 MB | CPU 60–90 % of one core during a turn; 5–25 % idle (the TUI redraws) |
| runner (forked from the zygote) | ~280 MB | PSS is much lower, because pages are shared copy-on-write with the zygote |
| `claude_native.bridge` (MCP bridge) | ~84 MB | |
| tmux server | ~4 MB | |
| **total per session (PSS)** | **~420–470 MB** | mean CPU during a turn 40–85 %; idle 6–25 % |

Fixed cost of the stack: the server is about 170 MB PSS, the host about 145 MB,
and the zygote about 30 MB.

Capacity estimate: on this 15 GiB machine, with about 7 GiB free next to the
desktop, **~12–15 concurrent sessions** fit. RAM runs out first; CPU does not.
Sub-agents run inside the parent's `claude` process and add no process tree
of their own.

## Boot the stack (exact commands)

These are the commands the integrator should reuse. Paths are examples.
`STATE` must be outside the repo, or be gitignored.

```bash
WT=/home/jugggernault/Work/Projects/omnigent-wt/spike      # any omnigent checkout / worktree
STATE=/home/jugggernault/Work/Projects/omnigent-wt/spike-sandbox/state
PORT=16767                                                  # non-default; 6767 left alone

cd "$WT"
uv sync --extra all --group dev                             # once per fresh worktree
scripts/shipcrew_stack.sh start "$STATE" "$PORT"            # server + host, background, logs in $STATE/logs
scripts/shipcrew_stack.sh status "$STATE" "$PORT"           # {"status":"ok"}
curl -s localhost:$PORT/v1/hosts | jq '.hosts[0].configured_harnesses["claude-native"]'   # must be true

# the spike itself (throwaway repo with a commit on main)
mkdir -p ../spike-sandbox/repo && cd ../spike-sandbox/repo && git init -q -b main \
  && printf 'def add(a, b):\n    return a + b\n' > calc.py && git add . && git commit -qm init && cd "$WT"
uv run --no-sync python scripts/shipcrew_spike.py --base-url http://127.0.0.1:$PORT \
  --repo "$(realpath ../spike-sandbox/repo)" --pretrust --out ../spike-sandbox/report.json
#   --parallel N   --skip-child   --stagger S   --no-wait-runner   --keep

scripts/shipcrew_stack.sh stop "$STATE"                     # kills both process groups
```

What `shipcrew_stack.sh` does:

- Sets the same isolation env as omnidev: `OMNIGENT_DATA_DIR`,
  `OMNIGENT_CONFIG_HOME`, `OMNIGENT_DATABASE_URI` (SQLite in `$STATE`) and
  `OMNIGENT_URL`. The real `~/.omnigent` is never touched.
- Turns telemetry off in both places: the env vars `OMNIGENT_ANALYTICS=0`,
  `OMNIGENT_DISABLE_TELEMETRY=true` and `DO_NOT_TRACK=1`, and `telemetry: false`
  in `$STATE/config/config.yaml`.
- Unsets `CLAUDECODE`, `CLAUDE_*` and `ANTHROPIC_API_KEY` before it starts
  anything (see auth below).
- Starts the processes with `setsid`, so `stop` can kill each whole process group.

To add the web UI, run
`OMNIGENT_URL=http://127.0.0.1:$PORT pnpm --filter web dev --port <free> --strictPort`.
The spike did not need it.

**Why not omnidev?** `omnidev` is an interactive TUI supervisor. It opens a
browser, prefills example conversations and hot-reloads on edit. That is good
for a human at a terminal, but it does not fit an agent-driven, headless boot.
The script above reproduces omnidev's isolation with plain background
processes. Humans can still use `cd dev/omnidev && cargo run` for interactive
work: it allocates its own ports, so it does not collide with this stack.

### Creating a task session (what `POST /v1/shipcrew/tasks/{id}/start` should send)

```json
POST /v1/sessions
{"agent_id": "<id of agent named claude-native-ui>", "host_id": "<online host>",
 "workspace": "<mission.repo_path>", "git": {"branch_name": "shipcrew/<task-id>"},
 "title": "<task title>",
 "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
 "terminal_launch_args": ["--permission-mode", "acceptEdits"]}
```

1. omnigent creates the worktree at `<repo>-worktrees/<branch with / -> ->`.
2. Wait until `GET /v1/runners/{runner_id}/status` returns `online: true`.
3. Then `POST /v1/sessions/{id}/events` with a `message` item.
4. To stop, send `{"type": "stop_session"}` to `/v1/sessions/{id}/events`. This
   kills tmux, claude and the bridge.
5. The worktree is **kept** after stop. shipcrew must garbage-collect worktrees
   after merge.

## Limits and pitfalls the orchestrator must handle

1. **Folder trust race (must fix).** omnigent pre-accepts Claude's folder trust
   for each worktree just before launch. It does this with a read-modify-write
   of `~/.claude.json`, and concurrent `claude` starts overwrite each other's
   entry.
   - With 6 simultaneous starts, 3 lost their trust entry and hung on the
     "Accessing workspace" dialog.
   - The server turns that hang into a session error ("Claude Code is waiting
     for an answer in its terminal…"), so the failure is detectable and maps to
     `intervention`.
   - Trusting the parent `<repo>-worktrees/` directory is **not** inherited by
     the worktrees under it.
   - **Fix, tested 12/12:** before a batch starts, call
     `ensure_claude_workspace_trusted(_resolve_worktree_path(repo, branch))` for
     every worktree the batch will create. The scheduler should also stagger
     starts, or serialize them per host.
2. **Permissions (must decide).** `acceptEdits` auto-approves file edits only.
   - Any shell command (tests, `gh`, `git`) waits on a terminal approval. A
     headless session either blocks there or surfaces an elicitation.
   - Real tasks need one of two setups, and it is a policy choice to write in
     APPROVALS.md:
     - `--permission-mode bypassPermissions`, which is acceptable inside a
       disposable worktree;
     - or `--allowedTools` / a `settings.json` allowlist in each role bundle.
   - The spike prompts were edit-only, so this was not exercised end to end.
3. **Auth.** No API key is needed. `claude` in the terminal uses
   `~/.claude/.credentials.json`: the OAuth subscription, `billingType:
   stripe_subscription`, no `primaryApiKey`. The pitfalls:
   - A stray `ANTHROPIC_API_KEY` in the host env overrides the subscription.
     Claude Code then shows the "Detected a custom API key" prompt, and the
     turn hangs.
   - Starting the stack from inside a Claude Code session leaks `CLAUDECODE` /
     `CLAUDE_CODE_*` (child-session mode, a foreign session id) into every agent.
   - omnigent strips some of these variables itself. `shipcrew_stack.sh` also
     unsets all of them at the root.
   - Subscription rate limits are shared across all parallel sessions and the
     user's own Claude Code usage. The scheduler's concurrency cap is the real
     throttle.
   - The ToS question for automated subscription use stays open (PROPOSAL-v3
     risk list).
4. **tmux is required, bwrap is not.**
   - `claude-native` runs Claude inside a per-runner tmux server
     (`/tmp/omnigent-terminal-*/tmux.sock`), so tmux must be on the host PATH.
     That is fine on Arch and on the VPS.
   - bwrap is only omnigent's *inner* tool sandbox, for its own harnesses
     (`omnigent/inner/bwrap_sandbox.py`). The Claude process is not wrapped in
     it.
   - Isolation between tasks comes from git worktrees, not from a sandbox. If
     we want per-task FS isolation on the VPS, that is extra work (P3).
5. **Cold start.**
   - The first sessions after the host boots take 10–28 s. The zygote is cold
     and the venv imports are slow.
   - Later sessions start in about 2 s.
   - Keep the host running. Never post a prompt before the runner is online,
     because that costs a 10 s grace plus a runner relaunch.
6. **Idle cost.** An idle `claude` still uses 5–25 % of a core, because the
   TUI keeps redrawing. Stop sessions of tasks in `review`/`merged` instead of
   leaving them parked.
7. **Child-session latency.** A sub-agent appears in `child_sessions` about
   10 s after the Task tool starts. The board should expect a short delay.

## Housekeeping

- The throwaway repo, its worktrees (under `spike-sandbox/repo-worktrees/`),
  the reports and the stack state live under
  `/home/jugggernault/Work/Projects/omnigent-wt/spike-sandbox/`. It is
  disposable.
- Every process started by the spike was stopped with
  `scripts/shipcrew_stack.sh stop`.
