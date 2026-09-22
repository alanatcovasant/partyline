#!/usr/bin/env python3
"""Antigravity PreInvocation shim — Partyline digest on turn 0, unread peek after.

Antigravity has no session-start event (its hooks are PreToolUse / PostToolUse /
PreInvocation / PostInvocation / Stop), but the PreInvocation payload carries
`invocationNum`, and invocation 0 *is* the first turn of a conversation. So this one
hook covers both roles every other harness in this repo wires separately:

    invocationNum == 0  -> covasant_partyline.hook          (recent line traffic digest)
    invocationNum >  0  -> covasant_partyline.check_unread  (silent unless mail for you)

An unreadable payload falls through to the peek, which is the safe default: it prints
nothing when the line is quiet, whereas a stray digest would repeat mid-conversation.

PARTYLINE_HOOK_FORMAT=antigravity makes both modules emit Antigravity's
{"injectSteps": [{"ephemeralMessage": "..."}]} envelope — plain stdout is not injected.

url / profile / agent id resolve from the gitignored .partyline/config.json via the
shared cli.resolve_* chain, so nothing clone-specific is hardcoded here (see #1545).

Fail-silent by contract: a broken hook must never block a turn.
"""

from __future__ import annotations

import json
import os
import sys

os.environ["PARTYLINE_HOOK_FORMAT"] = "antigravity"


def _invocation_num() -> int | None:
    """Read Antigravity's PreInvocation payload; also drains stdin so the runner does
    not see a broken pipe. Returns None when the payload is missing or unparseable."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("invocationNum"), int):
        return payload["invocationNum"]
    return None


if __name__ == "__main__":
    try:
        if _invocation_num() == 0:
            from covasant_partyline.hook import main

            # Rebuild argv so hook.main's argparse does not see this shim's path.
            sys.argv = [
                "covasant_partyline.hook",
                "--owner",
                "alan",
                "--repo",
                "covasant-partyline",
            ]
            main()
        else:
            from covasant_partyline.check_unread import main

            main()
    except Exception:
        pass  # never break a turn over a status hook
    sys.exit(0)
