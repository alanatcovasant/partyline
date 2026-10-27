"""Antigravity PreInvocation hook: line digest on turn 0, unread peek after.

The packaged twin of the repo's `.agents/agy_check_unread.py`, so a global hook in
`~/.gemini/config/hooks.json` works from any directory rather than only where that
relative script exists. Same contract: invocation 0 prints the recent-traffic digest, later
invocations print only mail addressed to this agent, as Antigravity's
`{"injectSteps": [{"ephemeralMessage": ...}]}` envelope. Identity comes from the nearest
`.partyline/config.json`. Fail-silent: a broken hook must never block a turn.
"""

from __future__ import annotations

import json
import os
import sys

os.environ["PARTYLINE_HOOK_FORMAT"] = "antigravity"


def _invocation_num() -> int | None:
    """Antigravity's PreInvocation payload; also drains stdin so the runner sees no broken pipe."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("invocationNum"), int):
        return payload["invocationNum"]
    return None


def main() -> None:
    if _invocation_num() == 0:
        from . import hook

        sys.argv = ["covasant_partyline.hook"]  # hook.main parses argv; don't hand it ours
        hook.main()
    else:
        from . import check_unread

        check_unread.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
