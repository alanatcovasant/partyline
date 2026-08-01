# Claim semantics: absolute-path subtrees + work-item claims — design

**Date:** 2026-08-01
**Status:** Approved for planning
**Scope:** The claim subsystem end to end — the conflict algorithm and schema in
`covasant-partyline/app/db.py`, the wire contract in `app/models.py`, the REST handlers in
`app/rest.py`, the MCP tools and `INSTRUCTIONS` in `app/mcp_server.py`, path normalization
and command surface in `partyline-client/covasant_partyline/cli.py`, the registration
side-effect in `db.register_agent`, the claim UI in `client/src/App.tsx`, and every
document that tells an agent what to claim (`CLAUDE.md`, the `using-partyline` skill,
`README.md`, `.github/copilot-instructions.md`, `partyline-plugin/agents/partyline-responder.md`).
Does **not** change messages, checkpoints, retention, search, or the SSE/channel push.

## Problem

Claims are documented as a mutual-exclusion mechanism and implemented as a string
registry. The gap makes them useless in exactly the case they exist for.

1. **Conflict is exact string equality.** The unique index (`db.py:285-286`) is
   `(repo, COALESCE(branch,''), resource_path)` and the holder lookup (`db.py:869-874`)
   is `resource_path = %s`. A claim on `covasant-partyline/app` therefore does not block
   `covasant-partyline/app/db.py`. There is no overlap logic anywhere in the codebase.
   Directory claims are inert: `resource_type` already accepts `directory`
   (`cli.py:356-357`) but the value is decorative metadata that changes no behavior.

2. **Paths are unrooted, so the same target has many keys.** Nothing requires
   `resource_path` to be rooted, normalized, or even to exist. `db.py`,
   `app/db.py`, and `covasant-partyline/app/db.py` are three unrelated leases on one
   file. Two agents can each hold a "claim" on the same file and both believe they are
   safe.

3. **`repo`/`branch` in the key are wrong on two counts.** They fail to disambiguate —
   git worktrees put the same `(repo, branch, relpath)` at two different physical
   directories, and agents in separate worktrees are not colliding — and they fail to
   discriminate, because a relative path under `repo` is meaningless to an agent whose
   working directory is a different clone.

4. **The unit of contention that matters is not claimable.** `issue_ref` exists on
   messages (`models.py:77`) but not on claims (`models.py:120-131`). An agent can
   *mention* the GitHub issue it is working; it cannot lease it. Meanwhile every claim
   statement in the repo points agents at files — the unit they are least likely to
   claim and least likely to benefit from claiming.

5. **Guidance alone has already been tried and has already failed.** Reminders, skills,
   `CLAUDE.md`, MCP `INSTRUCTIONS`, and the onboarding prompt all say "claim before
   editing a shared file." Agents still collide. The mechanism must make collision
   *visible without being remembered*, not merely be described more emphatically.

## Goals

- One claim key: a **normalized absolute filesystem path** or a **work-item URI**.
  `repo`/`branch` survive as display and filter metadata only.
- **Overlap, not equality**, decides conflict: a claim on a directory refuses any claim
  on a descendant, and a claim on a descendant refuses any claim on an ancestor.
- Path normalization happens **client-side** (only the client can see the filesystem);
  the server **validates and refuses** anything not already canonical, which kills bare
  basenames like `db.py` outright.
- **Work items are first-class and are the headline claim.** `issue:owner/repo#N` and
  `pr:owner/repo#N` are claimable targets, leasable atomically together with the
  directories the work will touch.
- **Registration establishes territory.** `register` accepts `--workdir` and attempts the
  subtree claims as a side effect, reporting the overlapping holder when refused — so
  collision surfaces at session start, in the one call every agent already makes.
- Every claim statement in the repo is rewritten to match: claim the issue and the
  directory; never claim a file; never use a relative path.
- No manual deploy step: the index change ships in `_MIGRATIONS`, applied by the existing
  privileged `scripts/migrate_schema.py` step.

