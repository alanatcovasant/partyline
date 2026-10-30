"""Devin CLI session digest and Stop gate; global hooks resolve identity via DEVIN_PROJECT_DIR."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path


def main() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    # Devin sets DEVIN_PROJECT_DIR to the session's project root; the user-level config
    # hooks otherwise run from wherever Devin started, which is not where the clone's
    # .partyline/config.json lives.
    root = os.getenv("DEVIN_PROJECT_DIR")
    old_cwd = None
    if isinstance(root, str) and Path(root).is_absolute() and Path(root).is_dir():
        old_cwd = os.getcwd()
        os.chdir(root)
    try:
        if event == "SessionStart":
            from . import hook

            os.environ["PARTYLINE_HOOK_FORMAT"] = "devin"
            sys.argv = ["covasant_partyline.hook"]
            hook.main()
        elif event == "Stop":
            from . import check_unread

            os.environ["PARTYLINE_HOOK_FORMAT"] = "devin_stop"
            sys.stdin = io.StringIO(raw)
            check_unread.main()
    finally:
        if old_cwd:
            os.chdir(old_cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # A hook failure must never break the agent's turn.
    sys.exit(0)
