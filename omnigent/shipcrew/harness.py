"""Which Claude harness a worker session runs on: ``claude-native`` or ``claude-sdk``.

The role bundles are authored for ``claude-native`` (the real Claude Code TUI
in tmux: a human can attach to the live terminal from the board). On an
unattended VPS the headless ``claude-sdk`` harness does the same work with a
lighter process tree (measured in ``docs/shipcrew/RESOURCES.md``: about 300 MB
PSS per session in parallel instead of 660 MB, half the CPU per turn, a third of
the idle CPU). The server picks the harness per role at session creation
(``SHIPCREW_WORKER_HARNESS``) and rewrites the uploaded copy of the bundle;
the bundles on disk never change.

``SHIPCREW_WORKER_HARNESS`` grammar: a default, then optional per-role
overrides, comma separated::

    auto                      # default: sdk for SDK_DEFAULT_ROLES, native otherwise
    sdk                       # every worker role on claude-sdk
    native                    # every worker role on claude-native (attachable)
    auto,qa=sdk,developer=native

What the rewrite keeps and what it cannot (see RESOURCES.md, "Guarantees"):

- kept: every guardrail (the policies judge ``sys_os_shell`` / ``sys_os_write``
  / ``sys_os_edit`` / ``sys_os_read`` exactly like ``Bash`` / ``Write`` /
  ``Edit`` / ``Read``; ASK becomes an approval card, DENY returns its hint to
  the agent), the owned-paths contract (injected into the same policy slots),
  ``--strict-mcp-config`` (``strict_mcp_config`` -> the SDK strict env flag),
  ``--setting-sources project,local``, the bundle's skills (``--plugin-dir``),
  cost reporting, interrupt and stop (a stopped session relaunches with its
  conversation on the next message);
- dropped: the bundle's own ``mcp_config`` servers (chrome-devtools):
  the SDK path does not load them, so roles whose instructions rely on one
  stay on native under ``auto``; Claude's Task sub-agents (the SDK harness
  exposes no Task tool, so no hidden sub-agent can run outside the board
  tree; loop children such as the reviewer are API-created either way);
- ``permission_mode: default`` + ``allowed_tools`` (native) becomes ``auto``
  (the SDK pre-approves omnigent's tools and the guardrails gate each call),
  the same setup as the planner bundle.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

NATIVE = "claude-native"
SDK = "claude-sdk"
_ALIASES = {
    "native": NATIVE,
    "claude-native": NATIVE,
    "sdk": SDK,
    "claude-sdk": SDK,
    "headless": SDK,
}
AUTO = "auto"

#: Roles that run headless under ``auto``: no MCP server of their own and no
#: human expected in their terminal (the builders add shadcn components with
#: the CLI, not the shadcn MCP). qa / security stay on native because their
#: instructions use the chrome-devtools MCP.
SDK_DEFAULT_ROLES: frozenset[str] = frozenset(
    {"designer", "scaffolder", "developer", "reviewer", "integrator", "devops"}
)

_HARNESS_LINE = re.compile(r"^(?P<indent>[ \t]+)harness:[ \t]*claude-native[ \t]*(?:#.*)?$", re.M)
_PERMISSION_LINE = re.compile(r"^(?P<indent>[ \t]+)permission_mode:[ \t]*\S+[ \t]*(?:#.*)?$", re.M)
_ALLOWED_TOOLS_LINE = re.compile(r"^[ \t]+allowed_tools:[^\n]*\n", re.M)
_MCP_CONFIG_LINE = re.compile(r"^[ \t]+mcp_config:[^\n]*\n", re.M)


@dataclass(frozen=True)
class WorkerHarness:
    """Parsed ``SHIPCREW_WORKER_HARNESS``.

    :param default: ``"auto"``, ``"claude-native"`` or ``"claude-sdk"``.
    :param overrides: Role -> harness (``"claude-native"`` / ``"claude-sdk"``).
    """

    default: str = AUTO
    overrides: Mapping[str, str] = field(default_factory=dict)

    def for_role(self, role: str) -> str:
        """Harness for a worker *role* whose bundle is authored for claude-native."""
        if role in self.overrides:
            return self.overrides[role]
        if self.default == AUTO:
            return SDK if role in SDK_DEFAULT_ROLES else NATIVE
        return self.default

    def describe(self) -> str:
        """Human-readable form, e.g. ``"auto (sdk: developer, ...), qa=claude-sdk"``."""
        base = (
            f"auto (sdk: {', '.join(sorted(SDK_DEFAULT_ROLES))})"
            if self.default == AUTO
            else self.default
        )
        extra = [f"{r}={h}" for r, h in sorted(self.overrides.items())]
        return ", ".join([base, *extra])


def _harness_name(value: str, *, allow_auto: bool) -> str:
    key = value.strip().lower()
    if allow_auto and key == AUTO:
        return AUTO
    if key not in _ALIASES:
        choices = "auto, native, sdk" if allow_auto else "native, sdk"
        raise ValueError(f"SHIPCREW_WORKER_HARNESS: unknown harness {value!r} ({choices})")
    return _ALIASES[key]


def parse_worker_harness(raw: str | None) -> WorkerHarness:
    """Parse ``SHIPCREW_WORKER_HARNESS`` (empty -> ``auto``).

    :raises ValueError: On an unknown harness name or a malformed override.
    """
    default = AUTO
    overrides: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            role, _, value = part.partition("=")
            role = role.strip()
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", role):
                raise ValueError(f"SHIPCREW_WORKER_HARNESS: bad role name {role!r}")
            overrides[role] = _harness_name(value, allow_auto=False)
        else:
            default = _harness_name(part, allow_auto=True)
    return WorkerHarness(default=default, overrides=overrides)


def bundle_harness(config_text: str) -> str | None:
    """The harness a bundle ``config.yaml`` declares (``None`` when it names none)."""
    match = re.search(r"^[ \t]+harness:[ \t]*([A-Za-z0-9_-]+)", config_text, re.M)
    return match.group(1) if match else None


def apply_worker_harness(config_text: str, harness: str | None) -> str:
    """Render a claude-native worker bundle ``config.yaml`` for *harness*.

    Only a bundle authored for claude-native is rewritten, and only towards
    claude-sdk: ``harness: claude-sdk``, ``permission_mode: auto``, no
    ``allowed_tools`` (native-only) and no ``mcp_config`` (not loaded by the
    SDK path). ``strict_mcp_config``, ``setting_sources``, the guardrails and
    the owned-paths slots are left untouched. ``None`` / claude-native / an
    SDK bundle (planner, orchestrator) come back unchanged.
    """
    if harness != SDK or _HARNESS_LINE.search(config_text) is None:
        return config_text
    if _MCP_CONFIG_LINE.search(config_text):
        _logger.info("shipcrew: claude-sdk worker session drops the bundle's mcp_config servers")
    text = _HARNESS_LINE.sub(lambda m: f"{m['indent']}harness: {SDK}", config_text, count=1)
    text = _PERMISSION_LINE.sub(lambda m: f"{m['indent']}permission_mode: auto", text, count=1)
    text = _ALLOWED_TOOLS_LINE.sub("", text, count=1)
    return _MCP_CONFIG_LINE.sub("", text, count=1)