## Non-goals

- No change to messages, checkpoints, search, retention, or the SSE/channel push.
- No cross-machine coordination (see Limitations).
- No enforcement in the editing path. A claim remains advisory: nothing prevents an agent
  from writing to a file it has not claimed. This design makes the collision *detectable
  and attributable*, not impossible.
- No backfill of existing claims (see Rollout).

## Design

### 1. The conflict key collapses to `resource_path`

`resource_path` holds exactly one of two forms:

| Form | Example | Conflict rule |
| --- | --- | --- |
| Absolute path | `c:/users/alana/code/repos/aura/aura_core/src/aura_core/domain/tenant` | Subtree overlap |
| Work-item URI | `issue:covasant/aura#482`, `pr:covasant/aura#517` | Exact match |

`repo` and `branch` remain columns on `claims` and remain in the request models — they
are what the dashboard groups by and what `list_claims` filtering will want — but they
leave the uniqueness constraint and the holder lookup.

Schema change, added to `_MIGRATIONS` in `db.py` (owner-only DDL; applied by
`scripts/migrate_schema.py`, never by the app SP):

```sql
DROP INDEX IF EXISTS ux_claims_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_path ON claims (resource_path);
```

`repo` stays `NOT NULL`; no column is added or dropped.

### 2. Conflict detection: subtree overlap under an advisory lock

A new claim on path `p` conflicts with any unexpired claim whose path is equal to `p`, is
a descendant of `p`, or is an ancestor of `p`:

```sql
SELECT {_CLAIM_COLS} FROM claims
 WHERE expires_at > now()
   AND agent_id <> %(agent)s
   AND (   resource_path = %(p)s
        OR starts_with(resource_path, %(p)s || '/')     -- holder is below me
        OR starts_with(%(p)s, resource_path || '/') )   -- holder is above me
 LIMIT 1
```

Three details this query gets right that the obvious version does not:

- **`starts_with`, not `LIKE`.** `_` and `%` are legal in filenames and are `LIKE`
  wildcards. With `LIKE`, `aura_core` matches `auraXcore` — and `aura_core` is a real
  directory in this fleet. `starts_with` has no pattern semantics.
- **The appended `/` is load-bearing.** Without it, `/a/foo` would conflict with
  `/a/foobar`, which are unrelated siblings. Comparing against `p || '/'` tests for a
  genuine path boundary.
- **`agent_id <> caller`** lets an agent re-claim and refine its own territory (claim a
  parent, then a child) without fighting itself, matching the existing
  `OR claims.agent_id = EXCLUDED.agent_id` behavior.

This cannot be expressed as a unique index, so the single `INSERT … ON CONFLICT`
(`db.py:846-863`) becomes a short transaction:

```text
BEGIN
  SELECT pg_advisory_xact_lock(_CLAIM_LOCK_KEY)   -- module constant, e.g. 0x504C434C

  <overlap query>
  if holder found -> return (False, holder)
  INSERT ... ON CONFLICT (resource_path) DO UPDATE
    SET ... WHERE claims.expires_at < now() OR claims.agent_id = EXCLUDED.agent_id
COMMIT
```

One fixed advisory-lock key serializes every claim attempt fleet-wide. At this volume —
claims are rare, the lock is held for microseconds, and the table holds tens of live rows
— that is cheaper than any partitioned scheme and removes the race entirely: two agents
claiming a parent and a child concurrently cannot both win. The lock releases at commit,
including on rollback.

The unexpired-holder scan is a sequential scan. With claims in the tens of rows that is
correct and not worth indexing; `starts_with` would not use the `text_pattern_ops` index
anyway.

`release_resource` keeps exact-match semantics — you release precisely what you hold, not
a subtree.

### 3. Normalization client-side, validation server-side

Only the CLI can see the filesystem, so only the CLI can canonicalize. `cli.py` gains:

