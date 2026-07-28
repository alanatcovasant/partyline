# Field-based Search/Filter + Lakebase 5-min Scale-to-Zero Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every read surface (dashboard, CLI, REST, MCP) one server-backed field-filtered query over the whole live+archive history, and drop Lakebase idle scale-to-zero from 15 min to 5 min.

**Architecture:** Add one shared `db.query_messages(**filters)` builder that ANDs optional filters over the live-∪-archive union (full-text `q` over summary+payload plus exact-match structured columns), rewrite the existing `search_messages` as a thin wrapper over it, and expose it through a new REST endpoint that the CLI, MCP tool, and dashboard `<Search/>` all drive. New search indexes ride the existing privileged migrate step. Lakebase suspend timeout changes via a constant.

**Tech Stack:** Python 3.10+ / FastAPI / psycopg3 / Postgres full-text search (`websearch_to_tsquery`, GIN `to_tsvector`); React + Vite (TypeScript SPA); stdlib-only CLI; `databricks-sdk` (`w.postgres`) for Lakebase; pytest + `@pytest.mark.anyio` (asyncio) against a local Postgres.

## Global Constraints

- **No manual steps.** All DDL (the new indexes) must apply automatically via `scripts/migrate_schema.py` at deploy time (`deploy.sh` step 1.5) as the table owner. The app's own `init_schema` will skip owner-only DDL (`InsufficientPrivilege`) — that skip is expected and non-fatal. Never add a "click in the UI" step.
- **Verify against the deployed app**, not just locally: `databricks bundle run covasant_partyline --profile dev-eastus2`. Clean up any test agents/messages posted to the live line.
- **Workspace/profile:** always pass `--profile dev-eastus2` to `databricks`.
- **Structured filters are exact-match**; fuzzy/partial matching is the job of the full-text `q` only. No per-column substring matching.
- **No pagination cursor** — `limit` only (default 50 for search surfaces, 500 for conversation reads).
- **No change** to the write path, message columns, retention job, SSE/channel push, or `MAX_CU`/`MIN_CU`.
- **Postgres GIN index rule:** the query's `to_tsvector(...)` expression must **exactly** match the index's expression or the index is not used. The full-text expression used everywhere is verbatim: `to_tsvector('english', coalesce(summary,'') || ' ' || coalesce(payload,''))`.
- **Tests live at repo root `tests/`** (not under `covasant-partyline/`). Run: `uv run pytest tests/ -v`. DB-backed tests skip cleanly when no local Postgres is listening (`docker compose up -d` to enable).

---

## File Structure

- `covasant-partyline/app/db.py` — **Modify.** Add `query_messages()`, its two helper constants (`_FTS_FULL_EXPR`), rewrite `search_messages()` as a wrapper, append search indexes to `_MIGRATIONS`.
- `covasant-partyline/app/rest.py` — **Modify.** Add `GET /api/messages/query`.
- `covasant-partyline/app/mcp_server.py` — **Modify.** Extend the `search_messages` tool with optional filter params.
- `partyline-client/covasant_partyline/cli.py` — **Modify.** Add `search` + `conversation` subcommands, handlers, and help text.
- `covasant-partyline/client/src/App.tsx` — **Modify.** Upgrade `<Search/>` to a field form hitting `/api/messages/query`.
- `scripts/provision_lakebase.py` — **Modify.** `SUSPEND_SECONDS = 900 → 300`; conditionally patch the live endpoint if the SDK now supports it.
- `tests/test_query_messages.py` — **Create.** DB-layer coverage of `query_messages` (seed via the `client` fixture, assert via `db.query_messages`).
- `tests/test_rest_api.py` — **Modify.** Add `/api/messages/query` endpoint tests + `/api/search` back-compat.
- `tests/test_cli.py` — **Create.** Pure unit tests (no network) for the CLI querystring builders.

---

### Task 1: `db.query_messages` shared builder, `search_messages` wrapper, and search indexes

**Files:**
- Modify: `covasant-partyline/app/db.py` (add `query_messages` + `_FTS_FULL_EXPR` after `_MSG_COLS_CONCISE` around line 507; rewrite `search_messages` at lines 640-665; append to `_MIGRATIONS` at line 346)
- Test: `tests/test_query_messages.py` (create)

**Interfaces:**
- Consumes: `get_pool()`, `_MSG_COLS`, `_MSG_COLS_CONCISE` (existing in `db.py`).
- Produces:
  ```python
  def query_messages(
      q: str | None = None,
      message_type: str | None = None,
      agent_id: str | None = None,
      author: str | None = None,
      recipient: str | None = None,
      mention: str | None = None,
      repo: str | None = None,
      branch: str | None = None,
      resource_path: str | None = None,
      issue_ref: str | None = None,
      conversation_id: str | None = None,
      since=None,
      until=None,
      limit: int = 50,
      include_payload: bool = True,
  ) -> list[dict]: ...
  ```
  Returns rows with the `_MSG_COLS` shape plus a `source` (`live`|`archive`) tag. Later tasks (REST/MCP) call this by keyword.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_messages.py`:

```python
"""DB-layer coverage for db.query_messages (the shared field-filtered query).

Seeds messages through the REST client fixture (which brings up a live local Postgres
and the connection pool), then asserts directly against db.query_messages. Skips cleanly
when no local Postgres is listening (see tests/conftest.py).
"""

