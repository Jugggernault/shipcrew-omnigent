"""shipcrew P0 spike: drive parallel claude-native sessions through the HTTP API.

Starts N sessions in parallel, each in its own git worktree of a throwaway
repo, sends each a tiny coding prompt, then runs one session whose Claude
spawns a Task sub-agent and checks ``GET /v1/sessions/{id}/child_sessions``
plus the parent's SSE stream. Samples CPU/RAM of each session's processes.

Needs a running server + host (see ``scripts/shipcrew_stack.sh``)::

    uv run --no-sync python scripts/shipcrew_spike.py \
        --base-url http://127.0.0.1:16767 --repo /path/to/throwaway/repo
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CLAUDE_NATIVE_AGENT = "claude-native-ui"
NATIVE_LABELS = {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"}
# acceptEdits: file edits auto-approved; shell commands still need approval.
LAUNCH_ARGS = ["--permission-mode", "acceptEdits"]
EDIT_ONLY = " Only edit files with your edit/write tools; do not run shell commands. Reply 'done'."
PROMPTS = [
    "In calc.py add a function sub(a, b) returning a - b." + EDIT_ONLY,
    "In calc.py add a function mul(a, b) returning a * b." + EDIT_ONLY,
    "Create test_calc.py with a pytest test for calc.add." + EDIT_ONLY,
]
CHILD_PROMPT = (
    "Use the Task tool to launch exactly one general-purpose subagent with this job: "
    "'Read calc.py and list the function names it defines.' "
    "Then reply with the subagent's answer. Do not run shell commands."
)
CLK_TCK = os.sysconf("SC_CLK_TCK")
# SSE events whose first arrival marks a startup milestone.
_TIMELINE = {
    "session.resource.created",
    "response.in_progress",
    "session.input.consumed",
    "response.output_text.delta",
    "response.completed",
    "response.failed",
    "session.created",
}


# ── HTTP helpers ─────────────────────────────────────────────────────────


def _msg(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def discover(client: httpx.Client) -> tuple[str, str]:
    """Return ``(host_id, claude-native agent_id)`` from the live server."""
    hosts = [h for h in client.get("/v1/hosts").json()["hosts"] if h["status"] == "online"]
    if not hosts:
        raise SystemExit("no online host: start `omnigent host` first")
    if hosts[0]["configured_harnesses"].get("claude-native") is not True:
        raise SystemExit(f"claude-native not ready on host: {hosts[0]['configured_harnesses']}")
    agents = client.get("/v1/agents", params={"limit": 200}).json()["data"]
    agent_id = next(a["id"] for a in agents if a["name"] == CLAUDE_NATIVE_AGENT)
    return hosts[0]["host_id"], agent_id


class SseRecorder(threading.Thread):
    """Record ``(t, type)`` of every event on one session's SSE stream."""

    def __init__(self, base_url: str, session_id: str) -> None:
        super().__init__(daemon=True)
        self.base_url, self.session_id = base_url, session_id
        self.events: list[tuple[float, str, dict[str, Any]]] = []
        self._stop = threading.Event()

    def run(self) -> None:
        url = f"{self.base_url}/v1/sessions/{self.session_id}/stream"
        try:
            with httpx.stream(
                "GET", url, headers={"Accept": "text/event-stream"}, timeout=None
            ) as resp:
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    self.events.append((time.monotonic(), str(data.get("type")), data))
        except httpx.HTTPError as exc:
            self.events.append((time.monotonic(), f"stream-error:{exc}", {}))

    def stop(self) -> None:
        self._stop.set()

    def types(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, t, _ in self.events:
            out[t] = out.get(t, 0) + 1
        return out


# ── process sampling (/proc) ─────────────────────────────────────────────


def _children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(d.name))
    return kids


def _cmdline(pid: int) -> str:
    try:
        return (
            Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        )
    except OSError:
        return ""


