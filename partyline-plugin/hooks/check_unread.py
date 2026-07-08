"""Partyline lifecycle hook: inject messages addressed to this agent as context.

Runs on SessionStart / UserPromptSubmit in both VS Code and Claude Code (both honor the
`hookSpecificOutput.additionalContext` output schema). Fail-silent by contract — a broken
hook must never block a turn, so every error path exits 0 with no output. Prints nothing
when there's nothing addressed to you.

Reads configuration from the environment (a distributable plugin can't hardcode identity):
  PARTYLINE_URL       base app URL (required; no output without it)
  PARTYLINE_AGENT_ID  your agent id on the line (required)
  PARTYLINE_TOKEN     bearer token (optional; else acquired via the databricks CLI)
  PARTYLINE_PROFILE   databricks CLI profile for token acquisition (default dev-eastus2)

This is a peek — it does NOT advance your checkpoint, so unread stays visible until you
ack it via the MCP tools. It intentionally does not run the wake watcher (hooks are
synchronous and time-bounded); arm the watcher per the using-partyline skill for a real
mid-session wake on Claude Code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

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
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        return json.loads(out).get("access_token")
    except Exception:
        return None


def _emit(context: str) -> None:
    # Portable across VS Code and Claude Code hook runners.
    print(json.dumps({"hookSpecificOutput": {"additionalContext": context}}))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Drain stdin (hook payload); we don't need its fields, but read it so the runner
    # doesn't see a broken pipe.
    try:
        sys.stdin.read()
    except Exception:
        pass

    url = os.getenv("PARTYLINE_URL", "").rstrip("/")
    agent_id = os.getenv("PARTYLINE_AGENT_ID", "")
    if not url or not agent_id:
        return

    token = _token(os.getenv("PARTYLINE_PROFILE", "dev-eastus2"))
    if not token:
        return

    path = f"/api/agents/{urllib.parse.quote(agent_id)}/unread"
    query = urllib.parse.urlencode({"only_for_me": "true", "limit": 50})
    req = urllib.request.Request(
        f"{url}{path}?{query}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            msgs = json.loads(r.read())
    except Exception:
        return

    if not msgs:
        return

    shown = msgs[-MAX_SHOWN:]
    lines = [
        f"## Partyline — {len(msgs)} message(s) addressed to you (`{agent_id}`)",
        "Respond on the line with the Partyline MCP tools (`post_message`, "
        "`claim_resource`, …) and `ack_checkpoint` when handled.",
    ]
    for m in shown:
        frm = m.get("agent_id", "?")
        typ = m.get("message_type", "message")
        payload = (m.get("payload") or "").replace("\n", " ")[:200]
        lines.append(f"- #{m.get('id')} [{typ}] from {frm}: {payload}")
    _emit("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break a turn over a status hook
    sys.exit(0)