import uuid

import pytest

from app import db


async def _post(client, agent_id, **fields):
    await client.post("/api/agents", json={"agent_id": agent_id, "owner": "alan", "repo": "partyline"})
    body = {"agent_id": agent_id, "message_type": fields.pop("message_type", "status")}
    body.update(fields)
    return (await client.post("/api/messages", json=body)).json()


@pytest.mark.anyio
async def test_query_by_issue_ref(client):
    tag = uuid.uuid4().hex[:8]
    posted = await _post(client, "qm_issue", issue_ref=tag, payload="issue-tagged")
    rows = db.query_messages(issue_ref=tag)
    assert [r["id"] for r in rows] == [posted["id"]]
    assert rows[0]["source"] == "live"


@pytest.mark.anyio
async def test_query_by_conversation_id(client):
    posted = await _post(client, "qm_conv", payload="conv-root")
    rows = db.query_messages(conversation_id=posted["conversation_id"])
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_q_matches_summary_only(client):
    """New behavior: full-text q now spans summary as well as payload."""
    token = "smrytok" + uuid.uuid4().hex[:6]
    posted = await _post(client, "qm_summary", summary=f"marker {token}", payload="unrelated body")
    rows = db.query_messages(q=token)
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_mention_array_contains(client):
    posted = await _post(client, "qm_mention", payload="hi", mentions=["target_x"])
    rows = db.query_messages(mention="target_x")
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_two_filters_anded(client):
    tag = uuid.uuid4().hex[:8]
    match = await _post(client, "qm_and", message_type="done", repo=tag, payload="a")
    await _post(client, "qm_and", message_type="status", repo=tag, payload="b")
    rows = db.query_messages(repo=tag, message_type="done")
    assert [r["id"] for r in rows] == [match["id"]]


@pytest.mark.anyio
async def test_query_no_filters_returns_newest_within_limit(client):
    ids = [(await _post(client, "qm_recent", payload=f"n{i}"))["id"] for i in range(3)]
    rows = db.query_messages(limit=2)
    assert len(rows) == 2
    assert rows == sorted(rows, key=lambda r: r["id"], reverse=True)  # newest-first
    assert rows[0]["id"] == max(ids)


@pytest.mark.anyio
async def test_query_limit_respected(client):
    for i in range(4):
        await _post(client, "qm_limit", repo="qm_limit_repo", payload=f"m{i}")
    rows = db.query_messages(repo="qm_limit_repo", limit=2)
    assert len(rows) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_query_messages.py -v`
Expected: FAIL with `AttributeError: module 'app.db' has no attribute 'query_messages'` (or skip if no local Postgres — start it with `docker compose up -d`, then re-run to get a real fail).

- [ ] **Step 3: Add `query_messages` and the FTS expression constant**

In `db.py`, immediately after `_MSG_COLS_CONCISE` (ends at line 507), add:

```python
# Full-text expression for query_messages. MUST match the idx_messages_fts_full /
# idx_archive_fts_full index expression EXACTLY, or Postgres will not use the GIN index.
_FTS_FULL_EXPR = "to_tsvector('english', coalesce(summary,'') || ' ' || coalesce(payload,''))"


