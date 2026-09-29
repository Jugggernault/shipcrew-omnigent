"""Per-role MCP scoping: bundle config -> Claude launch flags (native and SDK)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.harnesses.claude_native.bridge import augment_claude_args
from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.server.routes._sessions.helpers import _derive_terminal_launch_args_from_spec
from omnigent.shipcrew.launch_args import (
    claude_mcp_launch_args,
    claude_setting_sources_args,
    mcp_server_names,
    parse_mcp_config,
    setting_sources,
    strict_mcp_enabled,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec

SHADCN = {"mcpServers": {"shadcn": {"command": "npx", "args": ["-y", "shadcn@latest", "mcp"]}}}


def _spec(**config: object) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="developer",
        executor=ExecutorSpec(type="omnigent", config=dict(config)),
    )


def _mcp_configs(args: list[str]) -> list[dict[str, Any]]:
    return [json.loads(args[i + 1]) for i, a in enumerate(args) if a == "--mcp-config"]


class TestHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("true", True), ("True", True), ("1", True), (True, True), ("false", False),
         ("False", False), ("", False), (None, False), (False, False)],
    )  # fmt: skip
    def test_strict_flag_values(self, value: object, expected: bool) -> None:
        assert strict_mcp_enabled({"strict_mcp_config": value}) is expected

    def test_parse_and_names(self) -> None:
        both = {"mcpServers": {**SHADCN["mcpServers"], "chrome-devtools": {"command": "npx"}}}
        assert parse_mcp_config(json.dumps(both)) == both
        assert mcp_server_names({"mcp_config": json.dumps(both)}) == ["chrome-devtools", "shadcn"]
        assert parse_mcp_config("") is None
        assert mcp_server_names({}) == []

    @pytest.mark.parametrize(
        "raw",
        ["{", "[]", '{"servers": {}}', '{"mcpServers": []}', '{"mcpServers": {"a b": {}}}',
         '{"mcpServers": {"x": "npx"}}'],
    )  # fmt: skip
    def test_malformed_config_is_refused(self, raw: str) -> None:
        with pytest.raises(ValueError, match="mcp_config"):
            claude_mcp_launch_args({"mcp_config": raw})


class TestClaudeNative:
    def test_strict_with_role_servers(self) -> None:
        spec = _spec(
            harness="claude-native",
            permission_mode="default",
            allowed_tools="Bash,Read,mcp__omnigent,mcp__shadcn",
            strict_mcp_config="True",
            mcp_config=json.dumps(SHADCN, indent=2),
        )
        args = _derive_terminal_launch_args_from_spec(spec, headless_defaults=False)
        assert args is not None
        assert args[:4] == [
            "--permission-mode",
            "default",
            "--allowedTools",
            "Bash,Read,mcp__omnigent,mcp__shadcn",
        ]
        assert args[4] == "--strict-mcp-config"
        assert args[5] == "--mcp-config"
        # Re-serialized compactly: one argv entry, same content.
        assert json.loads(args[6]) == SHADCN
        assert "\n" not in args[6]
        assert len(args) == 7

    def test_strict_without_servers(self) -> None:
        spec = _spec(harness="claude-native", permission_mode="default", strict_mcp_config="true")
        assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
            "--permission-mode",
            "default",
            "--strict-mcp-config",
        ]

    def test_headless_worker_create_gets_the_same_flags(self) -> None:
        spec = _spec(harness="claude-native", strict_mcp_config="true")
        assert _derive_terminal_launch_args_from_spec(spec) == ["--strict-mcp-config"]

    def test_unscoped_bundle_is_unchanged(self) -> None:
        spec = _spec(harness="claude-native", permission_mode="default")
        assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
            "--permission-mode",
            "default",
        ]
        assert _derive_terminal_launch_args_from_spec(_spec(harness="claude-native")) is None

    def test_oversized_config_hits_the_launch_arg_bound(self) -> None:
        big = {"mcpServers": {"x": {"command": "npx", "args": ["a" * 5000]}}}
        spec = _spec(harness="claude-native", mcp_config=json.dumps(big))
        with pytest.raises(ValueError, match="terminal_launch_args"):
            _derive_terminal_launch_args_from_spec(spec)

    def test_sdk_harness_gets_no_terminal_args(self) -> None:
        spec = _spec(harness="claude-sdk", strict_mcp_config="true")
        assert _derive_terminal_launch_args_from_spec(spec) is None

    def test_relay_stays_after_the_role_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bridge appends omnigent's relay as a second ``--mcp-config``.

        Claude merges repeated ``--mcp-config`` flags and ``--strict-mcp-config``
        keeps every server passed that way (checked against the real CLI:
        both servers appear with ``source: dynamic``), so the ``sys_*`` relay
        survives the scoping.
        """
        spec = _spec(
            harness="claude-native",
            permission_mode="default",
            strict_mcp_config="true",
            mcp_config=json.dumps(SHADCN),
        )
        base = _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) or []
        monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
        args = augment_claude_args(tuple(base), bridge_dir=tmp_path)
        assert args.count("--strict-mcp-config") == 1
        configs = _mcp_configs(args)
        assert [sorted(c["mcpServers"]) for c in configs] == [["shadcn"], ["omnigent"]]
        relay = configs[1]["mcpServers"]["omnigent"]
        assert "serve-mcp" in relay["args"]


