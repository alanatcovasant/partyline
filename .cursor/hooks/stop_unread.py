#!/usr/bin/env python3
"""Cursor stop shim — wake the agent when Partyline mail is waiting.

Cursor's beforeSubmitPrompt cannot inject context (only continue / user_message).
The stop hook can auto-submit a followup_message, which is the closest native
idle-adjacent wake: when a turn completes and unread mail exists, Cursor starts
a follow-up turn with the peek body. Quiet line → silent (no follow-up).

loop_limit is set in .cursor/hooks.json (default here: 3) so a stubborn inbox
cannot spin forever.
"""

from __future__ import annotations

import os
import sys

os.environ["PARTYLINE_HOOK_FORMAT"] = "cursor_stop"

from covasant_partyline.check_unread import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