def session_pids(session_id: str, workspace: str | None) -> dict[str, list[int]]:
    """Processes of one session: tmux subtree (claude + bridge) and its runner."""
    kids = _children_map()
    out: dict[str, list[int]] = {"tmux": [], "claude": [], "bridge": [], "runner": []}
    for pid in [p for ps in kids.values() for p in ps]:
        cmd = _cmdline(pid)
        if cmd.startswith("tmux ") and session_id in cmd:
            out["tmux"].append(pid)
            stack = list(kids.get(pid, []))
            while stack:
                c = stack.pop()
                stack.extend(kids.get(c, []))
                ccmd = _cmdline(c)
                # claude's own argv mentions the bridge module (--mcp-config)
                if ccmd.startswith("claude "):
                    out["claude"].append(c)
                elif "claude_native.bridge" in ccmd:
                    out["bridge"].append(c)
        elif "omnigent.runner._zygote" in cmd and workspace:
            try:
                if os.readlink(f"/proc/{pid}/cwd") == workspace:
                    out["runner"].append(pid)
            except OSError:
                pass
    return out


def _proc_cpu_mem(pid: int) -> tuple[int, int, int]:
    """Return ``(cpu_ticks, rss_kb, pss_kb)`` for one pid (zeros if gone)."""
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(f[11]) + int(f[12])
        rss = pss = 0
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Rss:"):
                rss = int(line.split()[1])
            elif line.startswith("Pss:"):
                pss = int(line.split()[1])
        return ticks, rss, pss
    except (OSError, IndexError, ValueError):
        return 0, 0, 0


@dataclass
class Usage:
    samples: list[dict[str, float]] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {}
        keys = self.samples[0].keys()
        res: dict[str, float] = {}
        for k in keys:
            vals = [s[k] for s in self.samples]
            res[f"{k}_peak"] = round(max(vals), 1)
            res[f"{k}_mean"] = round(sum(vals) / len(vals), 1)
        return res


class Sampler(threading.Thread):
    """Sample CPU% (per pid delta) and RSS/PSS of each session every second."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.targets: dict[str, str | None] = {}
        self.usage: dict[str, Usage] = {}
        self.phase: dict[str, str] = {}
        self._stop = threading.Event()
        self._last: dict[int, tuple[float, int]] = {}

    def watch(self, session_id: str, workspace: str | None) -> None:
        self.targets[session_id] = workspace
        self.phase[session_id] = "busy"
        self.usage.setdefault(f"{session_id}:busy", Usage())
        self.usage.setdefault(f"{session_id}:idle", Usage())

    def run(self) -> None:
        while not self._stop.wait(1.0):
            now = time.monotonic()
            for sid, ws in list(self.targets.items()):
                groups = session_pids(sid, ws)
                sample: dict[str, float] = {}
                total_cpu = total_pss = 0.0
                for name, pids in groups.items():
                    cpu = rss = pss = 0.0
                    for pid in pids:
                        ticks, r, p = _proc_cpu_mem(pid)
                        prev = self._last.get(pid)
                        if prev is not None and now > prev[0]:
                            cpu += (ticks - prev[1]) / CLK_TCK / (now - prev[0]) * 100
                        self._last[pid] = (now, ticks)
                        rss += r / 1024
                        pss += p / 1024
                    sample[f"{name}_rss_mb"] = rss
                    sample[f"{name}_cpu_pct"] = cpu
                    total_cpu += cpu
                    total_pss += pss
                sample["total_pss_mb"] = total_pss
                sample["total_cpu_pct"] = total_cpu
                if any(groups.values()):
                    self.usage[f"{sid}:{self.phase[sid]}"].samples.append(sample)

    def stop(self) -> None:
        self._stop.set()


# ── session lifecycle ────────────────────────────────────────────────────


@dataclass
class Run:
    name: str
    prompt: str
    session_id: str = ""
    workspace: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    final_status: str = ""
    cost_usd: float | None = None
    sse: SseRecorder | None = None
    error: str | None = None


def create_session(
    client: httpx.Client, run: Run, host_id: str, agent_id: str, repo: str, tag: str
) -> None:
    body = {
        "agent_id": agent_id,
        "host_id": host_id,
        "workspace": repo,
        "git": {"branch_name": f"spike/{tag}-{run.name}"},
        "title": f"spike {run.name}",
        "labels": NATIVE_LABELS,
        "terminal_launch_args": LAUNCH_ARGS,
    }
    t0 = time.monotonic()
    resp = client.post("/v1/sessions", json=body)
    resp.raise_for_status()
    snap = resp.json()
    run.timings["create_http_s"] = time.monotonic() - t0
    run.session_id, run.workspace = snap["id"], snap.get("workspace")
    run.timings["t0"] = t0


def wait_runner_online(client: httpx.Client, run: Run, timeout_s: float = 60) -> None:
    """Block until the session's host-launched runner has registered its tunnel.

    Posting the first message before that makes the server wait a fixed
    10 s grace (``_HOST_BOUND_RUNNER_CONNECT_GRACE_S``) and, when a cold
    runner is slower than that, relaunch it and supersede the first one.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rid = client.get(f"/v1/sessions/{run.session_id}").json().get("runner_id")
        if rid and client.get(f"/v1/runners/{rid}/status").json().get("online"):
            run.timings["runner_online_s"] = time.monotonic() - run.timings["t0"]
            return
        time.sleep(0.25)