```python
def normalize_target(value: str) -> str:
    if _WORK_ITEM_RE.match(value):          # issue:owner/repo#N | pr:owner/repo#N
        return value.lower()                # GitHub owner/repo are case-insensitive
    p = os.path.realpath(os.path.expandvars(os.path.expanduser(value)))
    p = p.replace("\\", "/")
    if os.name == "nt":
        p = p.lower()                       # NTFS is case-insensitive
    return p.rstrip("/") or "/"
```

`realpath` resolves symlinks and Windows junctions and makes the path absolute against
the process CWD, so `partyline claim . …` from inside the target directory is the common
invocation. The CLI errors before making a request if the path does not exist.

The server validates and refuses non-canonical input rather than trying to repair it —
repair would require filesystem access it does not have. A target is rejected when it is
not a work-item URI **and** any of: it is not absolute (`^[a-z]:/` or `^/`); it contains
a backslash; it contains a `.` or `..` segment; it has a trailing slash; or it contains
an uppercase character in a Windows-drive-rooted path. Rejection returns HTTP 422 from
REST and `{"acquired": false, "reason": "invalid_target", "message": …, "hint": …}` from
the MCP tool — a readable correction the agent can act on, not an exception trace.

A bare `db.py` fails the absolute-path test, which is the mechanical version of "blocking
on a file makes no sense unless it's the full path."

### 4. Work items are the headline claim

`resource_type` becomes a closed set: `issue`, `pr`, `directory`, `workdir`, `file`.
`module` and `service` are dropped — nobody can point at them on disk. The server infers
the type from the target form when the caller omits it.

The primary command leases a work item and its directories **atomically**:

```bash
partyline claim-issue <agent> issue:covasant/aura#482 \
    --paths c:/users/alana/code/repos/aura/aura_core/src/aura_core/domain/tenant \
    --reason "tenant model refactor" --ttl 3600
```

All targets are checked and inserted inside one advisory-locked transaction: either every
target is acquired or none is, and the response names the holder of each refused target.
Partial acquisition would leave an agent believing it owns the work item while another
agent owns the directory — the exact ambiguity this design exists to remove.

`file` remains a legal `resource_type` because a full-path file claim is coherent, but it
disappears from every piece of guidance. The documented units are the issue and the
directory.

MCP gains `claim_work_item(agent_id, work_item, paths, …)` alongside the existing
`claim_resource`, which keeps working for single targets.

### 5. Registration establishes territory

`register_agent` gains `workdirs: list[str] | None`. After the presence upsert, it
attempts an exclusive claim on each workdir with `resource_type='workdir'` and returns:

```json
{ "agent_id": "...", "...": "...",
  "workdirs": [
    {"path": "c:/.../aura_core/src/aura_core/domain", "acquired": false,
     "holder": {"agent_id": "alan_aura_0", "expires_at": "...", "reason": "issue #482"}}
  ] }
```

**Registration itself always succeeds even when a workdir is refused.** Presence must
never fail — an agent that cannot register is invisible to the line, which is strictly
worse than one that registers while overlapping. The conflict is reported loudly in the
response and surfaced by the SessionStart hook, which renders refused workdirs as a
prominent warning naming the holder and telling the agent to talk to them or pick a
different subtree. (This refines the position taken during brainstorming, where
registration was to be refused outright.)

`workdir` claims are refreshed by the existing `_heartbeat` path: every subsequent call
from that agent extends its `workdir` claims to `now() + ttl`. A live session holds its
territory indefinitely; a dead session's territory expires one TTL later with no
session-end hook required. Default workdir TTL is 3600s, separate from
`default_claim_ttl_seconds` (900s) — a session outlives a task.

The CLI flag is repeatable: `partyline register alan_aura_1 --owner alan --repo aura
--workdir <abs> --workdir <abs>`. It is not mandatory; an agent that omits it registers
exactly as today.

### 6. Statements rewritten

The canonical statement replacing "before editing a shared file, claim it":

