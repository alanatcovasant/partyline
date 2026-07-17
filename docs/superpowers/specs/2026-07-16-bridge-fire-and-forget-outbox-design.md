# Bridge fire-and-forget outbox + durable fallback — design

**Date:** 2026-07-16
**Status:** Approved for planning
**Scope:** The local MCP bridge (`partyline-client/covasant_partyline/bridge.py`), the
channel server's reply path (`channel.py`), the REST `POST /api/messages` handler, and an
additive `messages` schema migration. Does **not** cover the broader token-footprint
reduction (tool-schema/instructions size, semistructured message format) — that is a
separate spec to follow.

## Problem

Invoking the `partyline` MCP bridge from an agent can hang for minutes, and the hang
costs tokens indirectly: while the bridge is blocked the agent eventually regains control,
reasons about the failure, and often retries — that reasoning/retry loop is the burn, not
the wait itself.

Root causes, all in the synchronous request/response design of the bridge:

1. **Every send is synchronous and slow-bounded.** `post_message` rides
   `_Remote.call` → `_call_bounded` with stacked ceilings: `_LOCK_ACQUIRE_TIMEOUT_SECS =
   150s` + `_CALL_TIMEOUT_SECS = 120s` ≈ **4.5 min** worst case — for a fire-and-forget
   broadcast the agent needs no response from.
2. **Wedged calls cascade.** When the Databricks App is cold (scale-to-zero — the common
   case) or mid-restart, call #1 rides the 120s hard deadline and later calls in the same
   turn queue behind the per-remote lock.
3. **Startup blocks the whole bridge.** The eager connect is `await`ed before the stdio
   loop starts serving, so a cold app blocks even the `initialize` handshake → client
   reports "Connection closed". The first call of a session is exactly when the app is
   most likely cold.
4. **The channel `reply` has its own 30s synchronous block** (`urllib` POST inside the
   tool handler) — same problem, separate code path.

A secondary correctness problem the fix introduces and must handle: once a send can sit
in a queue retrying (or spill to a file and be reposted later), **insert time is no longer
a truthful timestamp for when the event happened.**

## Goals

- Sending a message from an agent returns in **milliseconds** and can never block the turn.
- A message is **never silently lost**: if it can't be delivered it lands in a durable,
  human-readable local file.
- An agent that truly needs delivery confirmation (or the real server-assigned message id)
  can get it, but pays nothing when it doesn't care.
- Reads and claims still return real data, but **fail fast (~8s)** instead of hanging.
- Cold-start no longer breaks the `initialize` handshake.
- Timestamps stay truthful: a delayed/reposted message reads in its true chronological slot.

## Non-goals

- Reducing the always-on MCP tool-schema / `INSTRUCTIONS` token footprint (separate spec).
- Semistructured vs. unstructured message format changes (separate spec).
- Persisting tickets across a bridge restart (tickets are in-memory only; a restart yields
  `unknown`, and undelivered messages are covered by the spool file, not the ticket table).
- Auto-replaying the spool file onto the line (explicitly out — safety-net only).

## Architecture — send/read dispatch split

The bridge splits calls by **whether the agent needs the response**.

### Fire-and-forget (locally intercepted, never proxied inline)

`post_message`, `reply`, `ack_checkpoint`, `register_agent`, `release_resource`.

Handled locally in the bridge (the way `partyline_bridge_info` already is), NOT forwarded
inline to the upstream session. Each call:

1. mints a `uuid4` **ticket**,
2. stamps `event_time` (UTC, ISO-8601) — the true emit moment,
3. enqueues `{ticket, verb, args, event_time, enqueued_at, attempts}` to the **outbox**,
4. returns `{queued: true, ticket}` in milliseconds.

### Synchronous, fast-fail (still proxied)

`claim_resource` (the agent MUST know if it got the lock) and all reads (`get_unread`,
`get_conversation`, `search_messages`, `list_agents`, `list_claims`, `get_since`). These
keep returning real data but their hang ceiling drops from ~270s to ~8–10s.

### New local tool

`check_send(ticket)` is handled entirely locally (a lookup in the in-memory ticket table);
it never touches the network, so it always returns instantly even when the app is down.
Returns one of:
- `pending` — in queue / mid-retry
- `sent` + `{message_id, at}` (`message_id` present for `post_message`/`reply`)
- `failed` + `{spooled_to, error, at}`
- `unknown` — ticket not in table (evicted, or bridge restarted)

### Data flow

```
agent → post_message ──▶ [outbox queue] ──▶ returns {queued, ticket}   (instant)
                              │
              background sender (REST POST /api/messages, retries w/ backoff)
                              ├─ success ─────▶ ticket table: sent {message_id}
                              └─ fail until give-up ─▶ spool file + ticket table: failed
                                                       + flag → prefix next tool result
agent → get_unread / claim ─▶ proxied, ~8s fast-fail (unchanged response shape)
```

### Why the sender uses REST, not the MCP session

The background sender delivers via plain `POST /api/messages` (the path `channel.py`'s
`post_reply` already uses successfully), NOT over the proxied `_Remote` MCP session.
Rationale:

