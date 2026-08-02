"""Client-side claim-target normalization.

Only the client can see the filesystem, so only the client can canonicalize a path. The
server validates the result and refuses anything non-canonical — it cannot repair one.
Mirrors the grammar in the server's `app/targets.py`; the two packages are separate
deployables with no shared module, so keep them in sync.
"""

from __future__ import annotations

import os
import re
import sys

WORK_ITEM_RE = re.compile(r"^(issue|pr):[^/\s]+/[^#\s]+#[0-9]+$", re.IGNORECASE)


def canonicalize(value: str) -> str | None:
    """Canonical form of a claim target, or None if a path doesn't exist here.

    Work items are lowercased (GitHub owner/repo are case-insensitive). Paths go through
    realpath — resolving symlinks and Windows junctions and making the path absolute
    against the process CWD — then to forward slashes, and are case-folded on Windows
    because NTFS is case-insensitive and two spellings must not hold the same directory.

    Returns None rather than exiting so non-interactive callers (the fire-and-forget
    bridge outbox) can fall back instead of taking the process down.
    """
    value = value.strip()
    if WORK_ITEM_RE.match(value):
        return value.lower()

    expanded = os.path.expandvars(os.path.expanduser(value))
    if not os.path.exists(expanded):
        return None
    p = os.path.realpath(expanded).replace("\\", "/")
    if os.name == "nt":
        p = p.lower()
    return p.rstrip("/") or "/"


def normalize_target(value: str) -> str:
    """Canonical form of a claim target, or exit(2) with a usable message.

    The CLI front door: an unresolvable path is a user error worth stopping for, and
    stopping here means the bad target never reaches the server at all.
    """
    result = canonicalize(value)
    if result is None:
        sys.exit(
            f"partyline: no such path: {value!r}\n"
            "  Claim a directory that exists on this machine. Paths are filesystem-rooted,\n"
            "  not repo-relative — pass '.' to claim the directory you're standing in."
        )
    return result
