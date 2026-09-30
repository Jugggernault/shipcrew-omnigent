"""Resource governance: the auto capacity gate, worker harness selection, parking."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.shipcrew import resources
from omnigent.shipcrew.approvals import APP_STATE_HOOK, notify_accepted_relay_ask
from omnigent.shipcrew.harness import (
    NATIVE,
    SDK,
    SDK_DEFAULT_ROLES,
    WorkerHarness,
    apply_worker_harness,
    bundle_harness,
    parse_worker_harness,
)
from omnigent.shipcrew.resources import auto_max_parallel, mem_available_mb, parse_max_parallel
from omnigent.shipcrew.scheduler import ShipcrewScheduler
from omnigent.shipcrew.service import ShipcrewService
from omnigent.shipcrew.sessions import bundle_agent_dir, inject_task_contract
from omnigent.shipcrew.settings import ShipcrewSettings

from .conftest import FakeSessions

P = "/v1/shipcrew"

# The shape of a generated claude-native worker bundle (agents/developer/config.yaml).
NATIVE_CONFIG = """\
spec_version: 1
name: developer
description: test developer
executor:
  type: omnigent
  config:
    harness: claude-native
    # a comment that stays
    permission_mode: default
    allowed_tools: Bash,Edit,Write,Read,Glob,Grep,mcp__omnigent,mcp__shadcn
    # >>> shipcrew-mcp: shadcn
    strict_mcp_config: true
    setting_sources: project,local
    mcp_config: '{"mcpServers":{"shadcn":{"command":"npx","args":["-y","shadcn@latest","mcp"]}}}'
    # <<< shipcrew-mcp
instructions: Do the task.
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
guardrails:
  ask_timeout: 60
  policies:
    shipcrew_owned_paths:
      type: function
      on: [tool_call]
      function:
        path: omnigent.shipcrew.policies.owned_paths
        arguments:
          owned_paths: []  # @task.owned_paths
          other_tasks: []  # @task.other_tasks
          root: ""  # @task.root
"""

SDK_CONFIG = """\
spec_version: 1
name: planner
executor:
  type: omnigent
  config:
    harness: claude-sdk
    permission_mode: auto
    strict_mcp_config: true
