# Claim Semantics: Absolute-Path Subtrees + Work-Item Claims — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Partyline claim actually exclude — key it on a normalized absolute path or work-item URI, refuse overlapping subtrees, and make territory a side effect of registering rather than something an agent must remember.

**Architecture:** `resource_path` becomes the sole conflict key, holding either a canonical absolute path or an `issue:`/`pr:` URI. Conflict is subtree overlap evaluated under one fixed advisory lock inside a transaction, replacing the `INSERT … ON CONFLICT` on `(repo, branch, resource_path)`. Normalization happens client-side in the CLI (only it can call `realpath`); the server validates and refuses non-canonical input. A new `claim_targets` primitive leases a work item and its directories all-or-nothing, and `register_agent` uses it to take `--workdir` subtrees.

**Tech Stack:** Python 3.11 / FastAPI / psycopg3 / Postgres (Lakebase) / FastMCP / React+TypeScript / pytest.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-01-claim-semantics-absolute-path-subtree-design.md`. Read it before Task 1.
- **No manual steps.** Everything ships through `./deploy.sh <profile> <target>`. Never add a "click this in the UI" step.
- **Owner-only DDL goes in `_MIGRATIONS`** (`covasant-partyline/app/db.py:305`), applied by the privileged `scripts/migrate_schema.py`. The app SP cannot run DDL: `ensure_schema` catches `psycopg.errors.InsufficientPrivilege` and **skips silently** (`db.py:382-390`), so DDL that only runs at app startup will appear to succeed while doing nothing.
- **Workspace profile is `dev-eastus2`.** Always pass `--profile dev-eastus2` / `-p dev-eastus2`.
- **PG-backed tests need a local Postgres:** `docker compose up -d` first. `tests/conftest.py` skips them cleanly when none is listening, so a green run without Postgres proves nothing — confirm the tests actually ran.
- **Never use `LIKE` for path-prefix comparison.** `_` and `%` are legal filename characters and are `LIKE` wildcards; `aura_core` is a real directory in this fleet. Use `starts_with()`.
- **Always append `/` before a prefix comparison.** Without it `/a/foo` conflicts with the unrelated sibling `/a/foobar`.
- **Registration must never fail because of a claim conflict.** An agent that cannot register is invisible to the line; report the conflict, register anyway.
- **Path casing:** Windows-drive-rooted paths are lowercased end to end; POSIX paths are left case-sensitive.
- Verify against the deployed app (`databricks bundle run covasant_partyline`), and clean up any test agents/claims posted to the live line.

---

### Task 1: Server-side claim-target validation

A claim target is either a canonical absolute path or a work-item URI. The server cannot
see the filesystem, so it never repairs a target — it validates and refuses with a hint,
and the CLI is responsible for canonicalizing first. This module is pure so it tests
without Postgres.

**Files:**
- Create: `covasant-partyline/app/targets.py`
- Test: `tests/test_claim_targets.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `InvalidTarget(ValueError)` with `.message: str` and `.hint: str`;
  `is_work_item(target: str) -> bool`; `validate_target(target: str) -> None`;
  `infer_resource_type(target: str) -> str`; `WORK_ITEM_RE: re.Pattern`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_targets.py
"""Pure unit tests for claim-target validation (no Postgres, no network)."""

import pytest

from app.targets import (
    InvalidTarget,
    infer_resource_type,
    is_work_item,
    validate_target,
)


@pytest.mark.parametrize(
    "target",
    [
        "c:/users/alana/code/repos/aura/aura_core",
        "/home/alan/code/aura/aura_core",
        "/",
        "issue:covasant/aura#482",
        "pr:covasant/aura-web#17",
    ],
)
def test_canonical_targets_are_accepted(target):
    validate_target(target)  # must not raise


@pytest.mark.parametrize(
    "target, needle",
    [
        ("db.py", "not absolute"),
        ("app/db.py", "not absolute"),
        ("", "empty"),
        ("C:\\Users\\alana\\code", "backslash"),
        ("C:/Users/alana/code", "case-folded"),
        ("/home/alan/code/", "trailing slash"),
        ("/home/alan/../bob/code", "unresolved path segment"),
        ("/home/alan/./code", "unresolved path segment"),
        ("/home//alan/code", "empty path segment"),
        ("issue:covasant/aura", "malformed work-item"),
        ("issue:aura#482", "malformed work-item"),
    ],
)
def test_non_canonical_targets_are_refused(target, needle):
    with pytest.raises(InvalidTarget) as exc:
        validate_target(target)
    assert needle in exc.value.message
    assert exc.value.hint  # every refusal tells the caller how to fix it


def test_posix_paths_keep_their_case():
    validate_target("/home/Alan/Code")  # case-sensitive filesystem; not an error


def test_work_item_detection_and_type():
    assert is_work_item("issue:covasant/aura#482")
    assert is_work_item("pr:covasant/aura#482")
    assert not is_work_item("/home/alan/code")
    assert infer_resource_type("issue:covasant/aura#482") == "issue"
    assert infer_resource_type("pr:covasant/aura#482") == "pr"
    assert infer_resource_type("/home/alan/code") == "directory"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claim_targets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.targets'`

- [ ] **Step 3: Write the implementation**

```python
# covasant-partyline/app/targets.py
"""Claim-target parsing and validation (2026-08-01 claim-semantics design).

A claim target is EITHER a canonical absolute filesystem path OR a work-item URI
(issue:owner/repo#N, pr:owner/repo#N). The server cannot see the filesystem, so it never
repairs a target — it validates and refuses with a hint, and the CLI (which can call
realpath) canonicalizes before sending. Pure and dependency-free so it unit-tests without
Postgres.

Duplicated in spirit by `covasant_partyline.targets` in the client package: the two are
separate deployables with no shared module, so the work-item grammar is stated twice on
purpose. Keep them in sync.
"""

from __future__ import annotations

import re

WORK_ITEM_RE = re.compile(r"^(issue|pr):[a-z0-9._-]+/[a-z0-9._-]+#[0-9]+$")
_WINDOWS_ROOT_RE = re.compile(r"^[a-z]:/")
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claim_targets.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/targets.py tests/test_claim_targets.py
git commit -m "feat(claims): canonical claim-target validation (absolute path | work-item URI)"
```

---

### Task 2: Subtree-overlap conflict detection + index migration

Replaces exact-string equality with subtree containment, serialized by one advisory lock
so two agents claiming a parent and a child cannot both win.

**Files:**
- Modify: `covasant-partyline/app/db.py` — `_SCHEMA` claims index (`db.py:285-286`),
  `_MIGRATIONS` (`db.py:305`), claims section (`db.py:813-881`)
- Test: `tests/test_claim_overlap.py`

**Interfaces:**
- Consumes: `_CLAIM_COLS` (existing, `db.py:817`), `_heartbeat` (existing, `db.py:429`).
- Produces: `db._CLAIM_LOCK_KEY: int`;
  `db._find_conflict(conn, agent_id: str, resource_path: str) -> dict | None`;
  `db.claim_targets(agent_id, repo, branch, targets: list[tuple[str, str | None]], claim_scope, reason, ttl_seconds) -> tuple[bool, list[dict], dict | None]`;
  `db.claim_resource(...) -> tuple[bool, dict]` (signature unchanged).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_overlap.py
"""Subtree-overlap conflict tests (needs local Postgres: docker compose up -d)."""

import pytest

pytestmark = pytest.mark.anyio


async def _register(client, agent_id):
    r = await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "aura", "branch": "main",
    })
    assert r.status_code == 200


async def _claim(client, agent_id, path):
    return await client.post("/api/claims", json={
        "agent_id": agent_id, "repo": "aura", "resource_path": path,
        "claim_scope": "exclusive", "ttl_seconds": 300,
    })


