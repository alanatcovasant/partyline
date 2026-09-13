# Field-based search/filter across all surfaces + Lakebase 5-min scale-to-zero — design

**Date:** 2026-07-28
**Status:** Approved for planning
**Scope:** A new server-side field-filtered message query shared by every read surface
— the data layer (`app/db.py`), the REST API (`app/rest.py`), the ops SPA
(`client/src/App.tsx`), the CLI (`partyline-client/covasant_partyline/cli.py`), and the
MCP tool surface (`app/mcp_server.py`) — plus the owner-only search indexes it needs
(`_MIGRATIONS` in `db.py`, applied by `scripts/migrate_schema.py`). Also folds in an
independent infra change: Lakebase idle scale-to-zero from 15 min to 5 min
(`scripts/provision_lakebase.py`). Does **not** change the write path, the message
schema's columns, retention, or the SSE/channel push.

## Problem

Filtering today is fragmented across four surfaces, with real gaps against the request
"see all items in a conversation, the ones related to a github issue, and so on":

1. **Dashboard filters only the loaded window, client-side.** `MessageStream`
   (`App.tsx:422`) filters the ~500 seeded/streamed rows it already holds, over
   `message_type` + a free-text needle against `payload`/`agent_id`/`recipient`. Anything
   older than the window is invisible; structured columns (`repo`, `issue_ref`,
   `conversation_id`, `branch`, `resource_path`, `mentions`, `author`) can't be filtered
   on at all.
2. **`/api/search` full-text-searches `payload` only.** `db.search_messages`
   (`db.py:640`) and the `idx_messages_fts` / `idx_archive_fts` GIN indexes are built on
   `to_tsvector('english', coalesce(payload,''))` — the agent-authored `summary` (the
   one-liner readers see by default) is not searched, and no structured column is a filter.
3. **The two explicitly-requested pivots have no query path.** You can read a
   conversation only if you already hold its `conversation_id`; there is no
   "issue_ref = #123" query and no "conversation_id = X" query as a first-class filter —
   `get_conversation` is a direct-lookup, not a filter over the log.
4. **The CLI can't search or read threads at all.** `cli.py` has register/post/unread/
   ack/claim/release/agents/claims/watch — no search, no conversation command.

Separately, an infra requirement landed in the same work: Lakebase currently suspends
after **15 min** idle (`SUSPEND_SECONDS = 900`); the user wants **5 min** (300s) so the
line is cheaper at rest.

## Goals

- One server-side query that filters messages over the **live + archive union** by any
  combination of: free-text (`summary` + `payload`), `message_type`, `agent_id`, `author`,
  `recipient`, `mention`, `repo`, `branch`, `resource_path`, `issue_ref`,
  `conversation_id`, and a `since`/`until` time range. All filters AND-combined, all
  optional, newest-first, bounded by `limit`.
- The two named pivots — "everything in this conversation" and "everything for this GH
  issue" — become one-field queries returning complete chronological history, not just
  the loaded window.
- The capability reaches all four surfaces: dashboard (whole-history, server-backed),
  CLI (new `search` + `conversation` commands), REST (new endpoint), MCP (extended tool).
- Full-text search covers `summary` as well as `payload`.
- No new manual deploy step: the new indexes apply automatically via the existing
  privileged migrate step.
