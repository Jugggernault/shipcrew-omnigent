"""Per-role MCP scoping: bundle ``executor.config`` -> Claude launch flags.

Without this a Claude session loads every MCP server of the user who runs the
host (``~/.claude.json``, installed plugins, claude.ai connectors such as
Gmail, Canva or Notion). That costs context on every turn and widens what a
prompt-injected agent can reach. A shipcrew bundle declares instead::

    executor:
      config:
        strict_mcp_config: "true"   # only the servers below (+ omnigent's relay)
        mcp_config: '{"mcpServers": {"shadcn": {"command": "npx", "args": [...]}}}'

- claude-native: :func:`claude_mcp_launch_args` turns this into
  ``--strict-mcp-config`` and ``--mcp-config <json>`` terminal launch args
  (``omnigent/server/routes/_sessions/helpers.py``). The claude-native bridge
  appends its own ``--mcp-config`` for omnigent's relay (``sys_*`` tools) after
  these args; Claude merges repeated ``--mcp-config`` flags, and strict mode
  keeps every server passed that way, so the relay stays.
- claude-sdk: :func:`strict_mcp_enabled` gates ``HARNESS_CLAUDE_SDK_STRICT_MCP_CONFIG``
  (``omnigent/runtime/workflow.py``), which makes the SDK executor pass
  ``--strict-mcp-config``. The SDK hands its in-process ``omnigent`` server
  to the CLI through ``--mcp-config`` too, so it stays. ``mcp_config`` is not
  used there (no shipcrew SDK role needs a server).

The values are strings because omnigent's spec parser stringifies every scalar
``executor.config`` value.
"""

from __future__ import annotations

import json
import re
from typing import Any

STRICT_KEY = "strict_mcp_config"
CONFIG_KEY = "mcp_config"
_TRUE = frozenset({"true", "1", "yes", "on"})
# Claude names MCP tools ``mcp__<server>__<tool>``: keep server names plain.
_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def strict_mcp_enabled(config: dict[str, Any]) -> bool:
    """Whether the bundle asks for ``--strict-mcp-config``."""
    value = config.get(STRICT_KEY)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def parse_mcp_config(raw: object) -> dict[str, Any] | None:
    """The bundle's ``mcp_config`` as ``{"mcpServers": {...}}``, or ``None`` when unset.

    :raises ValueError: When it is not a JSON object with an ``mcpServers``
        mapping of named server objects.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else json.dumps(raw))
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"executor.config.{CONFIG_KEY} is not valid JSON: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(f'executor.config.{CONFIG_KEY} must be {{"mcpServers": {{...}}}}')
    for name, server in servers.items():
        if not _SERVER_NAME.match(str(name)) or not isinstance(server, dict):
            raise ValueError(f"executor.config.{CONFIG_KEY}: invalid MCP server entry {name!r}")
    return {"mcpServers": servers}


def mcp_server_names(config: dict[str, Any]) -> list[str]:
    """Names of the MCP servers the bundle declares (sorted)."""
    parsed = parse_mcp_config(config.get(CONFIG_KEY))
    return sorted(parsed["mcpServers"]) if parsed else []


def claude_mcp_launch_args(config: dict[str, Any]) -> list[str]:
    """``--strict-mcp-config`` / ``--mcp-config <json>`` for a claude-native bundle.

    :returns: An empty list when the bundle declares neither key.
    :raises ValueError: On a malformed ``mcp_config``.
    """
    args: list[str] = []
    if strict_mcp_enabled(config):
        args.append("--strict-mcp-config")
    parsed = parse_mcp_config(config.get(CONFIG_KEY))
    if parsed is not None:
        args += ["--mcp-config", json.dumps(parsed, separators=(",", ":"))]
    return args
