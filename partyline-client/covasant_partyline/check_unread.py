"""Partyline lifecycle hook: inject messages addressed to this agent as context.

Runs on SessionStart / UserPromptSubmit in VS Code, Claude Code, Qwen Code, and Cline,
and on Cursor's `stop` (and optional `sessionStart`) via `PARTYLINE_HOOK_FORMAT=cursor` /
`cursor_stop`. VS Code and Claude Code honor the `hookSpecificOutput.additionalContext`
output schema; Qwen Code honors the same envelope but *requires* `hookEventName` inside
`hookSpecificOutput` — set `PARTYLINE_HOOK_FORMAT=qwen` (the `.qwen/settings.json`
entries in this repo do) to emit it. Cline honors
`{"cancel": false, "contextModification": "..."}` (selected by setting
`PARTYLINE_HOOK_FORMAT=cline`, which the Cline `.ps1` shims do). Cursor injects via a
top-level `additional_context` field (`cursor`) or a `stop`-hook `followup_message`
(`cursor_stop`) — see `.cursor/hooks.json`. Fail-silent by contract — a broken hook must
never block a turn, so every error path exits 0 with no output. Prints nothing when
there's nothing addressed to you.

Invoke as a module so it resolves regardless of the process CWD (a relative script path
breaks the moment a session runs from another repo):

    python -m covasant_partyline.check_unread

Configuration resolves through the shared resolvers in cli.py — flag/env → per-clone
`.partyline/config.json` → default — so a clone that carries its identity in config (the
post-#1545 shape, keeping a per-clone id out of a tracked .mcp.json) is peeked correctly:

  PARTYLINE_URL       base app URL       (else config `url`, else http://localhost:8000)
  PARTYLINE_AGENT_ID  your agent id      (else config `agentId`/`agent_id`; required)
  PARTYLINE_TOKEN     bearer token       (optional; else minted via the databricks CLI)
  PARTYLINE_PROFILE   databricks profile (else config `databricksProfile`, else dev-eastus2;
                      unused for a local app, which needs no token)

Reading these from the environment *only* was the #1545 regression that left registered,
heartbeating agents silently deaf: identity resolved to "", the guard below returned, and
the output was byte-identical to an empty inbox. Resolve through cli, never os.getenv.

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

from . import cli

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
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        return json.loads(out).get("access_token")
    except Exception:
        return None


def _emit(context: str, hook_event_name: str = "UserPromptSubmit") -> None:
    fmt = os.getenv("PARTYLINE_HOOK_FORMAT") or ""
    # Cline hooks read a JSON envelope on stdout: {"cancel": false, "contextModification": "..."}.
    if fmt == "cline":
        print(json.dumps({"cancel": False, "contextModification": context}))
        return
    # Qwen Code reads the same JSON envelope as VS Code / Claude Code, but requires
    # hookEventName inside hookSpecificOutput — echo the one we were invoked with.
    if fmt == "qwen":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": context}}))
        return
    # Antigravity injects context only through PreInvocation's injectSteps envelope;
    # ephemeralMessage keeps the peek out of the transcript (userMessage would read back
    # as the human having asked for it).
    if fmt == "antigravity":
        print(json.dumps({"injectSteps": [{"ephemeralMessage": context}]}))
        return
    # Cursor Agent: top-level additional_context (sessionStart / postToolUse).
    if fmt == "cursor":
        print(json.dumps({"additional_context": context}))
        return
    # Cursor Agent stop hook: auto-submit a follow-up turn when something is addressed to you.
    if fmt == "cursor_stop":
        print(json.dumps({"followup_message": context}))
        return
    # Kimi Code appends raw stdout to context, so emit plain markdown for it.
    if os.getenv("KIMI_CODE_HOME") or os.getenv("KIMI_PLUGIN_ROOT"):
        print(context)
        return
    # Portable across VS Code and Claude Code hook runners.
    print(json.dumps({"hookSpecificOutput": {"additionalContext": context}}))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Drain stdin (hook payload) so the runner doesn't see a broken pipe. The Qwen Code
    # format additionally echoes hook_event_name back inside hookSpecificOutput. Cursor's
    # stop hook payload carries status + loop_count — only auto-follow up on a clean
    # completion (Cursor ignores followup_message otherwise).
    hook_event_name = "UserPromptSubmit"
    cursor_stop_ok = True
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                hook_event_name = payload.get("hook_event_name") or hook_event_name
                if (os.getenv("PARTYLINE_HOOK_FORMAT") or "") == "cursor_stop":
                    status = payload.get("status") or "completed"
                    cursor_stop_ok = status == "completed"
        except ValueError:
            pass
    except Exception:
        pass

    if (os.getenv("PARTYLINE_HOOK_FORMAT") or "") == "cursor_stop" and not cursor_stop_ok:
        return

    # One resolution chain, shared with cli/channel/outbox/hook (cli.resolve_*): flag/env →
    # .partyline/config.json → default. Resolving from env alone here is the #1545 bug.
    cfg = cli._load_config()
    url = cli.resolve_url(cfg=cfg)
    agent_id = cli.resolve_agent_id(cfg=cfg)
    if not url or not agent_id:
        return

    token = _token(cli.resolve_profile(url=url, cfg=cfg))
    if not token and not cli.is_local_url(url):
        return  # remote app without a token would only 401; stay silent

    path = f"/api/agents/{urllib.parse.quote(agent_id)}/unread"
    # include_payload=false: this peek only renders the concise summary (falling back to
    # payload when a message has none), so don't pull full payloads over the wire.
    query = urllib.parse.urlencode({"only_for_me": "true", "limit": 50, "include_payload": "false"})
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{url}{path}?{query}", headers=headers)
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
        "Respond on the line with `partyline post` / MCP `post_message` (and "
        "`partyline ack` / `ack_checkpoint` when handled).",
    ]
    for m in shown:
        frm = m.get("agent_id", "?")
        typ = m.get("message_type", "message")
        # Prefer the concise summary; fall back to payload when a message has no summary.
        text = (m.get("summary") or m.get("payload") or "").replace("\n", " ")[:200]
        lines.append(f"- #{m.get('id')} [{typ}] from {frm}: {text}")
    if (os.getenv("PARTYLINE_HOOK_FORMAT") or "") == "cursor_stop":
        lines.insert(
            1,
            "Partyline wake: handle these, ack them, then stop — do not start unrelated work.",
        )
    _emit("\n".join(lines), hook_event_name)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break a turn over a status hook
    sys.exit(0)