def drive(
    client: httpx.Client,
    run: Run,
    base_url: str,
    timeout_s: float,
    sampler: Sampler | None = None,
    wait_runner: bool = True,
) -> None:
    """Send the prompt (once the runner is up); wait for the turn to finish."""
    t0 = run.timings["t0"]
    run.sse = SseRecorder(base_url, run.session_id)
    run.sse.start()
    if wait_runner:
        wait_runner_online(client, run)
    client.post(f"/v1/sessions/{run.session_id}/events", json=_msg(run.prompt)).raise_for_status()
    deadline = t0 + timeout_s
    while time.monotonic() < deadline:
        if (
            "claude_proc_s" not in run.timings
            and session_pids(run.session_id, run.workspace)["claude"]
        ):
            run.timings["claude_proc_s"] = time.monotonic() - t0
        snap = client.get(f"/v1/sessions/{run.session_id}").json()
        errors = [it for it in snap["items"] if it["type"] == "error"]
        replied = any(
            it["type"] == "message" and it["data"].get("role") == "assistant"
            for it in snap["items"]
        )
        if (replied and snap["status"] != "running") or errors:
            run.timings["turn_done_s"] = time.monotonic() - t0
            run.final_status = snap["status"]
            if errors:
                run.error = str(errors[0]["data"].get("message", ""))[:300]
            run.cost_usd = snap.get("total_cost_usd")
            break
        time.sleep(0.5)
    else:
        run.final_status = "timeout"
    for ts, typ, _ in run.sse.events:
        key = f"sse_first:{typ}"
        if typ in _TIMELINE and key not in run.timings:
            run.timings[key] = ts - t0
    if sampler is not None:
        sampler.phase[run.session_id] = "idle"


def stop_session(client: httpx.Client, session_id: str) -> None:
    client.post(f"/v1/sessions/{session_id}/events", json={"type": "stop_session", "data": {}})


