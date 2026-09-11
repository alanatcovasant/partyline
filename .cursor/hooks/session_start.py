#!/usr/bin/env python3
"""Cursor sessionStart shim — Partyline traffic digest.

Sets PARTYLINE_HOOK_FORMAT=cursor so covasant_partyline.hook emits Cursor's
{"additional_context": "..."} envelope (not Claude's hookSpecificOutput).
Identity/url/profile resolve from gitignored .partyline/config.json.
"""

from __future__ import annotations

import os
import sys

os.environ["PARTYLINE_HOOK_FORMAT"] = "cursor"

# Drain Cursor's sessionStart stdin (session_id, composer_mode, …) so the runner
# does not see a broken pipe; the digest module does not need those fields.
try:
    sys.stdin.read()
except Exception:
    pass

from covasant_partyline.hook import main

if __name__ == "__main__":
    # Rebuild argv so argparse in hook.main does not see this shim's path.
    sys.argv = [
        "covasant_partyline.hook",
        "--url",
        "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com",
        "--profile",
        "dev-eastus2",
        "--owner",
        "alan",
        "--repo",
        "covasant-partyline",
    ]
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
