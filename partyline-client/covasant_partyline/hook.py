"""Claude Code SessionStart hook: inject recent Partyline traffic as session context.

Stdlib-only, fail-silent: a broken hook must never block a session, so every error
path exits 0 with no output. Prints nothing when the line is quiet.

Wire it in .claude/settings.json:

    "hooks": {
      "SessionStart": [{"hooks": [{"type": "command", "command":
        "python -m covasant_partyline.hook --url <app-url> --profile dev-eastus2 --owner alan --repo <repo>"}]}]
    }

The owner/repo pair highlights messages addressed to this repo's agents
(agent ids follow the <owner>_<repo>_<n> convention).

Every flag falls back to the shared resolvers in cli.py — env → `.partyline/config.json` →
default — so a clone carrying its identity in config needs no flags at all. Reading identity
from the environment *only* was the #1545 regression, and here it did more than silence the
digest: with agent_id="" the self-claim filter in territory_warning can never match, so the
hook warned you off your own claimed directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from . import cli

WINDOW_HOURS = 24
MAX_SHOWN = 10


def _token(profile: str | None) -> str | None:
    if os.getenv("PARTYLINE_TOKEN"):
        return os.environ["PARTYLINE_TOKEN"]
    if not profile:
        return None  # local app — no profile to mint against, and none needed
    exe = shutil.which("databricks")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "auth", "token", "-p", profile],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        return json.loads(out).get("access_token")
    except Exception:
        return None


def _is_path_target(target: str) -> bool:
    """True for a filesystem path, False for a work-item URI (issue:/pr:).

    Work items are claims on *work*, not territory — being 'inside' one is meaningless.
    """
    return target.startswith("/") or (len(target) > 1 and target[1] == ":" and "/" in target)


def _overlaps(a: str, b: str) -> bool:
    """True when a and b are the same directory or one contains the other.

    The appended '/' makes this a real path-boundary test: /a/foo and /a/foobar are
    unrelated siblings, not an overlap.
    """
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def territory_warning(claims: list, cwd: str, agent_id: str) -> str:
    """Render a warning when `cwd` sits inside another agent's claimed subtree.

    Read-only and advisory: this catches the agents that never declared a workdir, which
    is exactly the population that collides.
    """
    hits = []
    for c in claims:
        path = c.get("resource_path") or ""
        if not _is_path_target(path):
            continue
        if c.get("agent_id") == agent_id:
            continue
        if _overlaps(cwd, path):
            hits.append(c)
    if not hits:
        return ""
    lines = ["", "## ⚠ Partyline: you are inside another agent's territory", ""]
    for c in hits:
        reason = f" — {c['reason']}" if c.get("reason") else ""
        lines.append(f"- `{c['resource_path']}` is held by **{c['agent_id']}**{reason}")
    lines += [
        "",
        "Two agents must never work the same directory. Ask the holder before editing "
        "(`partyline post <you> --type question --recipient <holder>`), or work elsewhere.",
        "",
    ]
    return "\n".join(lines)


def _get(url: str, token: str | None) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main() -> None:
    # Hook output is read as UTF-8 by Claude Code; Windows consoles default to cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="covasant_partyline.hook")
    p.add_argument("--url", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--owner", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--agent-id", dest="agent_id", default=None)
    a = p.parse_args()

    # One resolution chain, shared with cli/channel/outbox/check_unread (cli.resolve_*):
    # flag → env → .partyline/config.json → default. Resolving from env alone was the #1545
    # regression, and here it is worse than silence: with agent_id="" the self-claim filter
    # in territory_warning can never match, so the hook warns you off your own territory.
    cfg = cli._load_config()
    base = cli.resolve_url(a.url, cfg=cfg)
    agent_id = cli.resolve_agent_id(a.agent_id, cfg=cfg) or ""
    if not base:
        return

    token = _token(cli.resolve_profile(a.profile, url=base, cfg=cfg))
    if not token and not cli.is_local_url(base):
        return  # remote app without a token would only 401; stay silent

    try:
        agents = _get(f"{base}/api/agents", token)
        messages = _get(f"{base}/api/messages?after_id=0&limit=500", token)
        claims = _get(f"{base}/api/claims", token)
    except Exception:
        return

    # Territory check first: being inside another agent's subtree is the one thing worth
    # interrupting for, and it must be visible before any traffic digest.
    try:
        from .targets import canonicalize

        warning = territory_warning(claims, canonicalize(".") or "", agent_id)
    except Exception:
        warning = ""  # fail-silent: a broken hook must never block a session
    if warning:
        print(warning)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    def recent(m: dict) -> bool:
        try:
            return datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")) >= cutoff
        except Exception:
            return False

    msgs = [m for m in messages if recent(m)][-MAX_SHOWN:]
    if not msgs and not agents:
        return

    prefix = f"{a.owner}_{a.repo}" if a.owner and a.repo else None
    lines = ["## Partyline (inter-agent line) — recent traffic"]
    if agents:
        lines.append("Active agents: " + ", ".join(
            f"{x['agent_id']} ({x.get('repo') or '?'})" for x in agents))
    for m in msgs:
        to = f" -> {m['recipient']}" if m.get("recipient") else ""
        star = ""
        if prefix and (str(m.get("recipient") or "").startswith(prefix)
                       or any(str(x).startswith(prefix) for x in m.get("mentions") or [])):
            star = " [ADDRESSED TO THIS REPO'S AGENTS]"
        lines.append(
            f"- #{m['id']} [{m['message_type']}] {m['agent_id']}{to}: "
            f"{(m.get('payload') or '')[:160]}{star}"
        )
    lines.append(
        "Register on the line (register_agent) before posting/claiming; "
        "check get_unread(only_for_me=true) for anything addressed to you."
    )
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break a session over a status hook
    sys.exit(0)
