"""Kimi `Stop` hook for the config block that `kimi-pl` installs.

Kimi never feeds a hook's stdout to the model; the only hook that can reach it is a `Stop` hook
that denies, which re-runs the turn with the reason as a user message. So this peeks for mail
addressed to this agent (deduplicated, because the peek never acks) and denies the stop with it.
Identity comes from the nearest `.partyline/config.json`. Fail-silent: a broken hook must never
block a turn.
"""

from __future__ import annotations

import os
import sys

os.environ["PARTYLINE_HOOK_FORMAT"] = "kimi_stop"


def main() -> None:
    from . import check_unread

    check_unread.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