class TestClaudeSdk:
    def test_spawn_env_flag(self) -> None:
        strict = _spec(harness="claude-sdk", permission_mode="auto", strict_mcp_config="True")
        env = _build_claude_sdk_spawn_env(strict)
        assert env["HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG"] == "1"
        plain = _spec(harness="claude-sdk", permission_mode="auto")
        assert "HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG" not in _build_claude_sdk_spawn_env(plain)

    def test_harness_reads_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.inner import claude_sdk_harness

        monkeypatch.setenv("HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG", "1")
        executor = claude_sdk_harness._build_claude_sdk_executor()
        assert executor._strict_mcp_config is True  # type: ignore[attr-defined]
        monkeypatch.delenv("HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG")
        executor = claude_sdk_harness._build_claude_sdk_executor()
        assert executor._strict_mcp_config is False  # type: ignore[attr-defined]

    @pytest.mark.parametrize("strict", [True, False])
    def test_executor_passes_strict_to_the_cli(self, strict: bool) -> None:
        options = _run_executor(strict_mcp_config=strict)
        expected: dict[str, None] = {"no-session-persistence": None}
        if strict:
            expected["strict-mcp-config"] = None
        assert options["extra_args"] == expected


def _run_executor(**kwargs: Any) -> dict[str, Any]:
    """Run one turn of ClaudeSDKExecutor(**kwargs) on a fake SDK; return the options."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    seen: list[dict[str, Any]] = []

    class _Result:
        def __init__(self, session_id: str, result: str) -> None:
            self.session_id = session_id
            self.result = result

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = _Result
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kw: self.__dict__.update(kw)},
        )

        class ClaudeSDKClient:
            def __init__(self, options: Any) -> None:
                self.options = options

            async def connect(self) -> None:
                return None

            async def query(self, prompt: str, session_id: str = "default") -> None:
                seen.append(dict(self.options.__dict__))

            async def receive_response(self) -> Any:
                yield _Result("claude-s", "ok")

            async def disconnect(self) -> None:
                return None

    async def run() -> None:
        executor = ClaudeSDKExecutor(**kwargs)
        with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
            messages = [{"role": "user", "content": "hi", "session_id": "s"}]
            [e async for e in executor.run_turn(messages, [], "")]

    asyncio.run(run())
    assert len(seen) == 1
    return seen[0]


class TestSettingSources:
    """``setting_sources: project,local``: no host-user settings, plugins or hooks.

    Checked against the real CLI (2.1.285, subscription login): with
    ``--setting-sources project,local`` the init event lists only the built-in
    plugins (no vercel / superpowers / ponytail ...), no SessionStart hook runs,
    and the turn still answers; without it 8 user plugins and 5 hooks load.
    """

    def test_parse(self) -> None:
        assert setting_sources({}) is None
        assert setting_sources({"setting_sources": "project,local"}) == ["project", "local"]
        assert setting_sources({"setting_sources": " project local "}) == ["project", "local"]
        assert setting_sources({"setting_sources": ""}) is None
        with pytest.raises(ValueError, match="unknown source"):
            setting_sources({"setting_sources": "project,managed"})

    def test_native_launch_args(self) -> None:
        spec = _spec(
            harness="claude-native",
            permission_mode="default",
            setting_sources="project,local",
            strict_mcp_config="true",
        )
        assert _derive_terminal_launch_args_from_spec(spec, headless_defaults=False) == [
            "--permission-mode", "default", "--setting-sources", "project,local",
            "--strict-mcp-config",
        ]  # fmt: skip
        assert claude_setting_sources_args({}) == []

    def test_bundle_skills_still_ride_plugin_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "bundle"
        (bundle / "skills" / "design-lock").mkdir(parents=True)
        (bundle / "skills" / "design-lock" / "SKILL.md").write_text(
            "---\nname: design-lock\n---\n"
        )
        spec = _spec(harness="claude-native", setting_sources="project,local")
        base = _derive_terminal_launch_args_from_spec(spec) or []
        monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
        args = augment_claude_args(tuple(base), bridge_dir=tmp_path, bundle_dir=bundle)
        assert args[args.index("--plugin-dir") + 1] == str(bundle)
        assert args.count("--setting-sources") == 1
        assert args[args.index("--setting-sources") + 1] == "project,local"
        assert "--settings" in args  # the bridge's hooks file still applies

    def test_sdk_spawn_env_and_harness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.inner import claude_sdk_harness

        spec = _spec(harness="claude-sdk", setting_sources="project,local")
        env = _build_claude_sdk_spawn_env(spec)
        assert env["HARNESS_CLAUDE_SDK_SETTING_SOURCES"] == "project,local"
        assert "HARNESS_CLAUDE_SDK_SETTING_SOURCES" not in _build_claude_sdk_spawn_env(
            _spec(harness="claude-sdk")
        )
        monkeypatch.setenv("HARNESS_CLAUDE_SDK_SETTING_SOURCES", "project,local")
        executor = claude_sdk_harness._build_claude_sdk_executor()
        assert executor._setting_sources == ["project", "local"]  # type: ignore[attr-defined]

    def test_sdk_executor_forwards_them(self) -> None:
        assert _run_executor(setting_sources=["project", "local"])["setting_sources"] == [
            "project",
            "local",
        ]
        assert "setting_sources" not in _run_executor()
        # A "none" skills filter hides every source and still wins.
        none = _run_executor(setting_sources=["project", "local"], skills_filter="none")
        assert none["setting_sources"] == []