instructions: Plan.
"""

MEMINFO = """\
MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    6291456 kB
"""


# ── the pure capacity gate ──


class TestAutoMaxParallel:
    def test_memory_bound(self) -> None:
        # 6 GiB free, 2 GiB reserve, 350 MB each -> 11 more; 16 CPUs do not bind.
        cap = auto_max_parallel(
            running=0, mem_available_mb=6144, reserve_mb=2048, per_session_mb=350, cpus=16
        )
        assert cap == 11

    def test_running_sessions_are_added_back(self) -> None:
        # MemAvailable already excludes the 3 running sessions.
        cap = auto_max_parallel(
            running=3, mem_available_mb=3048, reserve_mb=2048, per_session_mb=500, cpus=16
        )
        assert cap == 5

    def test_cpu_bound(self) -> None:
        cap = auto_max_parallel(
            running=0, mem_available_mb=64_000, reserve_mb=2048, per_session_mb=350, cpus=4
        )
        assert cap == 4

    def test_ceiling(self) -> None:
        cap = auto_max_parallel(
            running=0,
            mem_available_mb=64_000,
            reserve_mb=2048,
            per_session_mb=350,
            cpus=32,
            ceiling=6,
        )
        assert cap == 6

    def test_memory_pressure_never_evicts_and_one_may_run(self) -> None:
        low = {"mem_available_mb": 1000, "reserve_mb": 2048, "per_session_mb": 350, "cpus": 8}
        assert auto_max_parallel(running=0, **low) == 1
        assert auto_max_parallel(running=5, **low) == 5  # no new start, nobody stopped
        assert auto_max_parallel(running=5, ceiling=2, **low) == 5

    def test_unknown_memory_falls_back_to_cpus_and_ceiling(self) -> None:
        cap = auto_max_parallel(
            running=0, mem_available_mb=None, reserve_mb=2048, per_session_mb=350, cpus=8
        )
        assert cap == 8
        assert (
            auto_max_parallel(
                running=0,
                mem_available_mb=None,
                reserve_mb=0,
                per_session_mb=350,
                cpus=8,
                ceiling=3,
            )
            == 3
        )

    @pytest.mark.parametrize(
        ("vcpu", "gib", "want"),
        # The VPS sizing table of docs/shipcrew/RESOURCES.md (claude-sdk, idle OS
        # using ~0.6 GiB, default 2 GiB reserve, 350 MB per session).
        [(2, 4, 2), (4, 8, 4), (8, 16, 8), (8, 32, 8), (16, 32, 16)],
    )
    def test_vps_sizing_table(self, vcpu: int, gib: int, want: int) -> None:
        available = int(gib * 1024 - 600)
        cap = auto_max_parallel(
            running=0,
            mem_available_mb=available,
            reserve_mb=resources.DEFAULT_RESERVE_MB,
            per_session_mb=resources.SESSION_MB_SDK,
            cpus=vcpu,
        )
        assert cap == want


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (None, (4, None)),
            ("", (4, None)),
            ("6", (6, None)),
            ("0", (1, None)),
            ("auto", (None, None)),
            (" AUTO ", (None, None)),
            ("auto:8", (None, 8)),
        ],
    )
    def test_max_parallel(self, raw: str | None, want: tuple[int | None, int | None]) -> None:
        assert parse_max_parallel(raw) == want

    @pytest.mark.parametrize("raw", ["lots", "auto:x", "4.5"])
    def test_max_parallel_rejects(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_max_parallel(raw)

    def test_meminfo(self) -> None:
        assert mem_available_mb(MEMINFO) == 6144
        assert mem_available_mb("MemTotal: 1 kB\n") is None
        assert mem_available_mb("MemAvailable: x kB\n") is None

    def test_meminfo_of_this_machine(self) -> None:
        if not Path("/proc/meminfo").is_file():
            pytest.skip("no /proc/meminfo")
        value = mem_available_mb()
        assert value is not None and value > 0

    def test_cpu_count(self) -> None:
        assert resources.cpu_count() >= 1


# ── worker harness selection ──


class TestWorkerHarness:
    def test_auto_default(self) -> None:
        choice = parse_worker_harness(None)
        assert choice == WorkerHarness()
        builders = {"designer", "scaffolder", "developer"}
        assert builders | {"reviewer", "integrator", "devops"} == SDK_DEFAULT_ROLES
        for role in SDK_DEFAULT_ROLES:
            assert choice.for_role(role) == SDK
        for role in ("qa", "security"):
            assert choice.for_role(role) == NATIVE

    @pytest.mark.parametrize(
        ("raw", "want"), [("sdk", SDK), ("native", NATIVE), ("headless", SDK)]
    )
    def test_global(self, raw: str, want: str) -> None:
        choice = parse_worker_harness(raw)
        assert {choice.for_role(r) for r in ("developer", "qa", "designer")} == {want}

    def test_overrides(self) -> None:
        choice = parse_worker_harness("auto, qa=sdk ,developer=native")
        assert choice.for_role("qa") == SDK
        assert choice.for_role("developer") == NATIVE
        assert choice.for_role("reviewer") == SDK
        assert choice.for_role("designer") == SDK
        assert choice.for_role("security") == NATIVE
        assert "qa=claude-sdk" in choice.describe()

    @pytest.mark.parametrize("raw", ["fast", "qa=auto", "QA!=sdk", "developer=tmux"])
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_worker_harness(raw)

    def test_sdk_rendering(self) -> None:
        text = apply_worker_harness(NATIVE_CONFIG, SDK)
        assert bundle_harness(text) == SDK
        assert "    permission_mode: auto\n" in text
        assert "allowed_tools:" not in text and "\n    mcp_config:" not in text
        # Kept: strict MCP, setting sources, comments, the guardrail slots.
        assert "strict_mcp_config: true" in text and "setting_sources: project,local" in text
        assert "# a comment that stays" in text and "# @task.owned_paths" in text

    def test_native_and_sdk_bundles_unchanged(self) -> None:
        assert apply_worker_harness(NATIVE_CONFIG, NATIVE) == NATIVE_CONFIG
        assert apply_worker_harness(NATIVE_CONFIG, None) == NATIVE_CONFIG
        assert apply_worker_harness(SDK_CONFIG, SDK) == SDK_CONFIG
        assert apply_worker_harness(SDK_CONFIG, NATIVE) == SDK_CONFIG

    def test_rendered_bundle_is_a_valid_sdk_spec(self, tmp_path: Path) -> None:
        """The rendered copy parses as claude-sdk with strict MCP + setting sources."""
        from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
        from omnigent.spec import parse, validate

        text = inject_task_contract(
            apply_worker_harness(NATIVE_CONFIG, SDK),
            owned_paths=["src/a.ts"],
            root="/repo",
            other_tasks=[{"title": "Other", "owned_paths": ["src/b.ts"]}],
        )
        bundle = tmp_path / "developer"
        bundle.mkdir()
        (bundle / "config.yaml").write_text(text)
        spec = parse(bundle, expand_env=False)
        assert not validate(spec).errors
        assert spec.executor.config["harness"] == SDK
        assert spec.executor.config["permission_mode"] == "auto"
        env = _build_claude_sdk_spawn_env(spec)
        assert env.get("HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG") == "1"
        assert env.get("HARNESS_CLAUDE_SDK_SETTING_SOURCES") == "project,local"
        policy = next(p for p in spec.guardrails.policies if p.name == "shipcrew_owned_paths")
        assert policy.function.arguments["owned_paths"] == ["src/a.ts"]

    def test_bundle_agent_dir_renders_the_upload_only(self, tmp_path: Path) -> None:
        bundle = tmp_path / "developer"
        bundle.mkdir()
        (bundle / "config.yaml").write_text(NATIVE_CONFIG)

        def uploaded(**kw: Any) -> str:
            data = bundle_agent_dir(bundle, **kw)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                member = tar.extractfile("config.yaml")
                assert member is not None
                return member.read().decode()

        assert bundle_harness(uploaded(harness=SDK)) == SDK
        assert uploaded(harness=NATIVE) == NATIVE_CONFIG
        both = uploaded(harness=SDK, owned_paths=["src/a.ts"], workspace="/wt")
        assert 'owned_paths: ["src/a.ts"]' in both and bundle_harness(both) == SDK
        assert (bundle / "config.yaml").read_text() == NATIVE_CONFIG  # disk untouched


# ── settings ──


class TestSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "SHIPCREW_MAX_PARALLEL",
            "SHIPCREW_WORKER_HARNESS",
            "SHIPCREW_SESSION_MB",
            "SHIPCREW_MEM_RESERVE_MB",
            "SHIPCREW_PARK_IDLE_WORKERS",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = ShipcrewSettings.from_env()
        assert (cfg.max_parallel, cfg.max_parallel_ceiling) == (4, None)
        assert cfg.worker_harness == WorkerHarness()
        assert cfg.harness_for("developer") == SDK and cfg.harness_for("qa") == NATIVE
        assert cfg.per_session_mb() == resources.SESSION_MB_SDK
        assert cfg.mem_reserve_mb == resources.DEFAULT_RESERVE_MB
        assert cfg.park_idle_workers is True
        assert cfg.capacity(running=3) == 4

    def test_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHIPCREW_MAX_PARALLEL", "auto:6")
        monkeypatch.setenv("SHIPCREW_WORKER_HARNESS", "native,reviewer=sdk")
        monkeypatch.setenv("SHIPCREW_MEM_RESERVE_MB", "1024")
        monkeypatch.setenv("SHIPCREW_PARK_IDLE_WORKERS", "0")
        cfg = ShipcrewSettings.from_env()
        assert (cfg.max_parallel, cfg.max_parallel_ceiling) == (None, 6)
        assert cfg.harness_for("developer") == NATIVE and cfg.harness_for("reviewer") == SDK
        assert cfg.per_session_mb() == resources.SESSION_MB_NATIVE
        assert cfg.mem_reserve_mb == 1024 and cfg.park_idle_workers is False
        monkeypatch.setenv("SHIPCREW_SESSION_MB", "512")
        assert ShipcrewSettings.from_env().per_session_mb() == 512

    def test_bad_harness_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHIPCREW_WORKER_HARNESS", "turbo")
        with pytest.raises(ValueError):
            ShipcrewSettings.from_env()

    def test_auto_capacity_reads_the_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resources, "mem_available_mb", lambda: 4096)
        monkeypatch.setattr(resources, "cgroup_limit_mb", lambda: None)
        monkeypatch.setattr(resources, "cpu_count", lambda: 16)
        cfg = ShipcrewSettings(max_parallel=None, mem_reserve_mb=2048, session_mb=500)
        assert cfg.capacity(running=2) == 6
        # A tighter cgroup (container / systemd MemoryMax) wins over MemAvailable.
        monkeypatch.setattr(resources, "cgroup_limit_mb", lambda: 2548)
        assert cfg.capacity(running=2) == 3


# ── the scheduler recomputes the cap each tick ──


@pytest.fixture
def settings(tmp_path: Path, agents_dir: Path) -> ShipcrewSettings:
    return ShipcrewSettings(
        agents_dir=agents_dir,
        max_parallel=None,  # SHIPCREW_MAX_PARALLEL=auto
        mem_reserve_mb=1000,
        session_mb=500,
        max_usd=None,
        scheduler_enabled=False,
        pr_loop_enabled=False,
        worker_harness=parse_worker_harness("auto"),
        db_url=f"sqlite:///{tmp_path / 'shipcrew.db'}",
    )


async def _ready_tasks(client: httpx.AsyncClient, n: int, role: str = "developer") -> list[str]:
    """``n`` ready tasks of *role* in a new mission, each owning ``<role><i>/**``."""
    mission = (
        await client.post(f"{P}/missions", json={"title": "M", "repo_path": "/repo"})
    ).json()
    ids = []
    for i in range(n):
        r = await client.post(
            f"{P}/missions/{mission['id']}/tasks",
            json={"title": f"t{i}", "owned_paths": [f"{role}{i}/**"], "role": role},
        )
        ids.append(r.json()["id"])
        await client.patch(f"{P}/tasks/{ids[-1]}", json={"status": "ready"})
    return ids


class TestAutoScheduling:
    async def test_each_tick_uses_the_memory_now(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        free = {"mb": 2000}  # 1000 above the reserve: 2 sessions of 500 MB
        monkeypatch.setattr(resources, "mem_available_mb", lambda: free["mb"])
        monkeypatch.setattr(resources, "cgroup_limit_mb", lambda: None)
        monkeypatch.setattr(resources, "cpu_count", lambda: 8)
        ids = await _ready_tasks(client, 5)
        scheduler = ShipcrewScheduler(lambda: service, 60)
        assert await scheduler.tick() == ids[:2]
        waiting = await service.require_task(ids[2])
        assert waiting.blocked_reason == "capacity: 2/2 agents running"
        # The two running sessions used their share; nothing new fits.
        free["mb"] = 1100
        assert await scheduler.tick() == []
        # Memory freed (e.g. a build finished): one more slot.
        free["mb"] = 1500
        assert await scheduler.tick() == [ids[2]]
        assert len(sessions.created) == 3

    async def test_sessions_get_the_role_harness(
        self,
        client: httpx.AsyncClient,
        service: ShipcrewService,
        sessions: FakeSessions,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(resources, "mem_available_mb", lambda: None)
        monkeypatch.setattr(resources, "cpu_count", lambda: 8)
        await _ready_tasks(client, 1, role="developer")
        await _ready_tasks(client, 1, role="reviewer")
        await ShipcrewScheduler(lambda: service, 60).tick()
        by_role = {r.agent_dir.name: r.harness for r in sessions.created}
        assert by_role == {"developer": SDK, "reviewer": SDK}


# ── the relay-path approval hook (claude-sdk ASKs) ──


class _State:
    pass


class _App:
    def __init__(self) -> None:
        self.state = _State()


def test_accepted_relay_ask_reaches_the_board_hook() -> None:
    from omnigent.server.routes._sessions.common import (
        _pending_policy_ask_writes,
        _PendingPolicyAskWrites,
    )

    calls: list[tuple[str, str | None]] = []
    app = _App()
    setattr(app.state, APP_STATE_HOOK, lambda sid, reason: calls.append((sid, reason)))
    reason = "Write needs approval: `README.md` is outside this task's owned paths (src/**)"
    _pending_policy_ask_writes["elicit_t1"] = _PendingPolicyAskWrites(
        state_updates=None, set_labels=None, policy_reason=reason
    )
    try:
        notify_accepted_relay_ask(app, "s1", {"elicitation_id": "elicit_t1", "action": "decline"})
        assert calls == []
        notify_accepted_relay_ask(app, "s1", {"elicitation_id": "elicit_t1", "action": "accept"})
        assert calls == [("s1", reason)]
        # Unknown elicitation, no reason, no hook: silently nothing.
        notify_accepted_relay_ask(app, "s1", {"elicitation_id": "nope", "action": "accept"})
        accept = {"elicitation_id": "elicit_t1", "action": "accept"}
        notify_accepted_relay_ask(object(), "s1", accept)
        assert calls == [("s1", reason)]
    finally:
        _pending_policy_ask_writes.pop("elicit_t1", None)


# ── headless Chromium preference ──


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class TestHeadlessChromium:
    def test_path_wins(self, tmp_path: Path) -> None:
        shell = _exe(tmp_path / "bin" / "chrome-headless-shell")
        found = resources.headless_chromium(
            path_env=str(shell.parent), playwright_cache=tmp_path / "none"
        )
        assert found == str(shell)

    def test_newest_playwright_build(self, tmp_path: Path) -> None:
        cache = tmp_path / "ms-playwright"
        for rev in ("1228", "1234", "999"):
            _exe(cache / f"chromium_headless_shell-{rev}" / "chrome-headless-shell-linux64"
                 / "chrome-headless-shell")  # fmt: skip
        found = resources.headless_chromium(
            path_env=str(tmp_path / "empty"), playwright_cache=cache
        )
        assert found is not None and "chromium_headless_shell-1234" in found

    def test_none_without_a_shell(self, tmp_path: Path) -> None:
        assert (
            resources.headless_chromium(path_env=str(tmp_path), playwright_cache=tmp_path) is None
        )

    def test_session_env_prefers_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.shipcrew import tools

        cache = tmp_path / "ms-playwright"
        shell = _exe(cache / "chromium_headless_shell-1" / "chrome-headless-shell-linux64"
                     / "chrome-headless-shell")  # fmt: skip
        monkeypatch.setattr(tools, "CONFIG", tmp_path / "tools.json")
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.delenv("CHROMIUM_PATH", raising=False)
        monkeypatch.delenv("SHIPCREW_CHROMIUM", raising=False)
        assert tools.session_env()["CHROMIUM_PATH"] == str(shell)
        monkeypatch.setenv("SHIPCREW_CHROMIUM", "/opt/chrome")
        assert tools.session_env()["CHROMIUM_PATH"] == "/opt/chrome"
        monkeypatch.setenv("CHROMIUM_PATH", "/usr/bin/chromium")
        assert tools.session_env()["CHROMIUM_PATH"] == "/usr/bin/chromium"
