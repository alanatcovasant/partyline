"""Register this clone's agent on the line when a launcher starts.

`partyline init` registers once per clone; nothing after that refreshes presence for a harness
without its own session-start step, so an agent started with `codex-pl` / `grok-pl` / `agy-pl`
would sit idle/stale until it registered itself. Registration is an upsert (it also bumps the
heartbeat), so doing it at every launch is safe. Best-effort: a launcher must start even when
the line is unreachable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import cli, setup


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def register_self(harness: str) -> str | None:
    """Register the configured agent; return its id, or None when there is nothing to register
    (no `.partyline/config.json` identity) or the line refused/was unreachable."""
    cfg = cli._load_config()
    agent_id = cli.resolve_agent_id(cfg=cfg)
    if not agent_id:
        return None
    url = cli.resolve_url(cfg=cfg)
    profile = cli.resolve_profile(url=url, cfg=cfg)
    root = _git("rev-parse", "--show-toplevel") or os.getcwd()
    owner = setup._load_user().get("owner") or agent_id.split("_", 1)[0]
    try:
        workdir = cli.normalize_target(root)
    except SystemExit:  # normalize_target exits on a path that does not exist
        return None
    body = {
        "agent_id": agent_id,
        "owner": owner,
        "repo": Path(root).name,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or None,
        "harness": harness,
        "workdirs": [workdir],
    }
    if not os.getenv("PARTYLINE_TOKEN") and profile:
        token = cli._mint_token(profile)
        if token:
            os.environ["PARTYLINE_TOKEN"] = token
    try:
        cli._request("POST", url, "/api/agents", body)
    except SystemExit:  # _request exits on HTTP/connection errors
        return None
    return agent_id
