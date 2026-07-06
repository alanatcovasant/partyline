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

WINDOW_HOURS = 24
MAX_SHOWN = 10


def _token(profile: str) -> str | None:
    if os.getenv("PARTYLINE_TOKEN"):
        return os.environ["PARTYLINE_TOKEN"]
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


def _get(url: str, token: str) -> object:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main() -> None:
    # Hook output is read as UTF-8 by Claude Code; Windows consoles default to cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="covasant_partyline.hook")
    p.add_argument("--url", default=os.getenv("PARTYLINE_URL", ""))
    p.add_argument("--profile", default="dev-eastus2")
    p.add_argument("--owner", default="")
    p.add_argument("--repo", default="")
    a = p.parse_args()
    if not a.url:
        return

    token = _token(a.profile)
    if not token:
        return

    try:
        base = a.url.rstrip("/")
        agents = _get(f"{base}/api/agents", token)
        messages = _get(f"{base}/api/messages?after_id=0&limit=500", token)
    except Exception:
        return

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
