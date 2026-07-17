"""In-process outbox: spool-path/agent-id resolution, ticket table, and YAML spool writer.

Pure, testable units with no MCP-server or asyncio coupling. The bridge owns an Outbox
instance; the background sender (sender.py) drains it.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

from . import cli

_TICKET_BOUND = 200  # keep the last N ticket statuses; evict oldest


def resolve_agent_id(cli_arg: str | None) -> str | None:
    """flag -> PARTYLINE_AGENT_ID -> .partyline/config.json (agentId/agent_id)."""
    if cli_arg:
        return cli_arg
    env = os.getenv("PARTYLINE_AGENT_ID")
    if env:
        return env
    cfg = cli._load_config()
    return cfg.get("agentId") or cfg.get("agent_id")


def resolve_spool_path(agent_id: str, env_value: str | None, cwd: str) -> str:
    """Resolve where undeliverable messages spill. See module/spec docs for the rules."""
    if not env_value:
        parent = os.path.dirname(os.path.abspath(cwd))
        return os.path.join(parent, f"partyline_{agent_id}.yaml")
    if "{agent_id}" in env_value:
        return env_value.replace("{agent_id}", agent_id)
    if os.path.isdir(env_value) or env_value.endswith((os.sep, "/")):
        return os.path.join(env_value.rstrip("/" + os.sep), f"partyline_{agent_id}.yaml")
    return env_value
