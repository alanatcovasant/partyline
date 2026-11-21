"""Genie Code CLI session digest, unread peek, and Stop gate.

Genie's hook contract is Claude-compatible (verified on 0.4.1): hooks.json entries fire
per event, the stdin payload carries `hook_event_name`, context injects via
`hookSpecificOutput.additionalContext` — but only when `hookEventName` is echoed back,
the same quirk Qwen Code has, so both context paths reuse the `qwen` emit format. The
Stop gate takes `{"decision": "block", "reason": ...}` and the payload carries the same
`stop_hook_active` retry flag as Command Code/Devin, so `devin_stop` is reused verbatim.

Hooks are installed user-wide in $GENIE_HOME/hooks.json by `genie-pl`; each event maps
to this module and the dispatch below picks the right behaviour.
"""

from __future__ import annotations

import io
import json
import os
import sys


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        from . import hook

        os.environ["PARTYLINE_HOOK_FORMAT"] = "qwen"
        sys.argv = ["covasant_partyline.hook"]
        hook.main()
    elif event in ("UserPromptSubmit", "Stop"):
        from . import check_unread

        os.environ["PARTYLINE_HOOK_FORMAT"] = "qwen" if event == "UserPromptSubmit" else "devin_stop"
        sys.stdin = io.StringIO(raw)
        check_unread.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # A hook failure must never break the agent's turn.
    sys.exit(0)
