"""Partyline unread-peek hook (plugin entry point).

Thin shim: the real logic lives in the installed `covasant_partyline` package
(`covasant_partyline.check_unread`), so the peek behaves identically whether it's invoked
as `python -m covasant_partyline.check_unread` (project settings.json) or by file path via
`${CLAUDE_PLUGIN_ROOT}/hooks/check_unread.py` (this plugin's hooks.json). The plugin
already requires the package (its .mcp.json runs `python -m covasant_partyline.channel`),
so delegating here adds no new dependency and keeps a single source of truth.

Fail-silent by contract — a broken hook must never block a turn.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    try:
        from covasant_partyline.check_unread import main
        main()
    except Exception:
        pass  # never break a turn over a status hook
    sys.exit(0)