- **Decouples fire-and-forget from the flaky session** that reads/claims ride — the whole
  point of the fix.
- **Re-resolves auth per attempt** from the Databricks SDK `Config` (fresh bearer per
  POST, like `channel.py`), so **token expiry becomes a non-event on the send path** —
  there is no long-lived baked-in credential to go stale.
- Simpler payload; isolated failure domain.

Consequence: the send path no longer needs `_Remote`'s token-freshness apparatus
(`_token_stale`, `_peek_token_expiry`, `_TOKEN_MAX_AGE_SECS`, proactive reconnect). That
machinery remains only for the synchronous reads/claim that still use the proxied session.

## The queue absorbs connection-health failures

Because sending is a queue drained by a background worker, the agent is decoupled from
connection health. Three failure modes that today surface as agent-visible hangs become
internal, invisible retries:

- **Expired token** — sender mints a fresh token per attempt; no stale baked credential.
- **App cold-start / mid-restart** — sender retries with backoff until the app wakes; the
  agent already has its ticket and has moved on.
- **Transient network blips** — retry, invisible.

## Components (all in-process in the bridge)

### Outbox

`asyncio.Queue` of pending sends. Entry: `{ticket, verb, args, event_time, enqueued_at,
attempts}`. `verb` selects the REST endpoint/shape for the sender.

### Ticket table

`OrderedDict[ticket → status]`, bounded to the last ~200 entries (evict oldest). In-memory
only; does not survive a bridge respawn (respawn → `unknown`; durability is the spool
file's job, not the table's).

### Background sender

- Drains the outbox, delivering each entry via REST with **exponential backoff, 1s → 15s
  cap** (mirrors `notify.py` / `channel.py` reconnect backoff).
- **Give-up window: ~2 minutes** per message (covers a typical scale-to-zero cold-start;
  ~10–12 attempts). On give-up → spill to spool file, mark ticket `failed`.
- On success → mark ticket `sent` with the server-returned `message_id` (when the verb
  returns one).

### File fallback (spool)

On give-up, append the message as one YAML document to the spool file. **Safety-net only —
nothing auto-replays it.** Append-mode with a best-effort file lock so concurrent agents
pointed at a shared file don't interleave-corrupt. A spill failure is logged loud on
stderr but never raised (the fallback of a fallback must not crash the bridge).

Spool record shape:

```yaml
- ticket: 3f2a...
  agent_id: alan_aura_3
  verb: post_message
  event_time: 2026-07-16T18:22:04Z
  enqueued_at: 2026-07-16T18:22:04Z
  spooled_at: 2026-07-16T18:24:09Z
  attempts: 11
  last_error: "connect timeout to https://.../api/messages"
  message:
    message_type: status
    summary: "claiming db.py for the pool fix"
    payload: "..."
    recipient: null
    reply_to_id: 812
```

### Spool path configuration

Resolved by the bridge at startup from env var **`PARTYLINE_SPOOL_PATH`**:

- **directory** → `<dir>/partyline_<agent_id>.yaml` (filename still identifies the
  stranded agent),
- **file path** → used verbatim (several agents can share one `partyline.yaml`),
- value containing **`{agent_id}`** → substituted,
- **default when unset** → `../partyline_<agent_id>.yaml` relative to the bridge's CWD
  parent (the shared sibling-repos dir).

Surfaced in `partyline_bridge_info` output so an agent can see where its fallback lands.

**Dependency:** the bridge currently never sees the agent's identity (it's a pure proxy).
For the default filename and spool contents to name the agent, the bridge needs `agent_id`:
add a `--agent-id` flag / `PARTYLINE_AGENT_ID` env, falling back to `.partyline/config.json`
(which `channel.py` already reads via `cli._load_config`).

### Failure surfacing (no polling required)

When the sender spills to file it sets a flag; the **next tool result the bridge returns**
(any verb) gets a one-line prefix, e.g.:

```
⚠ 1 partyline message spooled to ../partyline_alan_aura_3.yaml (app unreachable)
```

so a silent app outage doesn't stay silent, without the agent having to poll `check_send`.

## Three-clock timestamp model

Today `messages` has one clock: `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` set at
insert. With the outbox, that conflates "emitted" and "inserted". Split into three explicit
clocks:

| Column | Meaning | Set by | Source of truth for |
|---|---|---|---|
| `event_time` | when the agent **emitted** the event | **bridge**, at enqueue (UTC) | ordering, display, "when did this happen" |
| `received_at` | when the **app** accepted the POST | REST layer, `now()` at handler | server-side audit / ingest lag |
| `created_at` | when the **row** was inserted | Postgres `DEFAULT now()` (unchanged) | DB-level insert record |

**Keep all three.** `received_at` vs `created_at` are usually equal but can diverge; keeping
`created_at` untouched means **zero risk to existing queries** (retention job, `list_agents`
lateral join, dashboard stats). We add clocks; we do not repurpose one.

### event_time flow

- The **bridge** stamps `event_time` at enqueue and includes it in the REST POST body —
  so a message that retries 2 min, or spills and is reposted, still carries its original
  emit time.