async def _release(client, agent_id, path):
    await client.post("/api/claims/release", json={
        "agent_id": agent_id, "repo": "aura", "resource_path": path,
    })


async def test_parent_claim_blocks_child(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl1/pkg")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "/t/ovl1/pkg/sub/mod")).json()
    assert refused["acquired"] is False
    assert refused["claim"]["agent_id"] == "ovl_a"
    await _release(client, "ovl_a", "/t/ovl1/pkg")


async def test_child_claim_blocks_parent(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl2/pkg/sub")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "/t/ovl2/pkg")).json()
    assert refused["acquired"] is False
    assert refused["claim"]["agent_id"] == "ovl_a"
    await _release(client, "ovl_a", "/t/ovl2/pkg/sub")


async def test_siblings_do_not_conflict(client):
    """/a/foo and /a/foobar are unrelated — this is why the prefix test appends '/'."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl3/foo")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "/t/ovl3/foobar")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl3/foo")
    await _release(client, "ovl_b", "/t/ovl3/foobar")


async def test_underscore_is_not_a_wildcard(client):
    """LIKE would match aura_core against auraXcore. starts_with must not."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl4/aura_core")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "/t/ovl4/auraXcore/sub")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl4/aura_core")
    await _release(client, "ovl_b", "/t/ovl4/auraXcore/sub")


async def test_agent_may_refine_its_own_territory(client):
    await _register(client, "ovl_a")
    assert (await _claim(client, "ovl_a", "/t/ovl5/pkg")).json()["acquired"] is True
    assert (await _claim(client, "ovl_a", "/t/ovl5/pkg/sub")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl5/pkg")
    await _release(client, "ovl_a", "/t/ovl5/pkg/sub")


async def test_expired_parent_does_not_block(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_a", "repo": "aura", "resource_path": "/t/ovl6/pkg",
        "claim_scope": "exclusive", "ttl_seconds": 1,
    })
    assert r.json()["acquired"] is True
    import anyio
    await anyio.sleep(1.5)
    assert (await _claim(client, "ovl_b", "/t/ovl6/pkg/sub")).json()["acquired"] is True
    await _release(client, "ovl_b", "/t/ovl6/pkg/sub")