> **Claim the work you're taking, not the files you'll touch.** Claim the GitHub issue
> (`issue:owner/repo#N`) and the absolute directory you'll work in — one call,
> `claim-issue`. Two agents must never work the same directory: a claim on a directory
> refuses any claim inside it. Paths are absolute and filesystem-rooted, never
> repo-relative. If a claim is refused, the holder is named — ask them; don't edit.

Applied to: `CLAUDE.md`; `partyline-plugin/skills/using-partyline/SKILL.md` (the "Before
editing a shared surface" section, the quick-reference table, and the "Common mistakes"
entry); `app/mcp_server.py` (`INSTRUCTIONS`, the `claim_resource`/`claim_work_item`
docstrings, and step 4 of the `onboard_me` prompt); `cli.py` (epilog examples and
per-flag help); `README.md`; `.github/copilot-instructions.md`;
`partyline-plugin/agents/partyline-responder.md`.

The dashboard claim view (`client/src/App.tsx`) groups live claims by holder and renders
paths with the common prefix elided, so a fleet-wide territory map is readable at a
glance.

## Testing

Unit, against a local Postgres (existing `tests/` harness):

- **Normalization** — Windows drive-letter case, backslash conversion, trailing slash,
  `..` and `.` segments, `~`/`%USERPROFILE%` expansion, symlink and junction resolution,
  work-item lowercasing.
- **Overlap, positive** — parent refuses child; child refuses parent; equal refuses.
- **Overlap, negative** — siblings `/a/foo` vs `/a/foobar` do **not** conflict (the
  appended-`/` boundary); `/a/aura_core` does **not** conflict with `/a/auraXcore` (the
  `LIKE`-wildcard trap); `issue:x/y#1` does **not** conflict with `issue:x/y#12`.
- **Self-claims** — an agent may claim a child of its own parent claim.
- **Expiry** — an expired parent claim does not block.
- **Race** — two concurrent transactions claiming a parent and a child: exactly one
  acquires, the loser sees the winner as holder.
- **Atomicity** — a `claim-issue` whose second path is held acquires nothing, and the
  work item is not left leased.
- **Validation** — bare `db.py`, `app/db.py`, `C:\Users\…`, `/a/b/`, and `/a/../b` are
  each refused with `invalid_target`.
- **Registration** — an overlapping `--workdir` still registers, reports the holder, and
  does not create the claim; a subsequent call from the same agent extends its workdir
  claims' `expires_at`.

Verification against the deployed app per the repo ground rule: register two agent ids
with overlapping workdirs on the live line, confirm the refusal names the holder, then
release and clean up both test agents.

## Rollout

1. Add the index swap to `_MIGRATIONS`; deploy runs the privileged
   `scripts/migrate_schema.py` step. The app SP cannot run this DDL itself — attempting
   it from app startup raises `InsufficientPrivilege` and is skipped silently, so the
   migration **must** go through the privileged step or the old index survives and the
   new conflict logic runs against stale uniqueness.
2. Deploy server + client together. The server rejects non-canonical targets, so an old
   CLI sending repo-relative paths gets a 422 naming the fix — a loud, self-explaining
   failure rather than a silent wrong-key claim.
3. **No backfill.** Every existing claim is repo-relative and meaningless under the new
   key, and claims are TTL'd at 900s — the old rows age out within fifteen minutes of
   deploy. The index swap tolerates them in the interim (relative paths are unique among
   themselves).

## Limitations

- **Absolute paths are per-machine.** Two developers on separate clones will never
  conflict, because their paths differ. That is correct — they are not colliding — but it
  means this coordinates a fleet sharing one filesystem view, not one spread across
  machines.
- **Case-folding is decided by the claiming client's platform.** A Windows agent
  (`c:/users/…`, folded) and a WSL agent (`/mnt/c/users/…`, not folded) viewing the same
  directory produce different keys and will not see each other. Agents that must
  coordinate have to share a path namespace.
- **Claims remain advisory.** Nothing stops an agent from editing an unclaimed or
  someone-else's directory. This design makes the overlap visible at the earliest moment
  an agent can act on it; it does not enforce.