def query_messages(
    q: str | None = None,
    message_type: str | None = None,
    agent_id: str | None = None,
    author: str | None = None,
    recipient: str | None = None,
    mention: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    resource_path: str | None = None,
    issue_ref: str | None = None,
    conversation_id: str | None = None,
    since=None,
    until=None,
    limit: int = 50,
    include_payload: bool = True,
) -> list[dict]:
    """Field-filtered search over the live + archive union (the shared query behind
    /api/messages/query, the CLI `search`, and the MCP search_messages tool).

    Every filter is optional and AND-combined. `q` is full-text over summary+payload
    (websearch syntax: quoted phrases, OR, -exclude); the structured filters are
    exact-match on their column; `mention` matches array membership; `since`/`until`
    bound `event_time`. A no-filter call returns the newest `limit` rows across
    live+archive.

    Ordering: relevance then recency when `q` is present (ts_rank DESC, id DESC), else
    newest-first (id DESC). Each row carries a `source` tag (live|archive).
    """
    cols = _MSG_COLS if include_payload else _MSG_COLS_CONCISE

    # Predicate list applied identically to both UNION ALL legs. Blank/None = not filtered.
    eq_filters = {
        "message_type": message_type, "agent_id": agent_id, "author": author,
        "recipient": recipient, "repo": repo, "branch": branch,
        "resource_path": resource_path, "issue_ref": issue_ref,
    }
    clauses: list[str] = []
    shared_params: list = []
    for col, val in eq_filters.items():
        if val is not None and val != "":
            clauses.append(f"{col} = %s")
            shared_params.append(val)
    if mention:
        clauses.append("%s = ANY(mentions)")
        shared_params.append(mention)
    if conversation_id:
        clauses.append("conversation_id = %s::uuid")
        shared_params.append(conversation_id)
    if since is not None:
        clauses.append("event_time >= %s")
        shared_params.append(since)
    if until is not None:
        clauses.append("event_time <= %s")
        shared_params.append(until)

    has_q = bool(q and q.strip())

    def leg(table: str, source: str) -> tuple[str, list]:
        leg_clauses = list(clauses)
        leg_params: list = []
        if has_q:
            # The tsquery param comes first in this leg (CROSS JOIN in FROM).
            leg_params.append(q.strip())
            leg_clauses.append(f"{_FTS_FULL_EXPR} @@ qq")
            rank = f"ts_rank({_FTS_FULL_EXPR}, qq)"
            frm = f"{table}, websearch_to_tsquery('english', %s) AS qq"
        else:
            rank = "0"
            frm = table
        leg_params += shared_params
        where = (" WHERE " + " AND ".join(leg_clauses)) if leg_clauses else ""
        return (f"SELECT {cols}, {rank} AS rank, '{source}' AS source FROM {frm}{where}", leg_params)

    live_sql, live_params = leg("messages", "live")
    arch_sql, arch_params = leg("messages_archive", "archive")
    order = "rank DESC, id DESC" if has_q else "id DESC"
    sql = f"SELECT * FROM ({live_sql} UNION ALL {arch_sql}) hits ORDER BY {order} LIMIT %s"
    with get_pool().connection() as conn:
        return conn.execute(sql, tuple(live_params + arch_params + [limit])).fetchall()
