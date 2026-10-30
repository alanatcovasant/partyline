"""Global Cursor hooks must resolve Partyline identity in the session's workspace."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path


def main(event: str | None = None) -> None:
    event = event or (sys.argv[1] if len(sys.argv) > 1 else "")
    raw = sys.stdin.read()
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        return
    roots = payload.get("workspace_roots")
    root = roots[0] if isinstance(roots, list) and roots else os.getenv("CURSOR_PROJECT_DIR")
    if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
        return
    old_cwd, old_argv, old_stdin = os.getcwd(), sys.argv, sys.stdin
    try:
        os.chdir(root)
        if event == "sessionStart":
            from . import hook

            os.environ["PARTYLINE_HOOK_FORMAT"] = "cursor"
            sys.argv = ["covasant_partyline.hook"]
            hook.main()
        elif event == "stop":
            from . import check_unread

            os.environ["PARTYLINE_HOOK_FORMAT"] = "cursor_stop"
            sys.stdin = io.StringIO(raw)
            check_unread.main()
    finally:
        os.chdir(old_cwd)
        sys.argv, sys.stdin = old_argv, old_stdin


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # A hook failure must never break the agent's turn.
    sys.exit(0)