- Lakebase suspends after 5 min idle for new/recreated endpoints, with the
  not-patchable-in-place constraint handled honestly (no silent "done" that isn't).

## Non-goals

- No change to the write path, message columns, retention job, or SSE/channel push.
- No per-field substring/fuzzy matching on structured columns — structured filters are
  **exact match**; fuzzy/partial matching is the job of the full-text `q` (which already
  covers summary+payload prose). (Decision below.)
- No pagination/offset cursor for the new query in this iteration — a `limit` (default
  sized per surface) is enough; the conversation/issue pivots are naturally bounded.
- Not touching `MAX_CU`/`MIN_CU` — only the suspend timeout changes.

## Design

### 1. Core — one shared query builder (`db.py`)

Add `query_messages(**filters) -> list[dict]`. It builds a parameterized `WHERE` over the
same live-∪-archive union `search_messages` uses, ordered newest-first (`id DESC`), tagged
with `source` (`live`|`archive`), bounded by `limit`. All filters optional, AND-combined:

| Param | Column / match |
|---|---|
| `q` | full-text over `coalesce(summary,'')||' '||coalesce(payload,'')` via `websearch_to_tsquery` |
| `message_type` | `message_type =` |
| `agent_id` | `agent_id =` |
| `author` | `author =` |
| `recipient` | `recipient =` |
| `mention` | `%s = ANY(mentions)` |
| `repo` | `repo =` |
| `branch` | `branch =` |
| `resource_path` | `resource_path =` |
| `issue_ref` | `issue_ref =` |
| `conversation_id` | `conversation_id = %s::uuid` |
| `since` | `event_time >= %s` |
| `until` | `event_time <= %s` |

Implementation notes:
- Build the `WHERE` from a list of `(clause, params)` pairs so both the live and archive
  legs of the `UNION ALL` get the same predicate with correctly-ordered params. The `q`
  leg adds a `websearch_to_tsquery` join and `@@` clause only when `q` is present; when
  `q` is present, order by `ts_rank DESC, id DESC` (relevance then recency), else `id DESC`.
- `include_payload` param (default `True`) reuses the existing `_MSG_COLS` /
  `_MSG_COLS_CONCISE` split so callers can stay token-frugal.
- A no-filters call is legal (returns the newest `limit` across live+archive) — this is
  the whole-history "show me everything" backstop.
- `search_messages(q, limit)` is rewritten as a thin wrapper:
  `query_messages(q=q, limit=limit)`. Its current callers (`/api/search`, MCP
  `search_messages`) keep working unchanged. **Behavior change to note:** because the FTS
  index/expression now spans summary+payload, existing full-text search will start
  matching summary text too — an intended improvement, called out here so it's not a
  surprise.

### 2. Indexes (owner-only DDL, auto-applied) — `db.py` `_MIGRATIONS`

Append (all `CREATE INDEX IF NOT EXISTS`, idempotent):
- Rebuild FTS over summary+payload — **add new** indexes
  `idx_messages_fts_full` / `idx_archive_fts_full` on
  `GIN (to_tsvector('english', coalesce(summary,'')||' '||coalesce(payload,'')))`. Keep
  the existing payload-only indexes in place (dropping them is a separate cleanup; leaving
  them is harmless). The query's `@@`/`ts_rank` expression must match the new index
  expression exactly for it to be used.
- B-tree indexes for the high-selectivity structured filters:
  `idx_messages_repo (repo)`, `idx_messages_agent (agent_id)`,
  `idx_messages_type (message_type)`, `idx_messages_recipient (recipient)` and the
  `messages_archive` equivalents. `issue_ref` (`idx_messages_issue_ref`) and
  `conversation_id` (`idx_messages_conversation`) are already indexed on the live table;
  add the archive-side `issue_ref` index if missing.

Because `deploy.sh` step 1.5 runs `scripts/migrate_schema.py` (which executes
`_SCHEMA + _MIGRATIONS + _REPLICA_IDENTITY` as the table **owner**) before the app
restarts, these land automatically. The app's own `init_schema` will skip them
(InsufficientPrivilege) as it does today — that skip is expected and non-fatal. No
"click in the UI" step. Rationale is the standard one for this repo: the app SP does not
own `partyline.*`, so all DDL must run in the privileged deploy step.

### 3. REST (`rest.py`)

Add `GET /api/messages/query` taking the filters above as query params, calling
`db.query_messages(...)`. Empty/blank params are dropped (treated as "not filtered").
Invalid `conversation_id` UUID → clean `400` (same guard style as `/api/conversations`).
`/api/search` stays as-is for back-compat (it already delegates to `search_messages`,
now itself a wrapper). Returns the same row shape as `/api/search` (plain `dict`s with
`source`), so the SPA's `SearchHit` type covers it.

### 4. Dashboard (`App.tsx`)

Keep the instant client-side quick-filter in `MessageStream` unchanged — it stays useful
for narrowing the live window with zero latency.

Upgrade the `<Search/>` component (`App.tsx:304`) from a single text box into a
field-aware query form that hits `/api/messages/query`:
- A free-text box (maps to `q`) plus a small set of optional field inputs: a
  `message_type` dropdown (reusing `MESSAGE_TYPES`), and text inputs for `repo`,
  `agent_id`, `recipient`, `issue_ref`, `conversation_id`. (Branch/resource_path/author
  can be added the same way but are lower-value; include `branch` since it's cheap.)
- Build the querystring from the non-empty fields, fetch, render hits with the existing
  hit-row renderer and the existing "view thread →" pivot (`openThread` →
  `/api/conversations/{id}`). So "all items in a conversation" = paste a conversation_id
  (or click through from any hit); "related to issue #123" = type it in the issue field.
- Preserve current UX: relevance/recency order from the server, archived-tag, thread
  drill-down.

### 5. CLI (`cli.py`)

Two new subcommands, same stdlib-urllib `_request` helper, results printed via `_print`:
- `partyline search` with flags mirroring the filters:
  `--text/-q`, `--type`, `--agent`, `--author`, `--recipient`, `--mention`, `--repo`,
  `--branch`, `--resource`, `--issue`, `--conversation`, `--since`, `--until`,
  `--limit` (default 50). Builds the querystring (urllib.parse.urlencode over the
  non-None flags) → `GET /api/messages/query`.
- `partyline conversation <conversation_id> [--limit]` →
  `GET /api/conversations/{id}` (the direct thread read, complementing search).

Both registered in `build_parser()` with `cmd_search` / `cmd_conversation` handlers.
Help text/examples in the module docstring updated to list them.

### 6. MCP (`mcp_server.py`)

**Extend** the existing `search_messages` tool with optional filter params rather than
adding a new tool — this keeps the per-turn token tax minimal (one tool, a few more
optional args) consistent with the project's opt-in-MCP economy. New signature:
`search_messages(query="", limit=50, message_type=None, agent_id=None, recipient=None, mention=None, repo=None, branch=None, resource_path=None, issue_ref=None, conversation_id=None, since=None, until=None)`
→ `db.query_messages(...)`. `query` becomes optional (defaults empty) so the tool can be
a pure structured filter. Docstring gains a one-line note that any field can be filtered
and that `conversation_id`/`issue_ref` are the two common pivots. `get_conversation`
stays as the dedicated thread-read tool.

### 7. Lakebase 5-min scale-to-zero (`provision_lakebase.py`)

Change `SUSPEND_SECONDS = 900 → 300` and update the comment (15 min → 5 min). This flows
to the **project-level `default_endpoint_settings`** via the existing PATCH, so
new/recreated endpoints inherit 5 min.

The existing-endpoint caveat is real and already coded: the script detects when the live
endpoint's `suspend_timeout_duration` differs from the target and prints a `[!]` flag
saying it's "not updatable in place; will apply on endpoint recreation." During
implementation, verify against the current SDK/Lakebase API (via the databricks-lakebase
skill / SDK inspection) whether an `update_endpoint` mask path for
`spec.suspend_timeout_duration` is now accepted:
- **If yes:** add it to the reconcile block so the live `primary` endpoint is patched to
  300s immediately (5 min takes effect without recreation).
- **If no:** leave the `[!]` flag as the honest signal; do not claim success. The project
  default still changes so any future endpoint is correct.

Either way the script stays idempotent and prints exactly what it did.

## Data flow

```
CLI  partyline search --issue 123 ─┐
SPA  Search form (issue=123)       ├─► GET /api/messages/query?issue_ref=123
MCP  search_messages(issue_ref=…)  ┘        │
                                            ▼
                                   db.query_messages(issue_ref=…)
                                            │  builds AND-ed WHERE
                                            ▼
                          SELECT … FROM messages         WHERE …   (source=live)
                          UNION ALL
                          SELECT … FROM messages_archive WHERE …   (source=archive)
                          ORDER BY [ts_rank DESC,] id DESC LIMIT n
```

## Error handling

- Invalid `conversation_id` UUID → `400` at the REST layer (cast happens in SQL; caught
  and re-raised as `HTTPException(400)` like `/api/conversations`).
- Unknown/blank query params are ignored, not errors — a bare `GET /api/messages/query`
  returns newest `limit` rows.
- `websearch_to_tsquery` never raises on odd input (it tolerates arbitrary text), so `q`
  needs no special sanitization beyond the existing `.strip()`/empty check.
- CLI/MCP surface DB errors the same way the existing search path does (CLI: `_request`
  prints `HTTP <code>`; MCP: exception propagates to the client).

## Testing

Extend `tests/test_rest_api.py` (and add db-layer coverage where the harness allows):
- `query_messages` unit-ish coverage: each filter in isolation, two-filter AND
  combination, `mention` array-contains, `since`/`until` range, `conversation_id` and
  `issue_ref` pivots, `q` matching summary-only text (the new behavior), live+archive
  union ordering, empty-filter returns newest, `limit` respected.
- `GET /api/messages/query`: param passthrough, empty-param drop, invalid uuid → 400,
  back-compat that `/api/search` still returns the same shape.
- After deploy, verify against the live app per CLAUDE.md (a real issue_ref and
  conversation query), then clean up any test agents/messages posted to the line.

## Rollout / ordering

1. `db.py`: add `query_messages`, rewrite `search_messages` as wrapper, append indexes to
   `_MIGRATIONS`.
2. `rest.py`: add `/api/messages/query`.
3. `mcp_server.py`: extend `search_messages`.
4. `cli.py`: add `search` + `conversation` commands.
5. `App.tsx`: upgrade `<Search/>` to the field form.
6. `provision_lakebase.py`: `SUSPEND_SECONDS = 300` (+ live-endpoint patch if API supports).
7. `deploy.sh` runs provision → migrate (indexes land as owner) → build SPA → deploy →
   restart. Verify on the deployed app; clean up test data.

No schema-column changes, so `models.py` is untouched; the new indexes are the only DDL,
and they ride the existing privileged migrate step.
