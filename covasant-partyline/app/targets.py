"""Claim-target parsing and validation (2026-08-01 claim-semantics design).

A claim target is one of three things:

  * a canonical absolute filesystem path — the PHYSICAL unit: "nobody else writes these
    bytes on this disk";
  * a work-item URI (issue:owner/repo#N, pr:owner/repo#N) — the TASK unit;
  * a source ref (src:owner/repo[/relpath]) — the LOGICAL unit: "nobody else edits this
    source, in any clone or worktree on any machine".

The third exists because the first cannot express it. Two clones of one repo
(…/aura_2/pkg and …/aura_3/pkg) are different paths, so path claims let two agents edit
the same file and only discover it at merge time. See docs/adr/2026-08-10-*.

The server cannot see the filesystem, so it never repairs a target — it validates and
refuses with a hint, and the CLI (which can call realpath) canonicalizes before sending.
Pure and dependency-free so it unit-tests without Postgres.

Mirrored by `covasant_partyline.targets` in the client package: the two are separate
deployables with no shared module, so the grammar is stated twice on purpose. Keep them
in sync.
"""

from __future__ import annotations

import re

WORK_ITEM_RE = re.compile(r"^(issue|pr):[a-z0-9._-]+/[a-z0-9._-]+#[0-9]+$")
# src:owner/repo, optionally followed by a repo-relative path. The owner/repo prefix is
# lowercase (GitHub is case-insensitive there); the relpath is NOT case-folded, because
# git is case-sensitive and App.tsx is a different file from app.tsx.
SOURCE_REF_RE = re.compile(r"^src:[a-z0-9._-]+/[a-z0-9._-]+(/.+)?$")
# Case-INSENSITIVE on purpose: 'C:/x' is a Windows-rooted path that happens to be
# mis-cased. Detecting it only in lowercase would report it as "not absolute", which is
# both wrong and unactionable — the caller needs to be told to lowercase it.
_WINDOWS_ROOT_RE = re.compile(r"^[A-Za-z]:/")
_WORK_ITEM_SCHEMES = ("issue", "pr")
_SOURCE_SCHEME = "src"


class InvalidTarget(ValueError):
    """A claim target that is not canonical. Carries a fixable hint for the caller."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(f"{message} — {hint}")
        self.message = message
        self.hint = hint


def is_work_item(target: str) -> bool:
    return target.split(":", 1)[0] in _WORK_ITEM_SCHEMES if ":" in target else False


def is_source_ref(target: str) -> bool:
    return target.split(":", 1)[0] == _SOURCE_SCHEME if ":" in target else False


def _validate_source_ref(target: str) -> None:
    """Validate src:owner/repo[/relpath].

    Deliberately branch-free. Claims key on the target alone (repo/branch are display
    metadata — see the 2026-08-01 migration note in db.py), and a branch in the key would
    also break subtree containment: branch names legally contain '/', so
    src:o/r@feature/x/pkg has no unambiguous boundary between branch and path. Two agents
    rewriting the same file on two branches is duplicated work whichever way it merges,
    so conflicting regardless of branch is the behavior we want anyway.
    """
    segments = target.partition(":")[2].split("/")
    owner_repo = "/".join(segments[:2])
    relpath = "/".join(segments[2:])

    # Check case before shape, so a mis-cased owner/repo gets the fix it needs ("lowercase
    # it") rather than the generic "malformed" that the lowercase-only regex would give.
    if owner_repo != owner_repo.lower():
        raise InvalidTarget(
            f"owner/repo is not case-folded: {target!r}",
            f"lowercase the owner/repo, e.g. src:{owner_repo.lower()}"
            + (f"/{relpath}" if relpath else ""),
        )
    if not SOURCE_REF_RE.match(target):
        raise InvalidTarget(
            f"malformed source ref: {target!r}",
            "expected src:owner/repo or src:owner/repo/path/inside/repo, "
            "with owner/repo lowercase",
        )
    if not relpath:
        return  # whole-repo claim

    if "\\" in relpath:
        raise InvalidTarget(
            f"backslash in source ref: {target!r}",
            f"use forward slashes, e.g. src:{owner_repo}/pkg/module.py",
        )
    if relpath.startswith("/") or _WINDOWS_ROOT_RE.match(relpath):
        raise InvalidTarget(
            f"absolute path inside source ref: {target!r}",
            "the part after owner/repo is REPO-relative — claim the absolute path "
            "directly if you meant a directory on this machine",
        )
    if relpath.endswith("/"):
        raise InvalidTarget(f"trailing slash: {target!r}", "drop the trailing slash")
    if "//" in relpath:
        raise InvalidTarget(f"empty path segment in {target!r}", "collapse repeated slashes")
    if "." in relpath.split("/") or ".." in relpath.split("/"):
        raise InvalidTarget(
            f"unresolved path segment in {target!r}",
            "resolve '.' and '..' — a source ref is the path git reports, nothing else",
        )


def validate_target(target: str) -> None:
    """Raise InvalidTarget unless `target` is canonical: path, work item, or source ref."""
    if not target:
        raise InvalidTarget(
            "empty target",
            "pass an absolute path, issue:owner/repo#N, or src:owner/repo/path",
        )

    if is_work_item(target):
        if not WORK_ITEM_RE.match(target):
            raise InvalidTarget(
                f"malformed work-item URI: {target!r}",
                "expected issue:owner/repo#123 or pr:owner/repo#123, lowercase",
            )
        return

    if is_source_ref(target):
        _validate_source_ref(target)
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
    resource_type='file' explicitly. Source refs are 'source' whether they name a
    directory or a file — the distinction is the repo's, not ours.
    """
    if is_work_item(target):
        return target.split(":", 1)[0]
    if is_source_ref(target):
        return "source"
    return "directory"
