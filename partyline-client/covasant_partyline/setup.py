"""`partyline setup` (once per machine) and `partyline init` (once per clone).

setup  registers the live channel as a *user-scope* Claude Code MCP server, installs a
       `claude-pl` launcher that passes the development-channel flag, and remembers
       owner/url/profile in ~/.partyline/user.json.
init   writes this clone's gitignored .partyline/config.json with the next free
       `<owner>_<repo>_<n>` agent id, then registers the agent.

The channel resolves its identity from the nearest .partyline/config.json (cli._load_config
walks up from the cwd), so one user-scope server definition serves every clone — the
per-clone id never has to live in a tracked file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import cli

CHANNEL_SERVER = "partyline-channel"
CHANNEL_FLAG = f"server:{CHANNEL_SERVER}"
USER_FILE = Path.home() / ".partyline" / "user.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "repo"


def next_agent_id(owner: str, repo: str, existing: Iterable[str]) -> str:
    """Lowest `<owner>_<repo>_<n>` (n>=1) not already registered on the line."""
    prefix = f"{_slug(owner)}_{_slug(repo)}_"
    taken = {
        int(i[len(prefix) :]) for i in existing if i.startswith(prefix) and i[len(prefix) :].isdigit()
    }
    n = 1
    while n in taken:
        n += 1
    return f"{prefix}{n}"


def _agent_ids(payload: object) -> list[str]:
    rows = payload.get("agents", []) if isinstance(payload, dict) else payload or []
    return [r["agent_id"] for r in rows if isinstance(r, dict) and r.get("agent_id")]


def _load_user() -> dict:
    try:
        return json.loads(USER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure_login(profile: str, host: str | None) -> None:
    """Make sure `profile` can mint a Databricks token, logging in interactively if not.

    The app requires OAuth on every route, so nothing else works until this does. The
    login opens a browser — unavoidable for U2M OAuth, but it is a command, not a UI hunt.
    """
    if cli._mint_token(profile):
        return
    db = shutil.which("databricks")
    if not db:
        sys.exit("partyline: the `databricks` CLI is not installed or not on PATH")
    if not host:
        sys.exit(
            f"partyline: profile '{profile}' has no valid login and no workspace host is known.\n"
            f"  run: databricks auth login --host <workspace-url> --profile {profile}\n"
            "  then re-run, or pass --workspace-host so setup can do it for you"
        )
    print(f"==> No valid Databricks login for profile '{profile}'; opening browser login ({host})")
    subprocess.run([db, "auth", "login", "--host", host, "--profile", profile], check=False)
    if not cli._mint_token(profile):
        sys.exit(f"partyline: login for profile '{profile}' did not complete; re-run `partyline setup`")


def _launchers(bin_dir: Path) -> list[Path]:
    cmd = bin_dir / "claude-pl.cmd"
    sh = bin_dir / "claude-pl"
    cmd.write_text(f"@echo off\r\nclaude --dangerously-load-development-channels {CHANNEL_FLAG} %*\r\n")
    sh.write_text(f'#!/bin/sh\nexec claude --dangerously-load-development-channels {CHANNEL_FLAG} "$@"\n')
    # The other launchers run under the interpreter that has this package (a pipx venv is not the
    # system python), so each pins sys.executable rather than relying on a console script.
    py = sys.executable
    written = [cmd, sh]
    for name, module, extra in (
        ("codex-pl", "covasant_partyline.codex_runner", ""),
        ("grok-pl", "covasant_partyline.harness_runner", " grok"),
        ("agy-pl", "covasant_partyline.harness_runner", " agy"),
        ("kimi-pl", "covasant_partyline.harness_runner", " kimi"),
    ):
        bat, script = bin_dir / f"{name}.cmd", bin_dir / name
        bat.write_text(f'@echo off\r\n"{py}" -m {module}{extra} %*\r\n')
        script.write_text(f'#!/bin/sh\nexec "{py}" -m {module}{extra} "$@"\n')
        written += [bat, script]
    for p in written:
        if p.suffix != ".cmd":
            try:
                p.chmod(0o755)
            except OSError:
                pass
    return written


def cmd_setup(a: argparse.Namespace) -> None:
    user = _load_user()
    user.update(
        {k: v for k, v in (("owner", a.owner), ("url", a.url_), ("profile", a.profile_), ("host", a.host_)) if v}
    )
    if not user.get("owner"):
        sys.exit("partyline setup: pass --owner (your handle, e.g. alan) the first time")
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_FILE.write_text(json.dumps(user, indent=2), encoding="utf-8")
    print(f"remembered {USER_FILE}: {user}")
    ensure_login(user.get("profile") or "dev-eastus2", user.get("host"))

    claude = shutil.which("claude")
    if not claude:
        sys.exit("partyline setup: `claude` not found on PATH")
    have = subprocess.run([claude, "mcp", "get", CHANNEL_SERVER], capture_output=True, text=True)
    if have.returncode == 0:
        print(f"user-scope MCP server '{CHANNEL_SERVER}' already registered")
    else:
        profile = user.get("profile") or "dev-eastus2"
        subprocess.run(
            # name first: `-e` is variadic and would otherwise swallow it
            [claude, "mcp", "add", CHANNEL_SERVER, "--scope", "user", "-e", f"DATABRICKS_CONFIG_PROFILE={profile}",
             "--", "partyline-channel"],
            check=True,
        )
        print(f"registered user-scope MCP server '{CHANNEL_SERVER}'")

    exe = shutil.which("partyline")
    if exe:
        for p in _launchers(Path(exe).parent):
            print(f"wrote launcher {p}")
    else:
        print("note: `partyline` not on PATH, skipped the claude-pl launcher")
    print(f"\nNext: in each clone run `partyline init`, then start Claude with `claude-pl` (or `codex-pl` / `grok-pl` / `agy-pl` / `kimi-pl`).")


def _repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path.cwd()


def _ensure_ignored(root: Path) -> None:
    gi = root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if not any(line.strip() in (".partyline", ".partyline/") for line in text.splitlines()):
        gi.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".partyline/\n", encoding="utf-8")
        print(f"added .partyline/ to {gi}")


def cmd_init(a: argparse.Namespace) -> None:
    root = _repo_root()
    cfg_path = root / ".partyline" / "config.json"
    if cfg_path.exists() and not a.force:
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8")).get("agentId")
        except (OSError, ValueError):
            existing = None
        if existing:
            print(f"{cfg_path} already has agentId {existing} (use --force to reassign)")
            return

    user = _load_user()
    owner = a.owner or user.get("owner")
    if not owner:
        sys.exit("partyline init: no owner — run `partyline setup --owner <handle>` first or pass --owner")
    url = a.url if a.url and not cli.is_local_url(a.url) else user.get("url") or a.url
    profile = a.profile or user.get("profile") or "dev-eastus2"
    repo = a.repo or root.name

    if not os.getenv("PARTYLINE_TOKEN") and not cli.is_local_url(url):
        ensure_login(profile, user.get("host"))
        token = cli._mint_token(profile)
        if token:
            os.environ["PARTYLINE_TOKEN"] = token
    ids = _agent_ids(cli._request("GET", url, "/api/agents?include_stale=true"))
    agent_id = next_agent_id(owner, repo, ids)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"url": url, "agentId": agent_id, "databricksProfile": profile}, indent=2), encoding="utf-8"
    )
    _ensure_ignored(root)
    cli._request(
        "POST", url, "/api/agents",
        {"agent_id": agent_id, "owner": owner, "repo": repo, "workdirs": [cli.normalize_target(str(root))]},
    )
    print(f"initialised {root} as {agent_id}")