def git_changes(workspace: str | None) -> str:
    if not workspace:
        return ""
    out = subprocess.run(
        ["git", "-C", workspace, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


# ── main ─────────────────────────────────────────────────────────────────


def child_phases(
    client: httpx.Client,
    args: argparse.Namespace,
    host_id: str,
    agent_id: str,
    tag: str,
    sampler: Sampler,
    report: dict[str, Any],
) -> Run:
    """Run a parent whose Claude spawns a Task sub-agent, then attach an API child."""
    # Phase 2: parent session whose Claude spawns a Task sub-agent.
    parent = Run(name="parent", prompt=CHILD_PROMPT)
    create_session(client, parent, host_id, agent_id, args.repo, tag)
    sampler.watch(parent.session_id, parent.workspace)
    drive(client, parent, args.base_url, args.timeout, sampler, not args.no_wait_runner)
    children: list[dict[str, Any]] = []
    for _ in range(20):  # the forwarder posts the child shortly after meta.json lands
        children = (
            client.get(f"/v1/sessions/{parent.session_id}/child_sessions").json().get("data", [])
        )
        if children:
            break
        time.sleep(1)
    report["child_sessions"] = children

    # Phase 3: an API-created child (how shipcrew would attach a reviewer).
    try:
        resp = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "parent_session_id": parent.session_id,
                "title": "reviewer:spike",
                "initial_items": [_msg("Say hi.")],
            },
        )
        report["api_child_create"] = {"status": resp.status_code, "body": resp.json()}
        if resp.is_success:
            listed = client.get(f"/v1/sessions/{parent.session_id}/child_sessions").json()["data"]
            report["api_child_listed"] = any(c.get("id") == resp.json()["id"] for c in listed)
    except (httpx.HTTPError, ValueError) as exc:
        report["api_child_create"] = {"error": str(exc)}

    return parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:16767")
    ap.add_argument("--repo", required=True, help="throwaway git repo (worktrees branch off it)")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--idle-seconds", type=float, default=10, help="idle sampling after turns")
    ap.add_argument(
        "--stagger",
        type=float,
        default=0.0,
        help="seconds between session creates (0 = all at once)",
    )
    ap.add_argument(
        "--no-wait-runner",
        action="store_true",
        help="post the prompt right after create instead of waiting for the runner tunnel",
    )
    ap.add_argument("--parallel", type=int, default=3, help="phase-1 session count")
    ap.add_argument("--skip-child", action="store_true", help="skip the sub-agent phases")
    ap.add_argument("--keep", action="store_true", help="leave sessions running")
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args()

    tag = uuid.uuid4().hex[:6]
    client = httpx.Client(base_url=args.base_url, timeout=90)
    host_id, agent_id = discover(client)
    report: dict[str, Any] = {
        "host_id": host_id,
        "agent_id": agent_id,
        "tag": tag,
        "stagger_s": args.stagger,
        "wait_runner": not args.no_wait_runner,
    }
    sampler = Sampler()
    sampler.start()

    # Phase 1: N parallel sessions, one worktree each.
    runs = [Run(name=f"p{i + 1}", prompt=PROMPTS[i % len(PROMPTS)]) for i in range(args.parallel)]
    t_all = time.monotonic()
    with ThreadPoolExecutor(len(runs)) as pool:
        futures = []
        for i, r in enumerate(runs):
            if i and args.stagger:
                time.sleep(args.stagger)
            create_session(client, r, host_id, agent_id, args.repo, tag)
            sampler.watch(r.session_id, r.workspace)
            futures.append(
                pool.submit(
                    drive,
                    client,
                    r,
                    args.base_url,
                    args.timeout,
                    sampler,
                    not args.no_wait_runner,
                )
            )
        for f in futures:
            f.result()
    report["parallel_wall_s"] = round(time.monotonic() - t_all, 1)

    parent = (
        None
        if args.skip_child
        else child_phases(client, args, host_id, agent_id, tag, sampler, report)
    )

    # Idle footprint once every turn is over.
    time.sleep(args.idle_seconds)
    sampler.stop()

    all_runs = [*runs, *([parent] if parent else [])]
    for r in all_runs:
        if r.sse:
            r.sse.stop()
    report["runs"] = [
        {
            "name": r.name,
            "session_id": r.session_id,
            "workspace": r.workspace,
            "final_status": r.final_status,
            "error": r.error,
            "cost_usd": r.cost_usd,
            "timings_s": {k: round(v, 2) for k, v in r.timings.items() if k != "t0"},
            "git_status": git_changes(r.workspace),
            "sse_event_types": r.sse.types() if r.sse else {},
            "busy": sampler.usage[f"{r.session_id}:busy"].summary(),
            "idle": sampler.usage[f"{r.session_id}:idle"].summary(),
        }
        for r in all_runs
    ]
    parent_sse = parent.sse.types() if parent and parent.sse else {}
    report["parent_saw_session_created"] = parent_sse.get("session.created", 0) > 0

    if not args.keep:
        for r in all_runs:
            stop_session(client, r.session_id)
        if isinstance(report.get("api_child_create", {}).get("body"), dict):
            cid = report["api_child_create"]["body"].get("id")
            if cid:
                stop_session(client, cid)

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
