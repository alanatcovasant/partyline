"""Command Code session digest and Stop gate, using the clone's Partyline identity."""

from __future__ import annotations

import io
import json
import os
import sys


def main() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name") or os.getenv("COMMANDCODE_HOOK_EVENT")
    if event == "SessionStart":
        from . import hook

        os.environ["PARTYLINE_HOOK_FORMAT"] = "cmdc"
        sys.argv = ["covasant_partyline.hook"]
        hook.main()
    elif event == "Stop":
        from . import check_unread

        os.environ["PARTYLINE_HOOK_FORMAT"] = "cmdc_stop"
        sys.stdin = io.StringIO(raw)
        check_unread.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # A connection or hook failure must never prevent a turn from finishing.
    sys.exit(0)