async def test_distinct_work_items_do_not_conflict(client):
    """issue #1 must not prefix-match issue #12 — the appended '/' guards this too."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "issue:covasant/aura#1")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "issue:covasant/aura#12")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "issue:covasant/aura#1")).json()
    assert refused["acquired"] is False
    await _release(client, "ovl_a", "issue:covasant/aura#1")
    await _release(client, "ovl_b", "issue:covasant/aura#12")


async def test_concurrent_parent_and_child_have_exactly_one_winner(started_app):
    """The whole reason for the advisory lock: check-then-insert must be atomic.

    Without it both threads read an empty conflict set, both insert non-conflicting keys
    (the unique index is on resource_path, and parent != child), and both believe they
    hold exclusive territory.
    """
    import threading

    from app import db

    db.register_agent("race_a", "test", "aura", "main", None)
    db.register_agent("race_b", "test", "aura", "main", None)
    results: dict[str, bool] = {}
    start = threading.Barrier(2)

    def claim(agent: str, path: str) -> None:
        start.wait()
        results[agent] = db.claim_resource(
            agent, "aura", None, path, "directory", "exclusive", None, 300
        )[0]

    threads = [
        threading.Thread(target=claim, args=("race_a", "/t/race/pkg")),
        threading.Thread(target=claim, args=("race_b", "/t/race/pkg/sub")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results.values()) == 1, f"expected exactly one winner, got {results}"
    db.release_resource("race_a", "aura", None, "/t/race/pkg")
    db.release_resource("race_b", "aura", None, "/t/race/pkg/sub")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose up -d && pytest tests/test_claim_overlap.py -v`
Expected: FAIL — parent/child claims both acquire (no overlap logic yet). Confirm the
tests ran; a "skipped" result means Postgres isn't up and proves nothing.

- [ ] **Step 3: Update the schema index in `_SCHEMA`**

In `covasant-partyline/app/db.py`, replace the claims index (currently `db.py:285-286`):

```python
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_path
    ON claims (resource_path);
```

- [ ] **Step 4: Add the index swap to `_MIGRATIONS`**

Append to the `_MIGRATIONS` list in `db.py` (owner-only DDL — runs via
`scripts/migrate_schema.py`, silently skipped at app startup):

```python
    # 2026-08-01 claim semantics: claims key on the target alone (canonical absolute path
    # or work-item URI). repo/branch stay as display metadata but leave the key — git
    # worktrees put the same (repo, branch, relpath) at two different directories, so
    # they never identified the thing being claimed.
    "DROP INDEX IF EXISTS ux_claims_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_path ON claims (resource_path)",
```

- [ ] **Step 5: Replace the claims implementation**

In `db.py`, replace `claim_resource` (`db.py:820-881`) with the lock key, the conflict
finder, the multi-target primitive, and a thin single-target wrapper:

```python
# One fixed advisory lock serializes every claim attempt fleet-wide. Subtree overlap is
# not expressible as a unique index, so the check-then-insert must be atomic; at this
# volume (tens of live claims, microsecond holds) a single lock is cheaper than any
# partitioned scheme and removes the parent/child race entirely.
_CLAIM_LOCK_KEY = 0x504C434C  # "PLCL"


def _find_conflict(conn, agent_id: str, resource_path: str) -> dict | None:
    """Return a live claim held by *another* agent that overlaps `resource_path`.

    Overlap is subtree containment, not string equality: a claim on a directory conflicts
    with any claim inside it, and vice versa.

    `starts_with`, not LIKE: `_` and `%` are legal in filenames and are LIKE wildcards, so
    LIKE would match `aura_core` against `auraXcore`. The appended '/' makes each
    comparison test a real path boundary, so unrelated siblings (/a/foo vs /a/foobar) do
    not collide — and it also keeps work-item URIs on exact-match semantics, since
    'issue:o/r#1' || '/' is not a prefix of 'issue:o/r#12'.
    """
    return conn.execute(
        f"""SELECT {_CLAIM_COLS} FROM claims
             WHERE expires_at > now()
               AND agent_id <> %(agent)s
               AND (   resource_path = %(p)s
                    OR starts_with(resource_path, %(p)s || '/')
                    OR starts_with(%(p)s, resource_path || '/') )
             LIMIT 1""",
        {"agent": agent_id, "p": resource_path},
    ).fetchone()


def claim_targets(
    agent_id: str,
    repo: str,
    branch: str | None,
    targets: list[tuple[str, str | None]],
    claim_scope: str,
    reason: str | None,
    ttl_seconds: int,
) -> tuple[bool, list[dict], dict | None]:
    """Atomically acquire every target or none (FR5).

    `targets` is a list of (resource_path, resource_type). Returns
    (acquired, claims, blocker); on refusal `claims` is empty and `blocker` is the live
    claim that stopped us. All-or-nothing matters: partial acquisition would leave an
    agent owning the issue while another owns the directory — the exact ambiguity claims
    exist to remove.
    """
    with get_pool().connection() as conn:
        # Transaction-scoped: released at commit or rollback. psycopg3 opens a transaction
        # implicitly (autocommit off), which the surrounding conn.commit() already relies on.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK_KEY,))
        for path, _rtype in targets:
            blocker = _find_conflict(conn, agent_id, path)
            if blocker:
                conn.rollback()
                log.info(
                    "claim_targets REFUSED agent_id=%s target=%s holder=%s",
                    agent_id, path, blocker.get("agent_id"),
                )
                return False, [], blocker

        rows: list[dict] = []
        for path, rtype in targets:
            # Only expired claims or this agent's own can reach ON CONFLICT — _find_conflict
            # already excluded every live claim held by someone else.
            rows.append(
                conn.execute(
                    f"""INSERT INTO claims
                         (agent_id, repo, branch, resource_path, resource_type,
                          claim_scope, reason, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s,
                               now() + make_interval(secs => %s))
                       ON CONFLICT (resource_path) DO UPDATE
                         SET agent_id = EXCLUDED.agent_id,
                             repo = EXCLUDED.repo,
                             branch = EXCLUDED.branch,
                             resource_type = EXCLUDED.resource_type,
                             claim_scope = EXCLUDED.claim_scope,
                             reason = EXCLUDED.reason,
                             created_at = now(),
                             expires_at = EXCLUDED.expires_at
                       RETURNING {_CLAIM_COLS}""",
                    (agent_id, repo, branch, path, rtype, claim_scope, reason, ttl_seconds),
                ).fetchone()
            )
        _heartbeat(conn, agent_id)
        conn.commit()
    log.info("claim_targets ACQUIRED agent_id=%s targets=%s", agent_id, [t[0] for t in targets])
    return True, rows, None


def claim_resource(
    agent_id: str,
    repo: str,
    branch: str | None,
    resource_path: str,
    resource_type: str | None,
    claim_scope: str,
    reason: str | None,
    ttl_seconds: int,
) -> tuple[bool, dict]:
    """Single-target claim. Thin wrapper over claim_targets; signature unchanged."""
    acquired, rows, blocker = claim_targets(
        agent_id, repo, branch, [(resource_path, resource_type)],
        claim_scope, reason, ttl_seconds,
    )
    return (True, rows[0]) if acquired else (False, blocker)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_claim_overlap.py tests/test_rest_api.py -v`
Expected: PASS. `test_rest_api.py::test_claim_and_release` and `::test_claim_conflict`
still use repo-relative paths (`src/auth.py`) and must keep passing here — REST-layer
validation lands in Task 4, which updates them.

- [ ] **Step 7: Commit**

```bash
git add covasant-partyline/app/db.py tests/test_claim_overlap.py
git commit -m "feat(claims): subtree-overlap conflicts under an advisory lock; key on resource_path"
```

---

### Task 3: Workdir claim TTL setting + heartbeat renewal

A session's territory should outlive a task and expire on its own when the session dies —
no session-end hook required.

**Files:**
- Modify: `covasant-partyline/app/config.py:43` (add setting next to `default_claim_ttl_seconds`)
- Modify: `covasant-partyline/app/db.py` — `_heartbeat` (`db.py:429-433`)
- Test: `tests/test_claim_overlap.py` (append)

**Interfaces:**
- Consumes: `db.claim_targets` (Task 2), `settings` (existing import in `db.py`).
- Produces: `settings.workdir_claim_ttl_seconds: int`; `_heartbeat` now also extends
  live `resource_type='workdir'` claims for that agent.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claim_overlap.py
async def test_heartbeat_extends_workdir_claims(client):
    """A live session holds its territory; a dead one loses it one TTL later."""
    await _register(client, "ovl_hb")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_hb", "repo": "aura", "resource_path": "/t/ovl7/wd",
        "resource_type": "workdir", "claim_scope": "exclusive", "ttl_seconds": 60,
    })
    assert r.json()["acquired"] is True
    first_expiry = r.json()["claim"]["expires_at"]

    # Any later call from this agent heartbeats, which re-extends workdir claims.
    await client.post("/api/agents", json={
        "agent_id": "ovl_hb", "owner": "test", "repo": "aura", "branch": "main",
    })
    claims = (await client.get("/api/claims")).json()
    held = next(c for c in claims if c["resource_path"] == "/t/ovl7/wd")
    assert held["expires_at"] > first_expiry  # extended to the workdir TTL, not 60s
    await _release(client, "ovl_hb", "/t/ovl7/wd")


async def test_heartbeat_does_not_extend_ordinary_claims(client):
    await _register(client, "ovl_hb2")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_hb2", "repo": "aura", "resource_path": "/t/ovl8/pkg",
        "resource_type": "directory", "claim_scope": "exclusive", "ttl_seconds": 300,
    })
    first_expiry = r.json()["claim"]["expires_at"]
    await client.post("/api/agents", json={
        "agent_id": "ovl_hb2", "owner": "test", "repo": "aura", "branch": "main",
    })
    claims = (await client.get("/api/claims")).json()
    held = next(c for c in claims if c["resource_path"] == "/t/ovl8/pkg")
    assert held["expires_at"] == first_expiry  # task leases expire on their own schedule
    await _release(client, "ovl_hb2", "/t/ovl8/pkg")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claim_overlap.py -k heartbeat -v`
Expected: FAIL — `assert held["expires_at"] > first_expiry` is False (no renewal yet).

- [ ] **Step 3: Add the setting**

In `covasant-partyline/app/config.py`, directly below `default_claim_ttl_seconds`:

```python
    # A session's territory outlives a single task, so workdir claims get their own,
    # longer TTL. Refreshed on every heartbeat, so a live session holds indefinitely and
    # a dead one releases one TTL later without a session-end hook.
    workdir_claim_ttl_seconds: int = int(os.getenv("PARTYLINE_WORKDIR_TTL", "3600"))
```

- [ ] **Step 4: Extend `_heartbeat`**

Replace `_heartbeat` in `db.py`:

```python
def _heartbeat(conn, agent_id: str) -> None:
    """Refresh presence for an already-registered agent (no-op if unregistered).

    Also re-extends this agent's live `workdir` claims: territory follows the session, so
    an active agent keeps its subtree and a dead one drops it one TTL later. Ordinary
    (task-scoped) claims are untouched — they expire on their own schedule.
    """
    cur = conn.execute("UPDATE agents SET last_heartbeat = now() WHERE agent_id = %s", (agent_id,))
    if cur.rowcount == 0:
        log.warning("Heartbeat ignored for unregistered agent_id=%s", agent_id)
        return
    conn.execute(
        """UPDATE claims
              SET expires_at = now() + make_interval(secs => %s)
            WHERE agent_id = %s AND resource_type = 'workdir' AND expires_at > now()""",
        (settings.workdir_claim_ttl_seconds, agent_id),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_claim_overlap.py -v`
Expected: PASS (all overlap + heartbeat tests)

- [ ] **Step 6: Commit**

```bash
git add covasant-partyline/app/config.py covasant-partyline/app/db.py tests/test_claim_overlap.py
git commit -m "feat(claims): workdir claims renew on heartbeat, expire one TTL after a session dies"
```

---

### Task 4: Wire validation and work-item claims through models + REST

Pydantic validators turn a non-canonical target into an automatic HTTP 422 whose body
names the fix, so an old client gets a loud, self-explaining failure instead of a silent
wrong-key claim.

**Files:**
- Modify: `covasant-partyline/app/models.py:33-36` (ClaimScope area), `:44-51`
  (RegisterAgent), `:120-150` (claim models)
- Modify: `covasant-partyline/app/rest.py:277-312` (claim endpoints)
- Modify: `tests/test_rest_api.py:297-400` (existing claim tests use repo-relative paths)
- Test: `tests/test_claim_rest.py`

**Interfaces:**
- Consumes: `app.targets.validate_target`, `app.targets.infer_resource_type`,
  `app.targets.InvalidTarget` (Task 1); `db.claim_targets` (Task 2).
- Produces: `models.ClaimWorkItemRequest`; `RegisterAgent.workdirs: list[str]`;
  `POST /api/claims/work-item` returning
  `{"acquired": bool, "claims": list, "blocker": dict | None}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_rest.py
"""REST-layer claim validation + work-item claims (needs local Postgres)."""

import pytest

pytestmark = pytest.mark.anyio


async def _register(client, agent_id):
    await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "aura", "branch": "main",
    })


@pytest.mark.parametrize("bad", ["db.py", "app/db.py", "C:\\Users\\x", "/a/b/", "/a/../b"])
async def test_non_canonical_target_is_rejected_with_422(client, bad):
    await _register(client, "rest_v")
    r = await client.post("/api/claims", json={
        "agent_id": "rest_v", "repo": "aura", "resource_path": bad,
    })
    assert r.status_code == 422
    assert "resource_path" in r.text


async def test_work_item_claim_leases_issue_and_paths_together(client):
    await _register(client, "rest_wi")
    r = await client.post("/api/claims/work-item", json={
        "agent_id": "rest_wi", "repo": "aura", "work_item": "issue:covasant/aura#901",
        "paths": ["/t/wi1/pkg", "/t/wi1/other"], "ttl_seconds": 300,
    })
    body = r.json()
    assert body["acquired"] is True
    assert {c["resource_path"] for c in body["claims"]} == {
        "issue:covasant/aura#901", "/t/wi1/pkg", "/t/wi1/other",
    }
    assert next(c for c in body["claims"] if c["resource_path"].startswith("issue:"))[
        "resource_type"] == "issue"
    for p in ("issue:covasant/aura#901", "/t/wi1/pkg", "/t/wi1/other"):
        await client.post("/api/claims/release", json={
            "agent_id": "rest_wi", "repo": "aura", "resource_path": p,
        })


async def test_work_item_claim_is_all_or_nothing(client):
    """If any path is held, nothing is leased — not even the issue."""
    await _register(client, "rest_a")
    await _register(client, "rest_b")
    await client.post("/api/claims", json={
        "agent_id": "rest_a", "repo": "aura", "resource_path": "/t/wi2/taken", "ttl_seconds": 300,
    })
    r = await client.post("/api/claims/work-item", json={
        "agent_id": "rest_b", "repo": "aura", "work_item": "issue:covasant/aura#902",
        "paths": ["/t/wi2/free", "/t/wi2/taken/sub"], "ttl_seconds": 300,
    })
    body = r.json()
    assert body["acquired"] is False
    assert body["blocker"]["agent_id"] == "rest_a"
    claims = (await client.get("/api/claims")).json()
    paths = {c["resource_path"] for c in claims}
    assert "issue:covasant/aura#902" not in paths  # the issue was NOT leased
    assert "/t/wi2/free" not in paths
    await client.post("/api/claims/release", json={
        "agent_id": "rest_a", "repo": "aura", "resource_path": "/t/wi2/taken",
    })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claim_rest.py -v`
Expected: FAIL — 422 tests get 200; work-item tests get 404 (no endpoint).

- [ ] **Step 3: Update the models**

In `covasant-partyline/app/models.py`, add the import and validators:

```python
from pydantic import BaseModel, Field, field_validator

from .targets import InvalidTarget, infer_resource_type, validate_target


def _canonical(value: str) -> str:
    """Reusable validator body: refuse a non-canonical claim target.

    Raising ValueError makes FastAPI answer 422 with the hint in the response body — an
    old client sending repo-relative paths fails loudly and self-explainingly rather than
    quietly claiming a key nobody else will match.
    """
    try:
        validate_target(value)
    except InvalidTarget as exc:
        raise ValueError(f"{exc.message} — {exc.hint}") from exc
    return value
```

Add to `ClaimRequest` (currently `models.py:120-131`):

```python
    @field_validator("resource_path")
    @classmethod
    def _check_resource_path(cls, v: str) -> str:
        return _canonical(v)
```

Add the same validator to `ReleaseRequest.resource_path`. Update
`ClaimRequest.resource_type`'s description to `"issue|pr|directory|workdir|file"`.

Add to `RegisterAgent` (currently `models.py:44-51`):

```python
    workdirs: list[str] = Field(
        default_factory=list,
        description="Absolute directories this session owns; claimed as a side effect of "
                    "registering. Overlap is reported, never fatal — presence must not fail.",
    )

    @field_validator("workdirs")
    @classmethod
    def _check_workdirs(cls, v: list[str]) -> list[str]:
        return [_canonical(p) for p in v]
```

Add the new request model beside `ClaimRequest`:

```python
class ClaimWorkItemRequest(BaseModel):
    """Lease a work item and the directories it touches, all-or-nothing."""

    agent_id: str
    repo: str
    work_item: str = Field(..., description="issue:owner/repo#N or pr:owner/repo#N")
    paths: list[str] = Field(default_factory=list, description="Absolute directories")
    branch: str | None = None
    claim_scope: ClaimScope = ClaimScope.exclusive
    reason: str | None = None
    ttl_seconds: int | None = None

    @field_validator("work_item")
    @classmethod
    def _check_work_item(cls, v: str) -> str:
        return _canonical(v)

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, v: list[str]) -> list[str]:
        return [_canonical(p) for p in v]
```

- [ ] **Step 4: Add the REST endpoint and pass workdirs through register**

In `covasant-partyline/app/rest.py`, add `ClaimWorkItemRequest` to the `.models` import,
add `body.workdirs` as the last argument to the `db.register_agent` call
(`rest.py:66-73`), and add after `/claims` (`rest.py:300`):

```python
@router.post("/claims/work-item")
async def claim_work_item(body: ClaimWorkItemRequest) -> dict:
    """Lease a work item plus the directories it touches, atomically."""
    ttl = body.ttl_seconds or settings.default_claim_ttl_seconds
    targets = [(body.work_item, infer_resource_type(body.work_item))]
    targets += [(p, "directory") for p in body.paths]
    acquired, claims, blocker = await run_in_threadpool(
        db.claim_targets,
        body.agent_id, body.repo, body.branch, targets,
        body.claim_scope.value, body.reason, ttl,
    )
    log.info(
        "claim_work_item agent_id=%s work_item=%s paths=%s acquired=%s blocker=%s",
        body.agent_id, body.work_item, len(body.paths), acquired,
        blocker.get("agent_id") if blocker else None,
    )
    return {"acquired": acquired, "claims": claims, "blocker": blocker}
```

Import `infer_resource_type` from `.targets` at the top of `rest.py`.

- [ ] **Step 5: Update the existing REST claim tests to canonical paths**

In `tests/test_rest_api.py`, replace every repo-relative claim path with an absolute one:
`"src/auth.py"` → `"/t/legacy/src/auth"` (both `test_claim_and_release` at
`tests/test_rest_api.py:297` and `test_claim_conflict` at `:347`, including the
`assert data["claim"]["resource_path"] == …` and `any(c["resource_path"] == …)`
assertions). These now exercise the canonical contract rather than the old free-form one.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_claim_rest.py tests/test_rest_api.py tests/test_claim_overlap.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add covasant-partyline/app/models.py covasant-partyline/app/rest.py tests/test_claim_rest.py tests/test_rest_api.py
git commit -m "feat(claims): 422 on non-canonical targets; atomic work-item + directories endpoint"
```

---

### Task 5: Registration takes territory

**Files:**
- Modify: `covasant-partyline/app/db.py` — `register_agent` (`db.py:411-426`)
- Test: `tests/test_register_workdirs.py`

**Interfaces:**
- Consumes: `db.claim_targets` (Task 2), `settings.workdir_claim_ttl_seconds` (Task 3),
  `RegisterAgent.workdirs` (Task 4).
- Produces: `db.register_agent(agent_id, owner, repo, branch, display_name, workdirs=None)`
  returning the agent row plus
  `"workdirs": [{"path": str, "acquired": bool, "claim": dict | None, "holder": dict | None}]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_register_workdirs.py
"""Registration takes territory — and never fails because of a conflict."""

import pytest

pytestmark = pytest.mark.anyio


async def test_register_claims_its_workdir(client):
    r = await client.post("/api/agents", json={
        "agent_id": "wd_a", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd1/domain"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["workdirs"][0]["acquired"] is True
    assert body["workdirs"][0]["claim"]["resource_type"] == "workdir"
    claims = (await client.get("/api/claims")).json()
    assert any(c["resource_path"] == "/t/wd1/domain" for c in claims)


async def test_overlapping_workdir_reports_holder_but_still_registers(client):
    await client.post("/api/agents", json={
        "agent_id": "wd_b", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd2/src"],
    })
    r = await client.post("/api/agents", json={
        "agent_id": "wd_c", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd2/src/tenant"],
    })
    assert r.status_code == 200          # presence must never fail
    assert r.json()["agent_id"] == "wd_c"
    wd = r.json()["workdirs"][0]
    assert wd["acquired"] is False
    assert wd["holder"]["agent_id"] == "wd_b"
    assert wd["claim"] is None
    claims = (await client.get("/api/claims")).json()
    assert not any(c["resource_path"] == "/t/wd2/src/tenant" for c in claims)


async def test_register_without_workdirs_is_unchanged(client):
    r = await client.post("/api/agents", json={
        "agent_id": "wd_d", "owner": "test", "repo": "aura", "branch": "main",
    })
    assert r.status_code == 200
    assert r.json()["workdirs"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_register_workdirs.py -v`
Expected: FAIL — `KeyError: 'workdirs'` (the response has no such key).

- [ ] **Step 3: Implement**

Replace `register_agent` in `db.py`:

```python
def register_agent(
    agent_id: str,
    owner: str,
    repo: str,
    branch: str | None,
    display_name: str | None,
    workdirs: list[str] | None = None,
) -> dict:
    """Register (or refresh) an agent, optionally taking territory.

    Each workdir is claimed as an exclusive `workdir` subtree. A refused workdir is
    REPORTED, never fatal: an agent that cannot register is invisible to the line, which
    is strictly worse than one that registers while overlapping. Surfacing the holder at
    session start is the whole point — it is the earliest moment an agent can act on the
    collision, and the one call every agent already makes.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO agents (agent_id, owner, repo, branch, display_name)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (agent_id) DO UPDATE
                 SET owner = EXCLUDED.owner, repo = EXCLUDED.repo,
                     branch = EXCLUDED.branch, display_name = EXCLUDED.display_name,
                     last_heartbeat = now()
               RETURNING agent_id, owner, repo, branch, display_name,
                         registered_at, last_heartbeat""",
            (agent_id, owner, repo, branch, display_name),
        ).fetchone()
        conn.commit()

    results: list[dict] = []
    for path in workdirs or []:
        acquired, claims, blocker = claim_targets(
            agent_id, repo, branch, [(path, "workdir")], "exclusive",
            f"session workdir for {agent_id}", settings.workdir_claim_ttl_seconds,
        )
        results.append({
            "path": path,
            "acquired": acquired,
            "claim": claims[0] if acquired else None,
            "holder": None if acquired else blocker,
        })
        if not acquired:
            log.warning(
                "register_agent workdir REFUSED agent_id=%s path=%s holder=%s",
                agent_id, path, blocker.get("agent_id") if blocker else "?",
            )
    row["workdirs"] = results
    log.info("Agent registered/refreshed: agent_id=%s repo=%s branch=%s workdirs=%s",
             agent_id, repo, branch, len(results))
    return row
```

Note: `register_agent` is defined above `claim_targets` in the file but calls it at
runtime, which Python resolves fine at module scope.

Add `workdirs: list[dict] = Field(default_factory=list)` to the `Agent` response model in
`models.py:54` so FastAPI does not strip the key from the `response_model=Agent` payload.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_register_workdirs.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/db.py covasant-partyline/app/models.py tests/test_register_workdirs.py
git commit -m "feat(claims): register --workdir takes territory and reports the holder on overlap"
```

---

### Task 6: CLI — client-side normalization, `claim-issue`, `register --workdir`

Only the client can see the filesystem, so only the client can canonicalize. `realpath`
resolves symlinks and Windows junctions and makes the path absolute against the process
CWD, so `partyline claim <agent> <repo> .` is the common invocation.

**Files:**
- Create: `partyline-client/covasant_partyline/targets.py`
- Modify: `partyline-client/covasant_partyline/cli.py` — `cmd_claim` (`cli.py:109-114`),
  `cmd_release` (`:117-121`), `cmd_register`, parser (`:351-369`), epilog (`:12-33`)
- Test: `tests/test_cli_targets.py`

**Interfaces:**
- Consumes: `POST /api/claims/work-item` (Task 4), `RegisterAgent.workdirs` (Task 4).
- Produces: `covasant_partyline.targets.normalize_target(value: str) -> str`;
  `cli` subcommand `claim-issue <agent_id> <repo> <work_item> [--path P]...`;
  `register … [--workdir D]…`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_targets.py
"""Pure unit tests for client-side target normalization (no network, no pool)."""

import os

import pytest

from covasant_partyline.targets import normalize_target


def test_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    sub = tmp_path / "pkg"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    assert normalize_target("pkg") == normalize_target(str(sub))


def test_dot_resolves_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_target(".") == normalize_target(str(tmp_path))


def test_result_has_no_backslashes_and_no_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = normalize_target(".")
    assert "\\" not in out
    assert not out.endswith("/") or out == "/"
    assert ".." not in out.split("/")


@pytest.mark.skipif(os.name != "nt", reason="case-folding is Windows-only")
def test_windows_paths_are_case_folded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_target(".") == normalize_target(".").lower()


def test_work_items_are_lowercased_not_touched_by_realpath():
    assert normalize_target("issue:Covasant/Aura#482") == "issue:covasant/aura#482"
    assert normalize_target("PR:Covasant/Aura#17") == "pr:covasant/aura#17"


def test_missing_path_is_refused_before_any_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        normalize_target("no/such/dir")
```

```python
# append to tests/test_cli.py
def test_claim_issue_posts_work_item_and_paths(monkeypatch, tmp_path):
    bodies = {}

    def fake_request(method, base, path, body=None):
        bodies["path"], bodies["body"] = path, body
        return {"acquired": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()

    args = cli.build_parser().parse_args(
        ["claim-issue", "alan_aura_1", "aura", "issue:covasant/aura#482", "--path", "pkg"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert bodies["path"] == "/api/claims/work-item"
    assert bodies["body"]["work_item"] == "issue:covasant/aura#482"
    assert bodies["body"]["paths"][0].endswith("/pkg")
    assert "\\" not in bodies["body"]["paths"][0]


def test_register_sends_normalized_workdirs(monkeypatch, tmp_path):
    bodies = {}

    def fake_request(method, base, path, body=None):
        bodies["body"] = body
        return {"agent_id": "x"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "domain").mkdir()

    args = cli.build_parser().parse_args(
        ["register", "alan_aura_1", "--owner", "alan", "--repo", "aura", "--workdir", "domain"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert bodies["body"]["workdirs"][0].endswith("/domain")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_targets.py tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: covasant_partyline.targets`, and
`argparse` errors on the unknown `claim-issue` command / `--workdir` flag.

- [ ] **Step 3: Write the client-side normalizer**

```python
# partyline-client/covasant_partyline/targets.py
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


def normalize_target(value: str) -> str:
    """Return the canonical form of a claim target, or exit(2) with a usable message.

    Work items are lowercased (GitHub owner/repo are case-insensitive). Paths go through
    realpath — resolving symlinks and Windows junctions and making the path absolute
    against the process CWD — then to forward slashes, and are case-folded on Windows
    because NTFS is case-insensitive and two spellings must not hold the same directory.
    """
    value = value.strip()
    if WORK_ITEM_RE.match(value):
        return value.lower()

    expanded = os.path.expandvars(os.path.expanduser(value))
    if not os.path.exists(expanded):
        sys.exit(
            f"partyline: no such path: {value!r}\n"
            "  Claim a directory that exists on this machine. Paths are filesystem-rooted,\n"
            "  not repo-relative — pass '.' to claim the directory you're standing in."
        )
    p = os.path.realpath(expanded).replace("\\", "/")
    if os.name == "nt":
        p = p.lower()
    return p.rstrip("/") or "/"
```

- [ ] **Step 4: Wire it into the CLI**

In `partyline-client/covasant_partyline/cli.py`:

Import at the top: `from .targets import normalize_target`.

Normalize in `cmd_claim` and `cmd_release` — change
`"resource_path": a.resource_path` to `"resource_path": normalize_target(a.resource_path)`
in both.

Add the new command handler beside `cmd_claim`:

```python
def cmd_claim_issue(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/claims/work-item", {
        "agent_id": a.agent_id, "repo": a.repo,
        "work_item": normalize_target(a.work_item),
        "paths": [normalize_target(p) for p in (a.path or [])],
        "branch": a.branch, "claim_scope": a.scope, "reason": a.reason,
        "ttl_seconds": a.ttl,
    }))
```

Add `"workdirs": [normalize_target(d) for d in (a.workdir or [])]` to the body in
`cmd_register`.

Add to the parser, beside the existing `claim` subparser (`cli.py:351`):

```python
    sp = sub.add_parser("claim-issue", help="Claim a GH issue/PR and the directories it touches")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument("work_item", help="issue:owner/repo#123 or pr:owner/repo#123")
    sp.add_argument("--path", action="append",
                    help="Directory this work touches (repeatable; '.' = here)")
    sp.add_argument("--branch")
    sp.add_argument("--scope", default="exclusive",
                    choices=["exclusive", "shared_read", "intent"])
    sp.add_argument("--reason")
    sp.add_argument("--ttl", type=int, help="Lease seconds")
    sp.set_defaults(func=cmd_claim_issue)
```

Add to the `register` subparser:

```python
    sp.add_argument("--workdir", action="append",
                    help="Directory this session owns (repeatable; '.' = here). Claimed "
                         "on register; an overlap reports the holder but still registers.")
```

Update the `claim` subparser's `--resource-type` choices to
`["issue", "pr", "directory", "workdir", "file"]`, and change the `resource_path`
argument help to `"Absolute directory ('.' = here) or issue:owner/repo#N"`.

Replace the two claim lines in the epilog (`cli.py:18-21`, `:30`) with:

```text
  claim-issue  lease a GH issue/PR plus the directories it touches (preferred)
  claim      lease one target: an absolute directory, or issue:owner/repo#N
  release    release a claim you hold
```

```text
  partyline claim-issue alan_aura_1 aura issue:covasant/aura#482 --path .
  partyline register alan_aura_1 --owner alan --repo aura --workdir .
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cli_targets.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add partyline-client/covasant_partyline/targets.py partyline-client/covasant_partyline/cli.py tests/test_cli_targets.py tests/test_cli.py
git commit -m "feat(cli): realpath-normalize claim targets; add claim-issue and register --workdir"
```

---

### Task 7: MCP tools and instructions

**Files:**
- Modify: `covasant-partyline/app/mcp_server.py` — `INSTRUCTIONS` (`:29-44`),
  `register_agent` (`:50-64`), `claim_resource` (`:222-251`), `onboard_me` (`:281-295`)
- Test: `tests/test_mcp_claims.py`

**Interfaces:**
- Consumes: `db.claim_targets`, `db.register_agent(…, workdirs)`,
  `app.targets.{validate_target, infer_resource_type, InvalidTarget}`.
- Produces: MCP tool
  `claim_work_item(agent_id, repo, work_item, paths=None, branch=None, claim_scope="exclusive", reason=None, ttl_seconds=None) -> dict`
  returning `{"acquired": bool, "claims": [...], "blocker": {...} | None}` or
  `{"acquired": False, "reason": "invalid_target", "message": str, "hint": str}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_claims.py
"""MCP claim tools return structured refusals an agent can act on."""

import pytest

pytestmark = pytest.mark.anyio


async def test_invalid_target_returns_structured_refusal_not_an_exception(started_app):
    from app import mcp_server

    out = await mcp_server.claim_resource.fn(
        agent_id="mcp_x", repo="aura", resource_path="db.py",
    )
    assert out["acquired"] is False
    assert out["reason"] == "invalid_target"
    assert "not absolute" in out["message"]
    assert out["hint"]


async def test_claim_work_item_leases_issue_and_paths(started_app):
    from app import db, mcp_server

    db.register_agent("mcp_wi", "test", "aura", "main", None)
    out = await mcp_server.claim_work_item.fn(
        agent_id="mcp_wi", repo="aura", work_item="issue:covasant/aura#903",
        paths=["/t/mcp1/pkg"], ttl_seconds=300,
    )
    assert out["acquired"] is True
    assert {c["resource_path"] for c in out["claims"]} == {
        "issue:covasant/aura#903", "/t/mcp1/pkg",
    }
    db.release_resource("mcp_wi", "aura", None, "issue:covasant/aura#903")
    db.release_resource("mcp_wi", "aura", None, "/t/mcp1/pkg")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_claims.py -v`
Expected: FAIL — `AttributeError: module 'app.mcp_server' has no attribute 'claim_work_item'`.

- [ ] **Step 3: Implement**

In `covasant-partyline/app/mcp_server.py`, add
`from .targets import InvalidTarget, infer_resource_type, validate_target`, then:

Add a shared refusal helper above the claim tools:

```python
def _invalid(exc: InvalidTarget) -> dict:
    """Structured refusal, not an exception — the agent gets a correction it can apply."""
    return {"acquired": False, "reason": "invalid_target",
            "message": exc.message, "hint": exc.hint}
```

Replace the `claim_resource` docstring and add validation at the top of its body:

```python
    """Claim ONE target with a TTL'd lease: an absolute directory, or issue:owner/repo#N.

    Prefer claim_work_item — claiming the GitHub issue plus the directories it touches, in
    one atomic call, is the documented unit of coordination.

    A claim on a directory refuses any claim inside it, and vice versa: two agents must
    never work the same directory. Paths must be absolute and filesystem-rooted
    ('c:/users/you/code/repo/pkg'), never repo-relative — 'app/db.py' is refused.

    Returns {acquired: bool, claim: {...}}; on refusal `claim` is the current holder.
    """
    try:
        validate_target(resource_path)
    except InvalidTarget as exc:
        return _invalid(exc)
    resource_type = resource_type or infer_resource_type(resource_path)
```

Add the new tool after `claim_resource`:

```python
@mcp.tool()
async def claim_work_item(
    agent_id: str,
    repo: str,
    work_item: str,
    paths: list[str] | None = None,
    branch: str | None = None,
    claim_scope: str = "exclusive",
    reason: str | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """Claim the work you're taking: a GH issue/PR plus the directories it touches.

    work_item is 'issue:owner/repo#123' or 'pr:owner/repo#123'; paths are absolute
    filesystem directories. All-or-nothing — if any target is held, nothing is leased and
    `blocker` names the holder, so you can never own the issue while someone else owns the
    directory. Ask the holder; don't edit.

    Returns {acquired: bool, claims: [...], blocker: {...} | None}.
    """
    try:
        validate_target(work_item)
        for p in paths or []:
            validate_target(p)
    except InvalidTarget as exc:
        return _invalid(exc)

    ttl = ttl_seconds or settings.default_claim_ttl_seconds
    targets = [(work_item, infer_resource_type(work_item))]
    targets += [(p, "directory") for p in (paths or [])]
    log.info("claim_work_item agent_id=%s work_item=%s paths=%s", agent_id, work_item, len(paths or []))
    acquired, claims, blocker = await run_in_threadpool(
        db.claim_targets, agent_id, repo, branch, targets, claim_scope, reason, ttl,
    )
    log.info("claim_work_item agent_id=%s acquired=%s", agent_id, acquired)
    return {"acquired": acquired, "claims": claims, "blocker": blocker}
```

Add `workdirs: list[str] | None = None` to the `register_agent` tool signature, pass it to
`db.register_agent`, and extend its docstring:

```python
    """Register (or refresh) this agent instance. Call once before posting/claiming.

    agent_id is a unique instance name (e.g. 'alan_web_1'); owner is the developer/team.
    workdirs are the absolute directories this session owns — they're claimed as a side
    effect, and an overlap reports the holder in the response WITHOUT failing registration.
    Declare them: two agents must never work the same directory, and registering is the
    earliest moment the collision can surface.
    """
```

Replace the claim sentence in `INSTRUCTIONS` (`mcp_server.py:35-36`):

```text
Claim the work you're taking, not the files you'll touch: claim_work_item(issue:owner/repo#N,
paths=[absolute dirs]) before starting; release_resource when done. A claim on a directory
refuses any claim inside it — two agents must never work the same directory. Paths are
absolute and filesystem-rooted, never repo-relative. If refused, the holder is named: post
a `question` to them instead of editing.
```

Replace step 4 of `onboard_me` (`mcp_server.py:291`):

```python
        "4. Declare your territory: register_agent(workdirs=['<abs dir you own>']). "
        "Then claim_work_item('issue:owner/repo#N', paths=[...]) for the work you take, "
        "and release_resource when done. Never claim a bare filename — paths are absolute.\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_claims.py tests/test_mcp_path.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/mcp_server.py tests/test_mcp_claims.py
git commit -m "feat(mcp): claim_work_item tool, workdirs on register, structured invalid-target refusals"
```

---

### Task 8: SessionStart hook warns when you're inside someone else's territory

The hook stays read-only and fail-silent. It covers agents that never call
`register --workdir` at all, which is exactly the population that collides.

**Files:**
- Modify: `partyline-client/covasant_partyline/hook.py` — `main()` (`:53-80` and the
  render block below it)
- Test: `tests/test_hook_territory.py`

**Interfaces:**
- Consumes: `GET /api/claims` (existing), `covasant_partyline.targets.normalize_target`
  (Task 6).
- Produces: `hook.territory_warning(claims: list[dict], cwd: str, agent_id: str) -> str`
  — the rendered warning, or `""` when clear.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hook_territory.py
"""Pure unit tests for the SessionStart territory warning (no network)."""

from covasant_partyline.hook import territory_warning

CWD = "/home/alan/code/aura/aura_core/src/aura_core/domain/tenant"


def _claim(agent_id, path, **kw):
    base = {"agent_id": agent_id, "resource_path": path, "repo": "aura",
            "resource_type": "workdir", "reason": None,
            "expires_at": "2026-08-01T18:00:00Z"}
    base.update(kw)
    return base


def test_warns_when_cwd_is_inside_another_agents_claim():
    out = territory_warning(
        [_claim("alan_aura_0", "/home/alan/code/aura/aura_core/src/aura_core")],
        CWD, "alan_aura_1",
    )
    assert "alan_aura_0" in out
    assert "/home/alan/code/aura/aura_core/src/aura_core" in out


def test_warns_when_another_agent_claims_below_you():
    out = territory_warning([_claim("alan_aura_0", CWD + "/models")], CWD, "alan_aura_1")
    assert "alan_aura_0" in out


def test_silent_on_your_own_claim():
    assert territory_warning([_claim("alan_aura_1", CWD)], CWD, "alan_aura_1") == ""


def test_silent_on_unrelated_sibling():
    assert territory_warning(
        [_claim("alan_aura_0", "/home/alan/code/aura/aura_core/src/aura_coreX")],
        CWD, "alan_aura_1",
    ) == ""


def test_silent_on_work_item_claims():
    assert territory_warning(
        [_claim("alan_aura_0", "issue:covasant/aura#482", resource_type="issue")],
        CWD, "alan_aura_1",
    ) == ""


def test_silent_when_nothing_is_claimed():
    assert territory_warning([], CWD, "alan_aura_1") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hook_territory.py -v`
Expected: FAIL — `ImportError: cannot import name 'territory_warning'`.

- [ ] **Step 3: Implement**

Add to `partyline-client/covasant_partyline/hook.py`:

```python
def _overlaps(a: str, b: str) -> bool:
    """True when a and b are the same directory or one contains the other.

    The appended '/' makes this a real path-boundary test: /a/foo and /a/foobar are
    unrelated siblings, not an overlap.
    """
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def territory_warning(claims: list, cwd: str, agent_id: str) -> str:
    """Render a warning when `cwd` sits inside another agent's claimed subtree.

    Read-only and advisory: this catches the agents that never declared a workdir, which
    is exactly the population that collides. Work-item claims are skipped — they don't
    describe territory.
    """
    hits = []
    for c in claims:
        path = c.get("resource_path") or ""
        if not path.startswith("/") and not (len(path) > 1 and path[1] == ":"):
            continue  # work-item URI, not territory
        if c.get("agent_id") == agent_id:
            continue
        if _overlaps(cwd, path):
            hits.append(c)
    if not hits:
        return ""
    lines = ["", "## ⚠ Partyline: you are inside another agent's territory", ""]
    for c in hits:
        reason = f" — {c['reason']}" if c.get("reason") else ""
        lines.append(f"- `{c['resource_path']}` is held by **{c['agent_id']}**{reason}")
    lines += [
        "",
        "Two agents must never work the same directory. Ask the holder before editing "
        "(`partyline post <you> --type question --recipient <holder>`), or work elsewhere.",
        "",
    ]
    return "\n".join(lines)
```

In `main()`, add `--agent-id` (default `os.getenv("PARTYLINE_AGENT_ID", "")`), fetch
claims alongside agents inside the existing `try` block, and print the warning before the
existing traffic render:

```python
        claims = _get(f"{base}/api/claims", token)
```

```python
    try:
        from .targets import normalize_target
        warning = territory_warning(claims, normalize_target("."), a.agent_id)
    except Exception:
        warning = ""      # fail-silent: a broken hook must never block a session
    if warning:
        print(warning)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hook_territory.py tests/test_hook.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/hook.py tests/test_hook_territory.py
git commit -m "feat(hook): SessionStart warns when your cwd is inside another agent's claimed subtree"
```

---

### Task 9: Dashboard — territory map grouped by holder

**Files:**
- Modify: `covasant-partyline/client/src/App.tsx:1132-1144` (Active claims section)

**Interfaces:**
- Consumes: `GET /api/claims` (`Claim[]`, unchanged shape).
- Produces: no exported symbols; a local `groupByHolder` helper in `App.tsx`.

- [ ] **Step 1: Replace the claims section**

The current flat `<ul>` renders `repo:resource_path` per row, which with absolute paths
becomes an unreadable wall of near-identical prefixes. Group by holder and elide the
common prefix so the fleet's territory reads at a glance:

```tsx
          <section style={{ order: 2 }}>
            <h2>Active claims ({claims.length})</h2>
            {(() => {
              // Absolute paths share long prefixes; showing them in full makes the list
              // unreadable. Elide the prefix common to every claim, and group by holder
              // so the map answers "who owns what" in one pass.
              const paths = claims.map((c) => c.resource_path).filter((p) => !p.includes(":"));
              const common = paths.length > 1
                ? paths.reduce((a, b) => {
                    let i = 0;
                    while (i < a.length && i < b.length && a[i] === b[i]) i++;
                    return a.slice(0, a.lastIndexOf("/", i) + 1);
                  })
                : "";
              const byHolder = new Map<string, typeof claims>();
              claims.forEach((c) => {
                const list = byHolder.get(c.agent_id) ?? [];
                list.push(c);
                byHolder.set(c.agent_id, list);
              });
              if (!claims.length) return <ul className="pills"><li className="muted">Nothing claimed.</li></ul>;
              return (
                <>
                  {common && <p className="muted"><code>{common}</code>…</p>}
                  {[...byHolder.entries()].map(([agent, held]) => (
                    <div key={agent}>
                      <b>{agent}</b>
                      <ul className="pills">
                        {held.map((c) => (
                          <li key={c.id}>
                            <code>{c.resource_path.startsWith(common) && common
                              ? c.resource_path.slice(common.length)
                              : c.resource_path}</code>
                            <time>→ {new Date(c.expires_at).toLocaleTimeString()}</time>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </>
              );
            })()}
          </section>
```

- [ ] **Step 2: Build the SPA to verify it compiles**

Run: `cd covasant-partyline/client && npm run build`
Expected: build succeeds, no TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add covasant-partyline/client/src/App.tsx
git commit -m "feat(dashboard): group active claims by holder, elide the common path prefix"
```

---

### Task 10: Rewrite every claim statement

The statements are what agents actually read. Tasks 1-9 make claiming *mean* something;
this makes agents ask for the right thing.

**Files:**
- Modify: `CLAUDE.md` (the "Before editing shared surfaces" bullet)
- Modify: `partyline-plugin/skills/using-partyline/SKILL.md` — the "Before editing a
  shared surface" section (`:99-108`), the quick-reference table (`:130-139`), and the
  "Common mistakes" list (`:154-163`)
- Modify: `README.md`, `.github/copilot-instructions.md`,
  `partyline-plugin/agents/partyline-responder.md`

**Interfaces:**
- Consumes: the CLI surface from Task 6 and the MCP tools from Task 7 — every example
  must match those exact command and tool names.
- Produces: documentation only.

- [ ] **Step 1: Replace the claim section in the skill**

In `partyline-plugin/skills/using-partyline/SKILL.md`, replace the "Before editing a
shared surface — claim it" section with:

```markdown
## Claim the work you're taking — not the files you'll touch

**Two agents must never work the same directory.** A claim on a directory refuses any
claim inside it, and vice versa, so declaring territory is how you find out *before*
you collide rather than after.

- **Declare territory when you register:** `partyline register <id> --owner <o> --repo <r>
  --workdir .` — claims the directory you're standing in. An overlap doesn't stop you
  registering; it reports the holder so you can talk to them or work elsewhere.
- **Claim the work item:** `partyline claim-issue <id> <repo> issue:owner/repo#123
  --path .` (`claim_work_item`) — leases the GitHub issue *and* the directories it
  touches, all-or-nothing. This is the unit that matters; a lease on an issue is what
  stops two agents starting the same job.
- **Paths are absolute and filesystem-rooted**, never repo-relative. `app/db.py` is
  refused; `c:/users/you/code/repo/app` is a claim. The CLI resolves `.` for you.
- **Never claim a bare filename.** `db.py` identifies nothing — three agents can each
  "hold" it and all be wrong.
- **`partyline release <id> <repo> <target>`** when done. Expired and your-own claims
  re-acquire safely; workdir claims renew while your session is alive.
- **If a claim is refused, the holder is named.** Post a `question` to them
  (as `recipient`). Don't edit.
```

- [ ] **Step 2: Update the quick-reference table and Common mistakes in the skill**

Replace the claim row of the table with these two:

```markdown
| Take a work item + its dirs | `partyline claim-issue <id> <repo> issue:o/r#N --path .` | `claim_work_item` |
| Lease/release one target | `partyline claim …` / `release …` | `claim_resource` / `release_resource` |
```

Replace the "Editing a shared file without claiming it" mistake with:

```markdown
- **Working in a directory another agent holds.** This is the collision the line exists to
  prevent. Declare `--workdir` at register; if it's refused, that's the answer — pick a
  different subtree or ask the holder.
- **Claiming a bare filename or a repo-relative path.** `db.py` and `app/db.py` identify
  nothing and are refused. Claim absolute directories and work items.
```

- [ ] **Step 3: Update CLAUDE.md**

Replace the "Before editing shared surfaces" bullet with:

```markdown
- **Claim the work, not the files.** Declare territory when you register
  (`partyline register <id> --owner <o> --repo <r> --workdir .`) and claim the work item
  you take (`partyline claim-issue <id> <repo> issue:owner/repo#123 --path .` ≡
  `claim_work_item`). Two agents must never work the same directory — a claim on a
  directory refuses any claim inside it. Paths are absolute and filesystem-rooted, never
  repo-relative. If refused, the holder is named: post a `question` to them instead of
  editing. `release_resource` when done.
```

- [ ] **Step 4: Update the remaining three documents**

Each of `README.md`, `.github/copilot-instructions.md`, and
`partyline-plugin/agents/partyline-responder.md` gets this exact paragraph, wrapped to
that file's line width and placed where its current claim guidance sits:

> Claim the work you're taking, not the files you'll touch. Declare your territory when
> you register (`--workdir .`) and claim the work item you take
> (`partyline claim-issue <id> <repo> issue:owner/repo#123 --path .` ≡ `claim_work_item`).
> Two agents must never work the same directory — a claim on a directory refuses any
> claim inside it. Paths are absolute and filesystem-rooted, never repo-relative; a bare
> filename like `db.py` is refused. If a claim is refused the holder is named: ask them
> instead of editing.

Then grep each file for `claim` and fix every remaining occurrence — including CLI
examples, which must use `claim-issue` and absolute paths:

Run: `grep -rn "claim" README.md .github/copilot-instructions.md partyline-plugin/agents/partyline-responder.md`

- [ ] **Step 5: Verify no stale guidance survives**

Run: `grep -rn "claim.*src/\|claim_resource(repo, resource_path\|shared file" --include="*.md" .`
Expected: no hits pointing agents at repo-relative paths or file-level claims.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md .github/copilot-instructions.md partyline-plugin/
git commit -m "docs(claims): claim the work item and your directory; never a bare filename"
```

---

### Task 11: Deploy and verify against the live line

**Files:**
- No source changes; this task is verification.

**Interfaces:**
- Consumes: everything above.
- Produces: a verified deployment.

- [ ] **Step 1: Run the full test suite**

Run: `docker compose up -d && pytest tests/ partyline-client/tests/ -v`
Expected: PASS, with the PG-backed suites actually running (not skipped).

- [ ] **Step 2: Deploy**

Run: `./deploy.sh dev-eastus2 dev`
Expected: the privileged migrate step reports the `ux_claims_path` index created and
`ux_claims_key` dropped. If it reports `InsufficientPrivilege`, the DDL ran from app
startup instead of `scripts/migrate_schema.py` — fix that before continuing, or the new
conflict logic runs against the old uniqueness constraint.

- [ ] **Step 3: Verify the overlap refusal on the live line**

```bash
./partyline.sh register verify_claims_1 --owner test --repo covasant-partyline \
    --workdir c:/users/alana/code/repos/covasant-partyline/covasant-partyline/app
./partyline.sh register verify_claims_2 --owner test --repo covasant-partyline \
    --workdir c:/users/alana/code/repos/covasant-partyline/covasant-partyline/app/models.py
```

Expected: the first acquires; the second **registers successfully** with
`workdirs[0].acquired == false` and `holder.agent_id == "verify_claims_1"`.

- [ ] **Step 4: Verify a bad target is refused**

```bash
./partyline.sh claim verify_claims_1 covasant-partyline db.py
```

Expected: a non-zero exit with the "no such path" message from the CLI (it never reaches
the server), and — sending a repo-relative path that *does* exist — an HTTP 422 naming
`resource_path`.

- [ ] **Step 5: Clean up the live line**

```bash
./partyline.sh release verify_claims_1 covasant-partyline \
    c:/users/alana/code/repos/covasant-partyline/covasant-partyline/app
./partyline.sh claims   # confirm no verify_claims_* rows remain
```

- [ ] **Step 6: Commit any fixes found during verification**

```bash
git add -A && git commit -m "fix(claims): issues found verifying against the deployed line"
```