```

- [ ] **Step 4: Rewrite `search_messages` as a wrapper**

Replace the whole body of `search_messages` (lines 640-665) with:

```python
def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Full-text search over live + archived history (inspect history).

    Thin wrapper over query_messages(q=...). Note: the full-text index/expression now
    spans summary+payload, so this matches summary text too (an intended improvement over
    the old payload-only search). Each hit carries conversation_id (pivot to the full
    thread via get_conversation) and a `source` tag.
    """
    return query_messages(q=query, limit=limit)
```

- [ ] **Step 5: Append the search indexes to `_MIGRATIONS`**

At the end of the `_MIGRATIONS` list (after line 345, before the closing `]` on line 346), add:

```python
    # Full-text over summary+payload (the query_messages FTS expression). NEW indexes:
    # Postgres uses a GIN index only when the query's to_tsvector(...) matches the index
    # expression exactly, so these must mirror _FTS_FULL_EXPR verbatim. The old
    # payload-only idx_messages_fts / idx_archive_fts are left in place (harmless).
    "CREATE INDEX IF NOT EXISTS idx_messages_fts_full ON messages USING GIN (to_tsvector('english', coalesce(summary,'') || ' ' || coalesce(payload,'')))",
    "CREATE INDEX IF NOT EXISTS idx_archive_fts_full  ON messages_archive USING GIN (to_tsvector('english', coalesce(summary,'') || ' ' || coalesce(payload,'')))",
    # B-tree indexes for the high-selectivity structured filters.
    "CREATE INDEX IF NOT EXISTS idx_messages_repo      ON messages (repo)",
    "CREATE INDEX IF NOT EXISTS idx_messages_agent     ON messages (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_type      ON messages (message_type)",
    "CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (recipient)",
    "CREATE INDEX IF NOT EXISTS idx_archive_repo       ON messages_archive (repo)",
    "CREATE INDEX IF NOT EXISTS idx_archive_agent      ON messages_archive (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_archive_type       ON messages_archive (message_type)",
    "CREATE INDEX IF NOT EXISTS idx_archive_recipient  ON messages_archive (recipient)",
    # issue_ref: live already indexed (idx_messages_issue_ref in _SCHEMA); add archive side.
    "CREATE INDEX IF NOT EXISTS idx_archive_issue_ref  ON messages_archive (issue_ref)",
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_query_messages.py -v`
Expected: PASS (all 7). If it skips, run `docker compose up -d` first.

- [ ] **Step 7: Run the existing search test to confirm no regression**

Run: `uv run pytest tests/test_rest_api.py::test_search_finds_message_with_conversation -v`
Expected: PASS (the wrapper preserves the old `/api/search` behavior; payload matches still hit).

- [ ] **Step 8: Commit**

```bash
git add covasant-partyline/app/db.py tests/test_query_messages.py
git commit -m "feat(db): shared query_messages field filter + summary-spanning FTS indexes"
```

---

### Task 2: `GET /api/messages/query` REST endpoint

**Files:**
- Modify: `covasant-partyline/app/rest.py` (add a route after `search_messages` at line 163; `HTTPException` is already imported and used at line 185)
- Test: `tests/test_rest_api.py` (append)

**Interfaces:**
- Consumes: `db.query_messages(...)` from Task 1; `run_in_threadpool`, `HTTPException`, `router` (existing).
- Produces: `GET /api/messages/query` returning `list[dict]` (same row shape as `/api/search`, so the SPA `SearchHit` type covers it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rest_api.py`:

```python
@pytest.mark.anyio
async def test_query_endpoint_filters_by_issue_ref(client):
    await client.post("/api/agents", json={"agent_id": "q_issue_agent", "owner": "alan", "repo": "partyline"})
    posted = (await client.post("/api/messages", json={
        "agent_id": "q_issue_agent", "message_type": "status", "issue_ref": "GH-9931", "payload": "x",
    })).json()
    resp = await client.get("/api/messages/query", params={"issue_ref": "GH-9931"})
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()]
    assert posted["id"] in ids
    assert all(m["issue_ref"] == "GH-9931" for m in resp.json())


@pytest.mark.anyio
async def test_query_endpoint_blank_params_ignored(client):
    """Empty-string params are dropped (treated as not filtered), not matched literally."""
    await client.post("/api/agents", json={"agent_id": "q_blank_agent", "owner": "alan", "repo": "partyline"})
    await client.post("/api/messages", json={"agent_id": "q_blank_agent", "message_type": "status", "payload": "y"})
    resp = await client.get("/api/messages/query", params={"repo": "", "q": "", "limit": 5})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)  # returns newest rows, not zero from a blank match


@pytest.mark.anyio
async def test_query_endpoint_invalid_conversation_id_400(client):
    resp = await client.get("/api/messages/query", params={"conversation_id": "not-a-uuid"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_search_endpoint_still_returns_source_shape(client):
    """Back-compat: /api/search still works and returns the source-tagged shape."""
    await client.post("/api/agents", json={"agent_id": "bc_agent", "owner": "alan", "repo": "partyline"})
    token = "bctok" + "9182"
    posted = (await client.post("/api/messages", json={
        "agent_id": "bc_agent", "message_type": "status", "payload": f"backcompat {token}",
    })).json()
    resp = await client.get("/api/search", params={"q": token, "limit": 10})
    assert resp.status_code == 200
    match = [h for h in resp.json() if h["id"] == posted["id"]]
    assert match and match[0]["source"] == "live"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rest_api.py -k "query_endpoint or still_returns_source" -v`
Expected: the three `query_endpoint` tests FAIL with 404 (route not defined); `still_returns_source` PASSES (existing behavior).

- [ ] **Step 3: Add the endpoint**

In `rest.py`, immediately after the `search_messages` route (line 163), add:

```python
@router.get("/messages/query")
async def query_messages(
    q: str | None = None,
    message_type: str | None = None,
    agent_id: str | None = None,
    author: str | None = None,
    recipient: str | None = None,
    mention: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    resource_path: str | None = None,
    issue_ref: str | None = None,
    conversation_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Field-filtered query over live + archived history. Every param is optional and
    AND-combined; `q` is full-text (summary+payload), the rest are exact-match. A bare
    call returns the newest `limit` rows. Invalid conversation_id UUID -> 400."""
    def _clean(v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    try:
        rows = await run_in_threadpool(
            db.query_messages,
            q=_clean(q), message_type=_clean(message_type), agent_id=_clean(agent_id),
            author=_clean(author), recipient=_clean(recipient), mention=_clean(mention),
            repo=_clean(repo), branch=_clean(branch), resource_path=_clean(resource_path),
            issue_ref=_clean(issue_ref), conversation_id=_clean(conversation_id),
            since=_clean(since), until=_clean(until), limit=limit,
        )
    except Exception as exc:  # invalid uuid → clean 400 (same guard as /api/conversations)
        raise HTTPException(status_code=400, detail=f"invalid query: {exc}")
    log.info("query_messages endpoint hits=%s", len(rows))
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rest_api.py -k "query_endpoint or still_returns_source" -v`
Expected: all four PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/rest.py tests/test_rest_api.py
git commit -m "feat(rest): add GET /api/messages/query field-filtered endpoint"
```

---

### Task 3: Extend the MCP `search_messages` tool with filter params

**Files:**
- Modify: `covasant-partyline/app/mcp_server.py` (replace the `search_messages` tool at lines 157-169)
- Test: `tests/test_query_messages.py` (append a functional test)

**Interfaces:**
- Consumes: `db.query_messages(...)` from Task 1; `mcp`, `run_in_threadpool` (existing).
- Produces: an extended `search_messages` MCP tool (same name, more optional params). `get_conversation` is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_query_messages.py`:

```python
@pytest.mark.anyio
async def test_mcp_search_messages_accepts_filters(client):
    """The MCP tool delegates to query_messages and accepts structured filters."""
    from app import mcp_server

    tag = uuid.uuid4().hex[:8]
    posted = await _post(client, "mcp_filter_agent", issue_ref=tag, payload="mcp-filtered")
    rows = await mcp_server.search_messages(issue_ref=tag)
    assert posted["id"] in [r["id"] for r in rows]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_query_messages.py::test_mcp_search_messages_accepts_filters -v`
Expected: FAIL with `TypeError: search_messages() got an unexpected keyword argument 'issue_ref'`.

Note: `@mcp.tool()` in this repo's FastMCP returns the decorated function unchanged (`return fn`, verified in `mcp/server/fastmcp/__init__.py`), so `mcp_server.search_messages(...)` is directly callable as a coroutine — no tool-registry indirection needed.

- [ ] **Step 3: Extend the tool**

Replace the `search_messages` tool (lines 157-169) with:

```python
@mcp.tool()
async def search_messages(
    query: str = "",
    limit: int = 50,
    message_type: str | None = None,
    agent_id: str | None = None,
    author: str | None = None,
    recipient: str | None = None,
    mention: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    resource_path: str | None = None,
    issue_ref: str | None = None,
    conversation_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Search the whole line's history (live + archived), filtered by any field.

    `query` is full-text over summary+payload (quoted phrases, OR, -exclude — websearch
    syntax) and is optional, so this can be a pure structured filter. Every other arg is
    an exact-match filter on that column, AND-combined. `conversation_id` and `issue_ref`
    are the two common pivots ("everything in this thread" / "everything for this GH
    issue"). Results are ranked by relevance then recency; each hit carries
    `conversation_id` (pass to get_conversation for the full thread) and a `source`
    (live|archive).
    """
    log.info("search_messages query=%.60r limit=%s filters=%s", query, limit,
             {k: v for k, v in dict(message_type=message_type, agent_id=agent_id,
              repo=repo, issue_ref=issue_ref, conversation_id=conversation_id).items() if v})
    rows = await run_in_threadpool(
        db.query_messages,
        query, message_type, agent_id, author, recipient, mention, repo, branch,
        resource_path, issue_ref, conversation_id, since, until, limit,
    )
    log.info("search_messages OK returned=%s", len(rows))
    return rows
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_query_messages.py::test_mcp_search_messages_accepts_filters -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/mcp_server.py tests/test_query_messages.py
git commit -m "feat(mcp): extend search_messages tool with field filters"
```

---

### Task 4: CLI `search` + `conversation` commands

**Files:**
- Modify: `partyline-client/covasant_partyline/cli.py` (add `cmd_search`/`cmd_conversation` near the other `cmd_*`; register in `build_parser` after the `claims` parser ~line 350; update the module docstring Commands/Examples block, lines 13-31)
- Test: `tests/test_cli.py` (create)

**Interfaces:**
- Consumes: `_request`, `_print`, `build_parser` (existing in `cli.py`); `urllib.parse.urlencode`/`quote` (already imported).
- Produces: `partyline search [flags]` → `GET /api/messages/query?…`; `partyline conversation <id> [--limit]` → `GET /api/conversations/{id}?limit=…`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
"""Pure unit tests for the partyline CLI querystring builders (no network, no pool)."""

from urllib.parse import parse_qs, urlsplit

import pytest

from covasant_partyline import cli


def _capture(monkeypatch):
    calls = {}

    def fake_request(method, base, path, body=None):
        calls["method"], calls["path"] = method, path
        return {"ok": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    return calls


def test_search_builds_querystring_dropping_none(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(
        ["search", "--issue", "123", "--repo", "web", "--limit", "10"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert calls["method"] == "GET"
    parts = urlsplit(calls["path"])
    assert parts.path == "/api/messages/query"
    qs = parse_qs(parts.query)
    assert qs["issue_ref"] == ["123"]
    assert qs["repo"] == ["web"]
    assert qs["limit"] == ["10"]
    assert "agent_id" not in qs  # unset flags are dropped, not sent blank


def test_search_maps_text_flag_to_q(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(["search", "-q", "pool fix"])
    args.url = "http://localhost:8000"
    args.func(args)
    qs = parse_qs(urlsplit(calls["path"]).query)
    assert qs["q"] == ["pool fix"]


def test_conversation_builds_path(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(["conversation", "abc-123", "--limit", "50"])
    args.url = "http://localhost:8000"
    args.func(args)
    parts = urlsplit(calls["path"])
    assert parts.path == "/api/conversations/abc-123"
    assert parse_qs(parts.query)["limit"] == ["50"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `argparse` exits with "invalid choice: 'search'" (the subcommand isn't registered).

- [ ] **Step 3: Add the handlers**

In `cli.py`, after `cmd_claims` (line 124), add:

```python
def cmd_search(a: argparse.Namespace) -> None:
    params = {
        "q": a.text, "message_type": a.type, "agent_id": a.agent, "author": a.author,
        "recipient": a.recipient, "mention": a.mention, "repo": a.repo,
        "branch": a.branch, "resource_path": a.resource, "issue_ref": a.issue,
        "conversation_id": a.conversation, "since": a.since, "until": a.until,
        "limit": a.limit,
    }
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    _print(_request("GET", a.url, f"/api/messages/query?{qs}"))


def cmd_conversation(a: argparse.Namespace) -> None:
    qs = urllib.parse.urlencode({"limit": a.limit})
    _print(_request("GET", a.url, f"/api/conversations/{urllib.parse.quote(a.conversation_id)}?{qs}"))
```

- [ ] **Step 4: Register the subcommands**

In `build_parser`, after the `claims` parser line (line 350, `sub.add_parser("claims", …)`), add:

```python
    sp = sub.add_parser("search", help="Field-filtered search over live + archived history")
    sp.add_argument("--text", "-q", dest="text", help="Full-text query (summary+payload)")
    sp.add_argument("--type", dest="type", help="message_type")
    sp.add_argument("--agent", help="agent_id")
    sp.add_argument("--author")
    sp.add_argument("--recipient")
    sp.add_argument("--mention")
    sp.add_argument("--repo")
    sp.add_argument("--branch")
    sp.add_argument("--resource", help="resource_path")
    sp.add_argument("--issue", help="issue_ref")
    sp.add_argument("--conversation", help="conversation_id")
    sp.add_argument("--since", help="event_time lower bound (ISO-8601)")
    sp.add_argument("--until", help="event_time upper bound (ISO-8601)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("conversation", help="Read a full thread by conversation_id")
    sp.add_argument("conversation_id")
    sp.add_argument("--limit", type=int, default=500)
    sp.set_defaults(func=cmd_conversation)
```

- [ ] **Step 5: Update the module docstring**

In the `Commands:` list (lines 13-22) add these two lines after the `claims` line:

```
  search     field-filtered search over live + archived history
  conversation  read a full thread by conversation_id
```

In the `Examples:` block (lines 24-31) add after the `unread` example:

```
  partyline search --issue 123 --repo web
  partyline search -q "pool fix" --type done
  partyline conversation 3f2a1b4c-...-...
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all three PASS.

- [ ] **Step 7: Commit**

```bash
git add partyline-client/covasant_partyline/cli.py tests/test_cli.py
git commit -m "feat(cli): add search + conversation commands"
```

---

### Task 5: Dashboard `<Search/>` field form

**Files:**
- Modify: `covasant-partyline/client/src/App.tsx` (replace the `<Search/>` component, lines 304-387; reuse `MESSAGE_TYPES` at line 200, `SearchHit` at line 24, `getJSON` at line 82, existing `MsgBody`)
- Gate: TypeScript build (`npm run build`) — the client has no JS test runner, so the type-check is the automated gate; a live dashboard check follows in Task 7.

**Interfaces:**
- Consumes: `GET /api/messages/query` (Task 2), `GET /api/conversations/{id}` (existing), `SearchHit`, `MESSAGE_TYPES`, `getJSON`, `MsgBody`.
- Produces: no new exported symbols; `<Search/>` keeps its call site in the app shell.

- [ ] **Step 1: Replace the `<Search/>` component**

Replace lines 304-387 (`function Search() { … }`) with:

```tsx
function Search() {
  const [q, setQ] = useState("");
  const [mtype, setMtype] = useState("");
  const [repo, setRepo] = useState("");
  const [agent, setAgent] = useState("");
  const [recipient, setRecipient] = useState("");
  const [issue, setIssue] = useState("");
  const [branch, setBranch] = useState("");
  const [conversation, setConversation] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [thread, setThread] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  const anyFilter = [q, mtype, repo, agent, recipient, issue, branch, conversation]
    .some((v) => v.trim());

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!anyFilter) return;
    setBusy(true);
    setThread(null);
    const params = new URLSearchParams();
    const add = (k: string, v: string) => v.trim() && params.set(k, v.trim());
    add("q", q);
    add("message_type", mtype);
    add("repo", repo);
    add("agent_id", agent);
    add("recipient", recipient);
    add("issue_ref", issue);
    add("branch", branch);
    add("conversation_id", conversation);
    params.set("limit", "50");
    try {
      setHits(await getJSON<SearchHit[]>(`/api/messages/query?${params.toString()}`));
    } catch {
      setHits([]);
    } finally {
      setBusy(false);
    }
  }

  function clearAll() {
    setHits(null);
    setThread(null);
    setQ(""); setMtype(""); setRepo(""); setAgent("");
    setRecipient(""); setIssue(""); setBranch(""); setConversation("");
  }

  async function openThread(cid?: string | null) {
    if (!cid) return;
    try {
      setThread(await getJSON<SearchHit[]>(`/api/conversations/${cid}`));
    } catch {
      setThread([]);
    }
  }

  const row = (m: SearchHit) => (
    <li key={`${m.source ?? "live"}-${m.id}`}>
      <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
      <b>{m.agent_id}</b>
      {m.recipient && <span className="muted"> → {m.recipient}</span>}
      {m.source === "archive" && <span className="muted"> (archived)</span>}
      <MsgBody m={m} />
      <time>{new Date(m.created_at).toLocaleString()}</time>
    </li>
  );

  return (
    <section className="search">
      <form className="search-row" onSubmit={run}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder='🔎 text — "quoted phrase", OR, -exclude (summary + payload)'
          aria-label="Full-text search"
        />
        <select value={mtype} onChange={(e) => setMtype(e.target.value)} aria-label="Type">
          <option value="">any type</option>
          {MESSAGE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="repo" aria-label="repo" />
        <input value={agent} onChange={(e) => setAgent(e.target.value)} placeholder="agent_id" aria-label="agent_id" />
        <input value={recipient} onChange={(e) => setRecipient(e.target.value)} placeholder="recipient" aria-label="recipient" />
        <input value={issue} onChange={(e) => setIssue(e.target.value)} placeholder="issue_ref" aria-label="issue_ref" />
        <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="branch" aria-label="branch" />
        <input value={conversation} onChange={(e) => setConversation(e.target.value)} placeholder="conversation_id" aria-label="conversation_id" />
        <button type="submit" disabled={busy || !anyFilter}>{busy ? "…" : "Search"}</button>
        {(hits || thread) && (
          <button type="button" className="ghost" onClick={clearAll}>clear</button>
        )}
      </form>

      {thread ? (
        <div className="search-thread">
          <button type="button" className="ghost" onClick={() => setThread(null)}>← back to results</button>
          <ul>{thread.map(row)}{!thread.length && <li className="muted">Empty thread.</li>}</ul>
        </div>
      ) : hits ? (
        <ul className="search-results">
          {hits.map((m) => (
            <li key={`${m.source ?? "live"}-${m.id}`}>
              <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
              <b>{m.agent_id}</b>
              {m.source === "archive" && <span className="muted"> (archived)</span>}
              <MsgBody m={m} />
              {m.conversation_id && (
                <button type="button" className="link" onClick={() => openThread(m.conversation_id)}>
                  view thread →
                </button>
              )}
            </li>
          ))}
          {!hits.length && <li className="muted">No matches.</li>}
        </ul>
      ) : null}
    </section>
  );
}
```

- [ ] **Step 2: Type-check / build the SPA**

Run: `npm --prefix covasant-partyline/client run build`
Expected: build succeeds with no TypeScript errors. (If the type-check fails, fix the reported type mismatch before continuing.)

- [ ] **Step 3: Commit**

```bash
git add covasant-partyline/client/src/App.tsx
git commit -m "feat(dashboard): field-aware Search form over /api/messages/query"
```

---

### Task 6: Lakebase 5-minute scale-to-zero

**Files:**
- Modify: `scripts/provision_lakebase.py` (`SUSPEND_SECONDS` at line 30; the suspend-drift block at lines 106-117; the project-default block already uses the constant at lines 130-140)

**Interfaces:**
- Consumes: `w.postgres` SDK, `pg.FieldMask`, `pg.Endpoint`, `pg.EndpointSpec` (existing in the script).
- Produces: 5-minute suspend as the project default (new/recreated endpoints inherit) and, if the SDK now supports it, on the live `primary` endpoint immediately.

- [ ] **Step 1: Change the constant**

In `provision_lakebase.py`, replace lines 28-30:

```python
# Suspend after 5 min idle (PRD G5/NFR4 cheap-at-rest). The protocol is fail-open, so a
# cold start only delays awareness, never blocks work; agents warm it on first call.
SUSPEND_SECONDS = 300
```

- [ ] **Step 2: Verify whether the live-endpoint suspend timeout is now patchable**

Run this SDK inspection against the current environment:

```bash
python -c "from databricks.sdk.service import postgres as pg; import inspect; print('suspend_timeout_duration' in inspect.getsource(pg.EndpointSpec))"
```

Then check whether `update_endpoint` accepts a `spec.suspend_timeout_duration` mask path by reading the current SDK/Lakebase API docs for the workspace (use the databricks / databricks-apps skill or `w.postgres.update_endpoint`'s accepted masks). Record the answer (yes/no) — it decides Step 3.

- [ ] **Step 3a (only if patchable): patch the live endpoint in the reconcile block**

Replace the suspend-flag block (lines 106-117, the `current_suspend = …` through the `[!]` print) with:

```python
        current_suspend = getattr(
            getattr(st, "suspend_timeout_duration", None), "seconds", None
        )
        if current_suspend != SUSPEND_SECONDS:
            print(
                f"[~] reconciling endpoint {endpoint_id} suspend timeout: "
                f"{current_suspend}s -> {SUSPEND_SECONDS}s"
            )
            mask = pg.FieldMask()
            mask.paths = ["spec.suspend_timeout_duration"]
            w.postgres.update_endpoint(
                name=ENDPOINT_NAME,
                endpoint=pg.Endpoint(spec=desired),
                update_mask=mask,
            )
        else:
            print(f"[ok] endpoint {endpoint_id} suspend timeout matches ({SUSPEND_SECONDS}s)")
```

- [ ] **Step 3b (only if NOT patchable): keep the honest flag**

Leave lines 106-117 as they are — the `[!]` "not updatable in place; will apply on endpoint recreation" message is the honest signal. Only the comment's stale "15 min" needs no change (it references the API limitation, not the value). Do **not** claim the live endpoint changed.

- [ ] **Step 4: Dry-run the provisioner against dev-eastus2**

Run: `DATABRICKS_CONFIG_PROFILE=dev-eastus2 python scripts/provision_lakebase.py`
Expected: prints `[~] setting project default endpoint settings (suspend 300s)` (or `[ok] … match` on a second run), and either the `[~] reconciling … suspend timeout … -> 300s` line (3a) or the `[!] … not updatable in place` flag (3b). The script must remain idempotent — a second run prints only `[ok]` lines for anything already at 300s.

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_lakebase.py
git commit -m "chore(lakebase): idle scale-to-zero 15min -> 5min"
```

---

### Task 7: Deploy and live verification

**Files:** none (integration). Uses `deploy.sh` and the deployed app per CLAUDE.md.

**Interfaces:** Consumes everything above end-to-end.

- [ ] **Step 1: Run the full local suite**

Run: `uv run pytest tests/ -v`
Expected: green (DB-backed tests skip cleanly if no local Postgres; start it with `docker compose up -d` to run them for real).

- [ ] **Step 2: Coordinate the shared-surface edits on the line**

`scripts/migrate_schema.py` runs `db.py`'s `_MIGRATIONS` (indexes) and `deploy.sh` are shared surfaces. Before deploying, register and claim per CLAUDE.md:

```bash
partyline register alan_partyline_1 --owner alan --repo covasant-partyline --branch develop
partyline unread alan_partyline_1 --mine
partyline claim alan_partyline_1 covasant-partyline app/db.py --ttl 900
```

Post a `status` that you're deploying the search-index migration; `release` when done.

- [ ] **Step 3: Deploy**

Run: `./deploy.sh dev-eastus2 dev`
Expected: provision (5-min default) → migrate as owner (the new `idx_*_fts_full` / structured indexes land — watch for their `CREATE INDEX` in the migrate output) → build SPA → deploy → restart. The app's own `init_schema` will log the same indexes as skipped (`InsufficientPrivilege`) — expected and non-fatal.

- [ ] **Step 4: Verify the live pivots**

```bash
databricks bundle run covasant_partyline --profile dev-eastus2   # confirm app is up
# Post a throwaway probe, then query it back by field:
partyline post alan_partyline_1 --type status --issue PLTEST-1 --summary "search probe" --payload "field-search live check"
partyline search --issue PLTEST-1
partyline search -q "field-search live check"
```
Expected: both queries return the probe message with `source: live`. Take the probe's `conversation_id` from the output and confirm `partyline conversation <cid>` returns the thread.

- [ ] **Step 5: Clean up the live line**

Archive or remove the probe message and any `PLTEST-1` test data so the line stays clean (per CLAUDE.md). Release the claim:

```bash
partyline release alan_partyline_1 covasant-partyline app/db.py
partyline post alan_partyline_1 --type done --summary "field search + 5min scale-to-zero deployed"
```

- [ ] **Step 6: Confirm scale-to-zero setting on the live endpoint**

Re-run `DATABRICKS_CONFIG_PROFILE=dev-eastus2 python scripts/provision_lakebase.py` and confirm it reports 300s as the project default (and, if Task 6 Step 3a applied, the live endpoint at 300s; otherwise the honest `[!]` recreation flag).

---

## Notes for the whitepaper review (deferred — after implementation)

Per the original request, after this ships, review `c:\Users\alana\Downloads\whitepaper_draft_refined.md` for adjustments the current tool state warrants (e.g. the new field-search capability across surfaces, and the 5-min scale-to-zero posture). Not part of this plan's tasks.