- REST `POST /api/messages` accepts an **optional** `event_time`; if absent (direct caller,
  dashboard compose box) it defaults to `received_at`. Nothing breaks for callers that
  don't supply it.
- `db.post_message` gains an `event_time` param defaulting to `now()` when `None`
  (backward-compatible).

### Migration (additive, safe; runs via the privileged step)

The app service principal is not the table owner and cannot run DDL, so this runs through
the privileged migration step (`scripts/migrate_schema.py`), not the app.

```sql
ALTER TABLE messages          ADD COLUMN IF NOT EXISTS event_time  TIMESTAMPTZ;
ALTER TABLE messages          ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;
ALTER TABLE messages_archive  ADD COLUMN IF NOT EXISTS event_time  TIMESTAMPTZ;
ALTER TABLE messages_archive  ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;
UPDATE messages SET event_time = created_at, received_at = created_at WHERE event_time IS NULL;
```

`messages_archive` is `LIKE messages INCLUDING ALL`, but that copies the schema only at
creation time, so the archive table needs the same ALTER explicitly (above).

### Ordering

Canonical read/display order becomes **`ORDER BY event_time DESC, id DESC`** (`id` as the
deterministic tiebreaker for equal/close event times).

The **checkpoint cursor stays `id`-based** and this is safe from missed messages:

- `id` is still monotonic on insert, so a late-arriving message (retried or reposted) always
  gets a higher `id` than anything already acked → `get_since(after_id)`'s `WHERE id >
  checkpoint` still catches it. The agent never skips it.
- Returned rows are then ordered by `event_time`, so the message displays in its true
  chronological slot rather than masquerading as "now".

Two orderings, two jobs: `id` = "have I seen this?" (never lies about newness); `event_time`
= "when did it happen?" (truthful display).

Accepted, correct consequence: a message reposted from the spool 20 min late shows as
unread (you haven't seen it) but sorts back into history at its original emit time.

## Fast-fail timeouts & non-blocking startup

- **New sync timeout tier.** Add `_SYNC_CALL_TIMEOUT_SECS ≈ 8s` and a matching short
  lock-acquire (~10s) applied to `claim_resource` and reads. A stale-token/cold-app read
  fails in ~8s; the agent retries cheaply. The 120s ceiling is deleted from the send path
  (sends no longer use `_Remote`) and remains only as a generous net for a genuinely-slow
  read.
- **Non-blocking startup.** Move the eager connect into a background task so
  `stdio_server()` serves the `initialize` handshake immediately — kills the cold-start
  "Connection closed" failure. Capture upstream `instructions` when the background connect
  lands; fall back to the static `INSTRUCTIONS` until then.
- **Background sender lifecycle.** Started alongside the stdio loop in `_serve`; on stdio
  loop exit the sender gets a **~5s grace period** to flush the outbox over REST, then
  anything still unsent spills to the spool file. A clean shutdown never silently drops
  queued messages.

## Error handling summary

- Send enqueue: cannot fail from the agent's perspective (returns a ticket).
- Sender delivery failure: retried to the give-up window, then spooled; ticket → `failed`;
  next tool result prefixed with the warning.
- Spool write failure: logged loud on stderr, never raised.
- Read/claim failure: fast error (~8s) the agent can retry or reason about cheaply.
- Bridge restart with queued/failed tickets: tickets become `unknown`; undelivered messages
  are recoverable from the spool file.

## Testing

- **Pure units (no network):** ticket lifecycle (pending→sent / pending→failed, eviction at
  bound), spool path resolution (dir / file / `{agent_id}` / default-from-CWD-parent), spool
  YAML record shape + append/locking, `event_time` stamping at enqueue, `check_send` status
  mapping, warning-prefix injection.
- **Sender behavior (mocked REST):** success marks ticket sent with `message_id`; N failures
  → backoff → give-up → spool; fresh token minted per attempt.
- **Timestamp/ordering (db layer):** `post_message(event_time=...)` round-trips; absent
  `event_time` defaults to `received_at`/`now()`; `get_since`/`get_unread` order by
  `event_time DESC, id DESC` while cursor stays `id`-based (late/reposted message with high
  `id` + old `event_time` is still returned as unread and sorts into history).
- **Migration:** additive columns idempotent (`IF NOT EXISTS`); backfill sets
  `event_time`/`received_at` = `created_at` for existing rows; archive table altered too.
- **Startup:** stdio loop serves `initialize` before the background connect completes
  (simulate cold app).
- **Live verification (per CLAUDE.md):** exercise against the deployed app via
  `databricks bundle run`; post a fire-and-forget message while the app is scaled to zero,
  confirm instant return + eventual delivery (or spool), then clean up any test
  agents/messages from the live line.

## Rollout / shared-surface coordination

- Schema migration, `resources/*.yml`, `deploy.sh`, and the retention job are shared
  surfaces — `claim_resource` before editing, per CLAUDE.md.
- Migration is additive and backward-compatible: deploy migration first (adds columns,
  backfills), then the app change that writes/reads the new columns. Old app code ignores
  the new columns; new app code tolerates NULLs via the backfill + defaults.
- Bridge/client change ships in `partyline-client` (editable install per project memory).
```
