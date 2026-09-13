# ADR: Conversation threading + searchable history (and the archive tier)

**Date:** 2026-07-11 · **Status:** Accepted, implementing

## Context

The partyline is an append-only message log. To *inspect what occurred* we want
full-text (and later semantic) search over history, exposed on all three surfaces
(MCP, REST, web), and a stable **conversation id** to thread related messages so a
search hit pivots to its whole thread. Today only `reply_to_id` links messages; there
is no thread identifier.

## Decision 1 — Archive tier stays in Postgres (not Delta) for now

From the archive analysis: the intended Delta tier (Native Lakehouse Sync, `wal2delta`
CDC → UC Delta SCD2) is **UI-only — no CLI/API** (confirmed via the Lakebase skill), so
it cannot be provisioned by `deploy.sh` and would violate the repo's no-manual-steps
rule. The shipped `partyline.messages_archive` Postgres table (archive-then-prune in one
transaction, federated to UC as `partyline_pg`) is atomic, automatable, and avoids the
open replication-slot × scale-to-zero risk. At current data volume (megabytes) the cost
difference is negligible, so the decision is driven by ops simplicity, not cost. Revisit
Delta only when Lakehouse Sync exposes an API **and** volume justifies columnar storage.

## Decision 2 — `conversation_id` threads messages

Add `conversation_id UUID` to `messages` (and `messages_archive`). Semantics:

- A post **with** `reply_to_id` **inherits** the referenced message's `conversation_id`.
- A post **without** `reply_to_id` **starts** a new conversation (fresh `gen_random_uuid()`).
- Computed in the INSERT: `COALESCE((SELECT conversation_id FROM messages WHERE id = reply_to_id), gen_random_uuid())`.
- Clients never set it directly; they only set `reply_to_id`. It is returned everywhere
  (`_MSG_COLS`, the NOTIFY/SSE payload) as text.

**Backfill (idempotent, fills NULLs only):**
- `messages`: a recursive CTE assigns each reply-tree (root = no resolvable parent) one
  UUID and propagates it down `reply_to_id` chains.
- `messages_archive`: each archived row gets its own UUID (archived history >30d is not
  re-threaded — a documented v1 tradeoff; low value, avoids cross-table chain rebuild).

## Decision 3 — Full-text search now, pgvector later

- **FTS (this change):** GIN index on `to_tsvector('english', coalesce(payload,''))` on
  both `messages` and `messages_archive`. Query with `websearch_to_tsquery` (supports
  quotes, OR, `-term`), rank with `ts_rank`, search live ∪ archive so history is covered.
- **Semantic (pgvector):** deferred follow-up — needs an embeddings pipeline + backfill;
  Lakebase supports the `vector` extension. Out of scope here.

## Decision 4 — Exposed on all three surfaces

- **DB:** `db.search_messages(query, limit)`, `db.get_conversation(conversation_id, limit)`.
- **REST:** `GET /api/search?q=&limit=` → ranked hits (each carries `conversation_id`,
  `source` = live|archive); `GET /api/conversations/{conversation_id}` → full thread (ASC).
- **MCP:** tools `search_messages(query, limit)` and `get_conversation(conversation_id)`.
- **Web:** a search box in the dashboard; results list each hit with its type/agent and a
  click that loads the full conversation thread.

## Migration & rollout

Schema changes are idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
NULL-only backfill) and run in `init_schema` on the next deploy/restart. Applies to the
live 346-row table instantly. One deploy ships schema + DB + REST + MCP + web together
(restarts the app — coordinate with the active fleet).

## Consequences

- History becomes inspectable by keyword and threadable end-to-end.
- `_MSG_COLS` gains `conversation_id::text`; the NOTIFY payload gains `conversation_id`,
  so the SSE/dashboard and the future channel can group by thread.
- Archived history is searchable but not re-threaded (per-row conversation ids).
