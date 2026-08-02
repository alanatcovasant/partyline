"""Claim-target parsing and validation (2026-08-01 claim-semantics design).

A claim target is EITHER a canonical absolute filesystem path OR a work-item URI
(issue:owner/repo#N, pr:owner/repo#N). The server cannot see the filesystem, so it never
repairs a target — it validates and refuses with a hint, and the CLI (which can call
realpath) canonicalizes before sending. Pure and dependency-free so it unit-tests without
Postgres.

Mirrored by `covasant_partyline.targets` in the client package: the two are separate
deployables with no shared module, so the work-item grammar is stated twice on purpose.
Keep them in sync.
"""

from __future__ import annotations

import re

WORK_ITEM_RE = re.compile(r"^(issue|pr):[a-z0-9._-]+/[a-z0-9._-]+#[0-9]+$")
# Case-INSENSITIVE on purpose: 'C:/x' is a Windows-rooted path that happens to be
# mis-cased. Detecting it only in lowercase would report it as "not absolute", which is
# both wrong and unactionable — the caller needs to be told to lowercase it.
_WINDOWS_ROOT_RE = re.compile(r"^[A-Za-z]:/")
_WORK_ITEM_SCHEMES = ("issue", "pr")


class InvalidTarget(ValueError):
    """A claim target that is not canonical. Carries a fixable hint for the caller."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(f"{message} — {hint}")
        self.message = message
        self.hint = hint


def is_work_item(target: str) -> bool:
    return target.split(":", 1)[0] in _WORK_ITEM_SCHEMES if ":" in target else False


def validate_target(target: str) -> None:
    """Raise InvalidTarget unless `target` is a canonical work-item URI or absolute path."""
    if not target:
        raise InvalidTarget("empty target", "pass an absolute path or issue:owner/repo#N")

    if is_work_item(target):
        if not WORK_ITEM_RE.match(target):
            raise InvalidTarget(
                f"malformed work-item URI: {target!r}",
                "expected issue:owner/repo#123 or pr:owner/repo#123, lowercase",
            )
        return

    if "\\" in target:
        raise InvalidTarget(
            f"backslash in path: {target!r}",
            "use forward slashes, e.g. c:/users/you/code/repo/pkg",
        )

    windows_rooted = bool(_WINDOWS_ROOT_RE.match(target))
    if not windows_rooted and not target.startswith("/"):
        raise InvalidTarget(
            f"path is not absolute: {target!r}",
            "claim the full filesystem path, not a repo-relative one — "
            "the CLI resolves it for you: `partyline claim <agent> <repo> .`",
        )
    if target != "/" and target.endswith("/"):
        raise InvalidTarget(f"trailing slash: {target!r}", "drop the trailing slash")
    if "//" in target:
        raise InvalidTarget(f"empty path segment in {target!r}", "collapse repeated slashes")

    segments = target.split("/")
    if "." in segments or ".." in segments:
        raise InvalidTarget(
            f"unresolved path segment in {target!r}",
            "resolve '.' and '..' before claiming (the CLI calls realpath)",
        )
    if windows_rooted and target != target.lower():
        raise InvalidTarget(
            f"windows path is not case-folded: {target!r}",
            "lowercase windows paths — otherwise two spellings hold the same directory",
        )


def infer_resource_type(target: str) -> str:
    """Default resource_type for a target. Paths default to 'directory'.

    The server cannot stat the path, so it cannot tell a file from a directory. The
    documented unit is the directory; a caller who really means a file passes
    resource_type='file' explicitly.
    """
    if is_work_item(target):
        return target.split(":", 1)[0]
    return "directory"
