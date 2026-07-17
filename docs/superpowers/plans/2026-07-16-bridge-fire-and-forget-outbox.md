# Bridge Fire-and-Forget Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sending on the local MCP bridge fire-and-forget so an agent never blocks (or burns tokens) on a hung send, with durable file fallback, delivery tickets, fast-fail reads, and truthful timestamps.

**Architecture:** Send verbs (`post_message`, `reply`, `ack_checkpoint`, `register_agent`, `release_resource`) are intercepted locally in the bridge, enqueued to an in-process outbox, and return a uuid ticket instantly. A background worker drains the outbox via plain REST `POST` with bounded retries, marking each ticket sent/failed and spilling to a config'd YAML file on give-up. Reads and `claim_resource` stay proxied but fast-fail (~8s). Timestamps split into three clocks (`event_time`/`received_at`/`created_at`) so a delayed/reposted message reads in its true slot.

**Tech Stack:** Python 3.10+, `asyncio`, `mcp` low-level Server, `databricks-sdk` Config (auth), stdlib `urllib` (REST send), `psycopg` (Postgres/Lakebase), `pytest`. Optional `tiktoken` for token estimates (graceful fallback to chars/4).

## Global Constraints

- **No manual infra steps.** Schema changes ship as idempotent DDL in `db.py` `_MIGRATIONS`, applied by `scripts/migrate_schema.py` at deploy (app SP cannot run DDL — it is not the table owner). Never add a "click in the UI" step.
- **Bridge stays stdlib + existing deps only** on the send path — the fire-and-forget sender uses `urllib` (mirrors `channel.py.post_reply`), not new packages. `tiktoken` is import-guarded and optional.
- **Fail-silent contracts hold:** a spool-write failure is logged loud on stderr but never raised; diagnostics never break a call.
- **Databricks Apps reject PATs** — auth is OAuth via `databricks.sdk.core.Config` (already enforced in `bridge._auth_headers`); the sender reuses `Config.authenticate()`.
- **Migrations additive only:** `ADD COLUMN IF NOT EXISTS`, NULL-only backfill; also ALTER `messages_archive` explicitly (it is `LIKE messages INCLUDING ALL`, which copies schema only at creation).
- **Shared surfaces** (`db.py` schema, `scripts/migrate_schema.py`, `resources/*.yml`, `deploy.sh`, retention job): `claim_resource` on the live line before editing, per CLAUDE.md.
- **Verify against the deployed app** (`databricks bundle run covasant_partyline`), not just locally; clean up any test agents/messages posted to the live line.
- Estimated-token fields are always prefixed `est_` — never presented as a real count.

---

## File Structure

**New files:**
- `partyline-client/covasant_partyline/outbox.py` — the in-process outbox queue, ticket table, spool-path resolution, and YAML spool writer (pure/testable units, no MCP or asyncio server coupling).
- `partyline-client/covasant_partyline/sender.py` — the REST sender (one POST per verb) and the background drain worker with backoff→spool.
- `partyline-client/covasant_partyline/metrics.py` — token/byte estimation + the `partyline.metrics` structured logger and startup footprint report.
- `partyline-client/tests/test_outbox.py`, `test_sender.py`, `test_metrics.py`, `test_bridge_dispatch.py` — unit tests for the above + the bridge dispatch split.

**Modified files:**
- `covasant-partyline/app/db.py` — add `event_time`/`received_at` to schema + `_MIGRATIONS`; add `event_time` param to `post_message`; add columns to `_MSG_COLS`; order reads by `event_time`.
- `covasant-partyline/app/models.py` — add `event_time` to `PostMessage`/`Message`.
- `covasant-partyline/app/rest.py` — accept and pass `event_time`; stamp `received_at`.
- `partyline-client/covasant_partyline/bridge.py` — agent_id resolution; local send intercept; `check_send` tool; fast-fail sync timeouts; non-blocking startup; sender lifecycle; wire diagnostics.
- `tests/test_schema_bootstrap.py` — assert the new migration columns.

**Dependency order:** DB/timestamps (Tasks 1–3) → outbox/spool units (Tasks 4–6) → sender (Task 7) → bridge wiring (Tasks 8–11) → diagnostics (Tasks 12–13).

---

### Task 1: Three-clock schema migration (DB)

**Files:**
- Modify: `covasant-partyline/app/db.py` — **both** DDL sites: the `_SCHEMA` `messages` `CREATE TABLE` (~line 225–246, the source of truth for fresh DBs; archive inherits via `LIKE`) AND the `_MIGRATIONS` list (~line 297–326, for existing DBs).
- Test: `tests/test_schema_bootstrap.py`

**Interfaces:**
- Produces: `messages.event_time TIMESTAMPTZ`, `messages.received_at TIMESTAMPTZ`, and the same on `messages_archive`; declared in the base `CREATE TABLE` (fresh DBs) and added via idempotent ALTER + backfilled to `created_at` for existing rows.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schema_bootstrap.py`. The DDL must be correct in **both** places: the
base `CREATE TABLE` in `_SCHEMA` (the source of truth — a fresh DB gets its columns here,
and `messages_archive` inherits them via `LIKE messages INCLUDING ALL`), AND the
`_MIGRATIONS` block (which brings already-provisioned DBs up to date). Test asserts both:

```python
def test_schema_create_table_declares_three_clocks():
    # The base CREATE TABLE is the source of truth: fresh DBs get the columns here, and
    # messages_archive (LIKE messages INCLUDING ALL) only inherits them if declared here.
    from app.db import _SCHEMA
    norm = " ".join(_SCHEMA.split())
    assert "event_time TIMESTAMPTZ" in norm
    assert "received_at TIMESTAMPTZ" in norm


def test_migrations_add_three_clock_columns():
    from app.db import _MIGRATIONS
    joined = " ".join(" ".join(m.split()) for m in _MIGRATIONS)
    # event_time + received_at added to both live and archive tables (existing DBs)
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ" in joined
    # existing rows backfilled so ordering stays sane
    assert "SET event_time = created_at" in joined
    assert "SET received_at = created_at" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd covasant-partyline && python -m pytest ../tests/test_schema_bootstrap.py -k three_clock -v`
Expected: FAIL (columns not in `_SCHEMA` and not in `_MIGRATIONS`).

- [ ] **Step 3a: Adjust the base CREATE TABLE DDL (source of truth)**

In `covasant-partyline/app/db.py`, edit the `messages` `CREATE TABLE` inside `_SCHEMA`.
Change the final column line so it ends with a comma and adds the two clocks. Replace:

```sql
    conversation_id UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

with:

```sql
    conversation_id UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- event_time: when the agent EMITTED the event (set by the bridge at enqueue; the
    --   truthful "when did this happen", since a queued/retried/reposted send may insert
    --   much later). received_at: when the APP accepted the POST. Both nullable so existing
    --   rows and non-bridge callers keep working; post_message defaults event_time to now().
    --   (Keep this comment semicolon-free: _SCHEMA is split on the semicolon.)
    event_time    TIMESTAMPTZ,
    received_at   TIMESTAMPTZ
);
```

Because `messages_archive` is `CREATE TABLE ... (LIKE messages INCLUDING ALL)` immediately
after, it inherits both columns automatically on a fresh DB — no separate archive CREATE
edit is needed.

- [ ] **Step 3b: Add the idempotent migrations (existing DBs)**

Append to the `_MIGRATIONS` list (after the existing summary ALTERs, before the FTS indexes).
These are still required: an already-provisioned DB was created before the columns existed,
and its `messages_archive` was likewise materialized without them, so both need explicit
ALTERs plus a NULL-only backfill:

```python
    # Three-clock timestamps for already-provisioned DBs (fresh DBs get these from _SCHEMA
    # above; the archive there inherits via LIKE ... INCLUDING ALL, but an EXISTING archive
    # was materialized before the columns existed, so ALTER it explicitly too). Nullable +
    # NULL-only backfill so this is non-breaking and idempotent.
    "ALTER TABLE messages         ADD COLUMN IF NOT EXISTS event_time  TIMESTAMPTZ",
    "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS event_time  TIMESTAMPTZ",
    "ALTER TABLE messages         ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ",
    "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ",
    "UPDATE messages         SET event_time  = created_at WHERE event_time  IS NULL",
    "UPDATE messages         SET received_at = created_at WHERE received_at IS NULL",
    "UPDATE messages_archive SET event_time  = created_at WHERE event_time  IS NULL",
    "UPDATE messages_archive SET received_at = created_at WHERE received_at IS NULL",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd covasant-partyline && python -m pytest ../tests/test_schema_bootstrap.py -v`
Expected: PASS (both the `_SCHEMA` and `_MIGRATIONS` assertions).

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/db.py tests/test_schema_bootstrap.py
git commit -m "feat(db): add event_time/received_at three-clock migration"
```

---

### Task 2: post_message accepts event_time (DB layer)

**Files:**
- Modify: `covasant-partyline/app/db.py` (`_MSG_COLS` ~line 473; `post_message` ~line 489–567; `get_since` ~line 570–594; `get_recent` ~line 597–611)
- Test: `partyline-client`/app tests — add `tests/test_event_time.py` at repo root `tests/`

**Interfaces:**
- Consumes: nothing new.
- Produces: `db.post_message(..., summary=None, event_time=None)` — `event_time` is a `datetime | str | None`; when `None` the DB defaults it to `now()`. `received_at` is always `now()` at insert. Returned rows and `_MSG_COLS` include `event_time` and `received_at`. `get_since`/`get_recent` order returned rows by `event_time`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_time.py`:

```python
"""event_time flows through db.post_message and orders reads (uses the app db layer)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "covasant-partyline"))

from app import db


def test_msg_cols_include_three_clocks():
    assert "event_time" in db._MSG_COLS
    assert "received_at" in db._MSG_COLS


def test_post_message_signature_accepts_event_time():
    import inspect
    params = list(inspect.signature(db.post_message).parameters)
    assert params[-1] == "event_time", f"event_time must be the last param, got {params[-1]}"
    assert params[-2] == "summary"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_event_time.py -v`
Expected: FAIL (`event_time` not in `_MSG_COLS`; signature lacks `event_time`).

- [ ] **Step 3: Implement**

In `db.py`, extend `_MSG_COLS` (append the two columns):

```python
_MSG_COLS = (
    "id, agent_id, author, message_type, recipient, repo, branch, resource_path, "
    "issue_ref, summary, payload, metadata, mentions, reply_to_id, conversation_id::text, "
    "created_at, event_time, received_at"
)
```

Update `post_message` signature and INSERT. Change the signature line to end with:

```python
    summary: str | None = None,
    event_time=None,  # datetime | ISO-str | None; agent emit time (None -> now())
) -> dict:
```

In the INSERT, add `event_time`/`received_at` columns and values. Change the column list and VALUES so the two new columns are set with `COALESCE(%s::timestamptz, now())` for event_time and `now()` for received_at:

```python
        row = conn.execute(
            f"""INSERT INTO messages
                 (agent_id, author, message_type, recipient, repo, branch,
                  resource_path, issue_ref, summary, payload, metadata, mentions, reply_to_id,
                  conversation_id, event_time, received_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       COALESCE((SELECT conversation_id FROM messages WHERE id = %s),
                                gen_random_uuid()),
                       COALESCE(%s::timestamptz, now()), now())
               RETURNING {_MSG_COLS}""",
            (
                agent_id, author, message_type, recipient, repo, branch,
                resource_path, issue_ref, summary, payload, json.dumps(metadata),
                mentions, reply_to_id, reply_to_id, event_time,
            ),
        ).fetchone()
```

Add `event_time` to the NOTIFY payload dict (so SSE consumers see it), after `"created_at": str(row["created_at"]),`:

```python
                        "event_time": str(row["event_time"]),
```

Order reads by event_time. In `get_since`, wrap the id-window query so pagination stays id-safe but display is by event_time (see spec — inner id-window preserves cursor semantics, outer orders by emit time):

```python
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT * FROM (SELECT {cols} FROM messages WHERE {where} "
            f"ORDER BY id ASC LIMIT %s) w ORDER BY event_time ASC, id ASC",
            tuple(params),
        ).fetchall()
```

In `get_recent`, order by event_time then reverse:

```python
        rows = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY event_time DESC, id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return list(reversed(rows))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_event_time.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/db.py tests/test_event_time.py
git commit -m "feat(db): thread event_time through post_message and order reads by it"
```

---

### Task 3: REST + models accept event_time

**Files:**
- Modify: `covasant-partyline/app/models.py` (`PostMessage` ~line 68–87; `Message` ~line 89–96)
- Modify: `covasant-partyline/app/rest.py` (`post_message` handler ~line 112–141)
- Test: `tests/test_rest_api.py`

**Interfaces:**
- Consumes: `db.post_message(..., event_time=...)` from Task 2.
- Produces: `POST /api/messages` accepts optional `event_time` (ISO-8601 string); when omitted the DB defaults to `now()`. `Message` responses include `event_time` and `received_at`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_rest_api.py` (follow the existing fixture/pattern in that file for the test client):

```python
def test_post_message_accepts_event_time(client, monkeypatch):
    captured = {}

    def fake_post_message(*args, **kwargs):
        # event_time is the last positional arg (see db.post_message signature)
        captured["event_time"] = args[-1]
        return {
            "id": 1, "agent_id": "a", "message_type": "status", "recipient": None,
            "repo": None, "branch": None, "resource_path": None, "issue_ref": None,
            "summary": None, "payload": "hi", "metadata": {}, "mentions": [],
            "reply_to_id": None, "author": None, "conversation_id": None,
            "created_at": "2026-07-16T00:00:00Z", "event_time": "2026-07-16T00:00:00Z",
            "received_at": "2026-07-16T00:00:00Z",
        }

    monkeypatch.setattr("app.db.post_message", fake_post_message)
    r = client.post("/api/messages", json={
        "agent_id": "a", "message_type": "status", "payload": "hi",
        "event_time": "2026-07-16T00:00:00Z",
    })
    assert r.status_code == 200
    assert captured["event_time"] == "2026-07-16T00:00:00Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd covasant-partyline && python -m pytest ../tests/test_rest_api.py::test_post_message_accepts_event_time -v`
Expected: FAIL (PostMessage rejects unknown `event_time`, or it isn't forwarded).

- [ ] **Step 3: Implement**

In `models.py`, add to `PostMessage` (after `reply_to_id`):

```python
    event_time: datetime | None = Field(
        None, description="Agent emit time (ISO-8601). Defaults server-side to now() when omitted."
    )
```

Add to `Message` (after `created_at`):

```python
    received_at: datetime | None = None
```

(`event_time` is already inherited from `PostMessage`; `Message` extends it.)

In `rest.py` `post_message`, pass `event_time` as the final positional arg to `db.post_message`:

```python
    row = await run_in_threadpool(
        db.post_message,
        body.agent_id,
        body.message_type.value,
        body.recipient,
        body.repo,
        body.branch,
        body.resource_path,
        body.issue_ref,
        body.payload,
        body.metadata,
        body.mentions,
        body.reply_to_id,
        author,
        body.summary,
        body.event_time,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd covasant-partyline && python -m pytest ../tests/test_rest_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/models.py covasant-partyline/app/rest.py tests/test_rest_api.py
git commit -m "feat(rest): accept optional event_time on POST /api/messages"
```

---

### Task 4: Spool-path resolution + agent_id resolution (outbox.py)

**Files:**
- Create: `partyline-client/covasant_partyline/outbox.py`
- Test: `partyline-client/tests/test_outbox.py`

**Interfaces:**
- Produces:
  - `resolve_agent_id(cli_arg: str | None) -> str | None` — flag → `PARTYLINE_AGENT_ID` env → `.partyline/config.json` (`agentId`/`agent_id`) via `cli._load_config`.
  - `resolve_spool_path(agent_id: str, env_value: str | None, cwd: str) -> str` — env is `PARTYLINE_SPOOL_PATH`; directory → `<dir>/partyline_<agent_id>.yaml`; file path → verbatim; contains `{agent_id}` → substituted; unset → `<parent-of-cwd>/partyline_<agent_id>.yaml`.

- [ ] **Step 1: Write the failing test**

Create `partyline-client/tests/test_outbox.py`:

```python
import os
from covasant_partyline import outbox


def test_spool_path_default_is_cwd_parent(tmp_path):
    cwd = tmp_path / "repos" / "aura_3"
    cwd.mkdir(parents=True)
    p = outbox.resolve_spool_path("alan_aura_3", None, str(cwd))
    assert p == os.path.join(str(tmp_path / "repos"), "partyline_alan_aura_3.yaml")


def test_spool_path_directory_value(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    p = outbox.resolve_spool_path("alan_aura_3", str(d), os.getcwd())
    assert p == os.path.join(str(d), "partyline_alan_aura_3.yaml")


def test_spool_path_file_value_verbatim(tmp_path):
    f = str(tmp_path / "shared.yaml")
    p = outbox.resolve_spool_path("alan_aura_3", f, os.getcwd())
    assert p == f


def test_spool_path_agent_id_token(tmp_path):
    val = str(tmp_path / "partyline_{agent_id}.yaml")
    p = outbox.resolve_spool_path("alan_aura_3", val, os.getcwd())
    assert p == str(tmp_path / "partyline_alan_aura_3.yaml")


def test_resolve_agent_id_prefers_flag(monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert outbox.resolve_agent_id("from_flag") == "from_flag"


def test_resolve_agent_id_env_then_config(monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert outbox.resolve_agent_id(None) == "from_env"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -v`
Expected: FAIL (module `outbox` does not exist).

- [ ] **Step 3: Implement the resolvers**

Create `partyline-client/covasant_partyline/outbox.py`:

```python
"""In-process outbox: spool-path/agent-id resolution, ticket table, and YAML spool writer.

Pure, testable units with no MCP-server or asyncio coupling. The bridge owns an Outbox
instance; the background sender (sender.py) drains it.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

from . import cli

_TICKET_BOUND = 200  # keep the last N ticket statuses; evict oldest


def resolve_agent_id(cli_arg: str | None) -> str | None:
    """flag -> PARTYLINE_AGENT_ID -> .partyline/config.json (agentId/agent_id)."""
    if cli_arg:
        return cli_arg
    env = os.getenv("PARTYLINE_AGENT_ID")
    if env:
        return env
    cfg = cli._load_config()
    return cfg.get("agentId") or cfg.get("agent_id")


def resolve_spool_path(agent_id: str, env_value: str | None, cwd: str) -> str:
    """Resolve where undeliverable messages spill. See module/spec docs for the rules."""
    if not env_value:
        parent = os.path.dirname(os.path.abspath(cwd))
        return os.path.join(parent, f"partyline_{agent_id}.yaml")
    if "{agent_id}" in env_value:
        return env_value.replace("{agent_id}", agent_id)
    if os.path.isdir(env_value) or env_value.endswith((os.sep, "/")):
        return os.path.join(env_value.rstrip("/" + os.sep), f"partyline_{agent_id}.yaml")
    return env_value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/outbox.py partyline-client/tests/test_outbox.py
git commit -m "feat(bridge): spool-path and agent-id resolution"
```

---

### Task 5: Ticket table (outbox.py)

**Files:**
- Modify: `partyline-client/covasant_partyline/outbox.py`
- Test: `partyline-client/tests/test_outbox.py`

**Interfaces:**
- Produces: `class TicketTable` with:
  - `new(verb: str) -> str` — mint a uuid ticket, record `pending`, return the ticket.
  - `mark_sent(ticket: str, message_id: int | None) -> None`
  - `mark_failed(ticket: str, error: str, spooled_to: str) -> None`
  - `get(ticket: str) -> dict` — returns `{status: "pending"|"sent"|"failed"|"unknown", ...}`; `unknown` for evicted/missing.
  - Thread-safe; bounded to `_TICKET_BOUND` (evict oldest).

- [ ] **Step 1: Write the failing test**

Add to `partyline-client/tests/test_outbox.py`:

```python
from covasant_partyline.outbox import TicketTable


def test_ticket_lifecycle_pending_to_sent():
    t = TicketTable()
    tk = t.new("post_message")
    assert t.get(tk)["status"] == "pending"
    t.mark_sent(tk, 42)
    got = t.get(tk)
    assert got["status"] == "sent" and got["message_id"] == 42


def test_ticket_failed_records_spool():
    t = TicketTable()
    tk = t.new("reply")
    t.mark_failed(tk, "connect timeout", "/tmp/partyline_a.yaml")
    got = t.get(tk)
    assert got["status"] == "failed"
    assert got["spooled_to"] == "/tmp/partyline_a.yaml"
    assert got["error"] == "connect timeout"


def test_unknown_ticket():
    assert TicketTable().get("nope")["status"] == "unknown"


def test_ticket_table_is_bounded():
    from covasant_partyline import outbox
    t = TicketTable()
    tickets = [t.new("post_message") for _ in range(outbox._TICKET_BOUND + 10)]
    assert t.get(tickets[0])["status"] == "unknown"    # oldest evicted
    assert t.get(tickets[-1])["status"] == "pending"   # newest retained
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -k ticket -v`
Expected: FAIL (`TicketTable` undefined).

- [ ] **Step 3: Implement**

Append to `outbox.py`:

```python
class TicketTable:
    """Bounded, thread-safe map of ticket -> delivery status (in-memory; not persisted)."""

    def __init__(self) -> None:
        self._d: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def new(self, verb: str) -> str:
        import uuid
        ticket = str(uuid.uuid4())
        with self._lock:
            self._d[ticket] = {"status": "pending", "verb": verb}
            while len(self._d) > _TICKET_BOUND:
                self._d.popitem(last=False)  # evict oldest
        return ticket

    def mark_sent(self, ticket: str, message_id: int | None) -> None:
        with self._lock:
            if ticket in self._d:
                self._d[ticket].update(status="sent", message_id=message_id)

    def mark_failed(self, ticket: str, error: str, spooled_to: str) -> None:
        with self._lock:
            if ticket in self._d:
                self._d[ticket].update(status="failed", error=error, spooled_to=spooled_to)

    def get(self, ticket: str) -> dict:
        with self._lock:
            entry = self._d.get(ticket)
            return dict(entry) if entry else {"status": "unknown"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/outbox.py partyline-client/tests/test_outbox.py
git commit -m "feat(bridge): bounded ticket table for send status"
```

---

### Task 6: YAML spool writer (outbox.py)

**Files:**
- Modify: `partyline-client/covasant_partyline/outbox.py`
- Test: `partyline-client/tests/test_outbox.py`

**Interfaces:**
- Produces: `spool_message(path: str, record: dict) -> bool` — append one YAML document (a single-element list item) to `path`, creating parent dirs; best-effort advisory lock via a `.lock` sidecar so concurrent agents sharing a file don't interleave; returns `True` on success, `False` on any failure (never raises). Stdlib-only YAML emission (no PyYAML dependency) — emit a minimal, safe subset.

- [ ] **Step 1: Write the failing test**

Add to `partyline-client/tests/test_outbox.py`:

```python
from covasant_partyline.outbox import spool_message


def test_spool_writes_appendable_yaml(tmp_path):
    p = str(tmp_path / "spool.yaml")
    rec1 = {"ticket": "t1", "agent_id": "a", "verb": "post_message",
            "message": {"message_type": "status", "summary": "hi", "payload": "line1\nline2"}}
    rec2 = {"ticket": "t2", "agent_id": "a", "verb": "reply",
            "message": {"message_type": "ack", "summary": None, "payload": "ok"}}
    assert spool_message(p, rec1) is True
    assert spool_message(p, rec2) is True
    text = open(p, encoding="utf-8").read()
    # Two list items appended
    assert text.count("- ticket:") == 2
    assert "t1" in text and "t2" in text
    # Multiline payloads must not corrupt the doc (round-trips through a YAML loader if present)
    try:
        import yaml
        docs = yaml.safe_load(text)
        assert len(docs) == 2
        assert docs[0]["message"]["payload"] == "line1\nline2"
    except ImportError:
        pass  # PyYAML not required at runtime; structural check above suffices


def test_spool_never_raises_on_bad_path():
    # A directory path that can't be written must return False, not raise.
    assert spool_message("/nonexistent-root-xyz/\x00bad", {"ticket": "t"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -k spool -v`
Expected: FAIL (`spool_message` undefined).

- [ ] **Step 3: Implement**

Append to `outbox.py`:

```python
def _yaml_scalar(v) -> str:
    """Emit a YAML-safe scalar for the limited types spool records contain."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Always double-quote strings and escape backslashes/quotes/newlines -> a single safe line.
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{s}"'


def _yaml_doc(record: dict) -> str:
    """Render one record as a top-level list item ('- key: val' with nested 'message')."""
    lines = []
    first = True
    for k, v in record.items():
        prefix = "- " if first else "  "
        first = False
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            for sk, sv in v.items():
                lines.append(f"    {sk}: {_yaml_scalar(sv)}")
        else:
            lines.append(f"{prefix}{k}: {_yaml_scalar(v)}")
    return "\n".join(lines) + "\n"


def spool_message(path: str, record: dict) -> bool:
    """Append one YAML document to `path`. Best-effort lock; never raises."""
    import time
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        lock = path + ".lock"
        # crude cross-platform advisory lock: create-exclusive with brief spin
        acquired = False
        for _ in range(50):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.02)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(_yaml_doc(record))
            return True
        finally:
            if acquired:
                try:
                    os.remove(lock)
                except OSError:
                    pass
    except Exception:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_outbox.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/outbox.py partyline-client/tests/test_outbox.py
git commit -m "feat(bridge): stdlib YAML spool writer (durable fallback)"
```

---

### Task 7: REST sender + background drain worker (sender.py)

**Files:**
- Create: `partyline-client/covasant_partyline/sender.py`
- Test: `partyline-client/tests/test_sender.py`

**Interfaces:**
- Consumes: `outbox.TicketTable`, `outbox.spool_message`, `db-sdk Config` (for the bearer token, resolved fresh per attempt).
- Produces:
  - `VERB_ENDPOINTS: dict[str, tuple[str, callable]]` — maps verb → (REST path, arg-to-body builder).
  - `send_once(base_url, token, verb, args) -> dict` — one blocking `urllib` POST; returns the parsed JSON; raises on HTTP/network error.
  - `class BackgroundSender` with `enqueue(verb, args, event_time) -> str` (returns ticket), `async run()` (drain loop), `async drain_and_stop(grace=5.0)`, and attribute `spill_flag: dict` (`{"pending": int, "path": str|None}`) for the bridge's warning prefix.
  - Backoff 1s→15s, per-message give-up ~120s.

- [ ] **Step 1: Write the failing test**

Create `partyline-client/tests/test_sender.py`:

```python
import asyncio
from covasant_partyline import sender, outbox


def test_verb_body_builders_shape():
    # post_message body carries event_time; reply maps to /api/messages too
    path, build = sender.VERB_ENDPOINTS["post_message"]
    assert path == "/api/messages"
    body = build({"agent_id": "a", "message_type": "status", "payload": "hi"}, "2026-07-16T00:00:00Z")
    assert body["agent_id"] == "a" and body["event_time"] == "2026-07-16T00:00:00Z"

    path, build = sender.VERB_ENDPOINTS["ack_checkpoint"]
    assert path == "/api/checkpoints"
    body = build({"agent_id": "a", "last_read_message_id": 5}, "2026-07-16T00:00:00Z")
    assert body["last_read_message_id"] == 5


def test_sender_marks_sent_on_success(monkeypatch):
    tickets = outbox.TicketTable()

    def fake_send_once(base_url, token_fn, verb, args, event_time):
        return {"id": 99}

    monkeypatch.setattr(sender, "send_once", fake_send_once)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        await bs.drain_and_stop(grace=2.0)
        task.cancel()
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "sent"
    assert tickets.get(tk)["message_id"] == 99


def test_sender_spills_after_giveup(monkeypatch):
    tickets = outbox.TicketTable()
    spills = []

    def always_fail(*a, **k):
        raise RuntimeError("app down")

    monkeypatch.setattr(sender, "send_once", always_fail)
    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    # shrink timings so the test is fast
    monkeypatch.setattr(sender, "_GIVEUP_SECS", 0.3)
    monkeypatch.setattr(sender, "_BACKOFF_START", 0.05)
    monkeypatch.setattr(sender, "_BACKOFF_CAP", 0.1)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        await asyncio.sleep(1.0)
        task.cancel()
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed"
    assert spills, "message must be spooled after give-up"
    assert bs.spill_flag["pending"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_sender.py -v`
Expected: FAIL (module `sender` missing).

- [ ] **Step 3: Implement**

Create `partyline-client/covasant_partyline/sender.py`:

```python
"""REST sender + background drain worker for the bridge's fire-and-forget outbox.

Sends over plain urllib POST (like channel.py.post_reply), NOT the proxied MCP session —
so fire-and-forget is decoupled from the flaky proxy and mints a fresh token per attempt
(token expiry becomes a non-event). Failed messages spill to the spool file after a bounded
give-up window; nothing is ever silently dropped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

from . import outbox

log = logging.getLogger("partyline.sender")

_BACKOFF_START = 1.0
_BACKOFF_CAP = 15.0
_GIVEUP_SECS = 120.0
_POST_TIMEOUT = 30.0


def _msg_body(args: dict, event_time) -> dict:
    body = {
        "agent_id": args.get("agent_id"),
        "message_type": args.get("message_type", "status"),
        "recipient": args.get("recipient"),
        "repo": args.get("repo"),
        "branch": args.get("branch"),
        "resource_path": args.get("resource_path"),
        "issue_ref": args.get("issue_ref"),
        "summary": args.get("summary"),
        "payload": args.get("payload"),
        "metadata": args.get("metadata") or {},
        "mentions": args.get("mentions") or [],
        "reply_to_id": args.get("reply_to_id"),
        "event_time": event_time,
    }
    return body


# verb -> (path, body builder(args, event_time))
VERB_ENDPOINTS = {
    "post_message": ("/api/messages", _msg_body),
    "reply": ("/api/messages", _msg_body),
    "ack_checkpoint": ("/api/checkpoints", lambda a, et: {
        "agent_id": a.get("agent_id"), "last_read_message_id": a.get("last_read_message_id"),
    }),
    "register_agent": ("/api/agents", lambda a, et: {
        "agent_id": a.get("agent_id"), "owner": a.get("owner"), "repo": a.get("repo"),
        "branch": a.get("branch"), "display_name": a.get("display_name"),
    }),
    "release_resource": ("/api/claims/release", lambda a, et: {
        "agent_id": a.get("agent_id"), "repo": a.get("repo"),
        "resource_path": a.get("resource_path"), "branch": a.get("branch"),
    }),
}


def send_once(base_url: str, token: str | None, verb: str, args: dict, event_time) -> dict:
    """One blocking POST for `verb`. Raises on HTTP/network error."""
    path, build = VERB_ENDPOINTS[verb]
    body = build(args, event_time)
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_POST_TIMEOUT) as resp:
        return json.loads(resp.read() or "null")


class BackgroundSender:
    """Drains an outbox queue, delivering via send_once with backoff -> spool on give-up."""

    def __init__(self, base_url, token_fn, tickets: "outbox.TicketTable", spool_path: str,
                 agent_id: str = "?") -> None:
        self._base = base_url
        self._token_fn = token_fn          # callable -> fresh bearer per attempt
        self._tickets = tickets
        self._spool_path = spool_path
        self._agent_id = agent_id
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self.spill_flag = {"pending": 0, "path": None}

    def enqueue(self, verb: str, args: dict, event_time) -> str:
        ticket = self._tickets.new(verb)
        self._q.put_nowait({
            "ticket": ticket, "verb": verb, "args": args,
            "event_time": event_time, "enqueued_at": time.time(),
        })
        return ticket

    async def _deliver(self, item: dict) -> None:
        t0 = time.monotonic()
        backoff = _BACKOFF_START
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            try:
                token = self._token_fn()
                result = await asyncio.to_thread(
                    send_once, self._base, token, item["verb"], item["args"], item["event_time"]
                )
                mid = result.get("id") if isinstance(result, dict) else None
                self._tickets.mark_sent(item["ticket"], mid)
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if time.monotonic() - t0 >= _GIVEUP_SECS:
                    self._spill(item, attempts, last_error)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)

    def _spill(self, item: dict, attempts: int, error: str) -> None:
        import datetime as dt
        record = {
            "ticket": item["ticket"], "agent_id": self._agent_id, "verb": item["verb"],
            "event_time": str(item["event_time"]),
            "spooled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempts": attempts, "last_error": error,
            "message": item["args"],
        }
        ok = outbox.spool_message(self._spool_path, record)
        self._tickets.mark_failed(item["ticket"], error, self._spool_path if ok else "(spill failed)")
        if ok:
            self.spill_flag["pending"] += 1
            self.spill_flag["path"] = self._spool_path
            log.error("spooled undeliverable %s to %s after %s attempts (%s)",
                      item["verb"], self._spool_path, attempts, error)
        else:
            log.error("SPILL FAILED for %s (%s) — message lost", item["verb"], error)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self._deliver(item)

    async def drain_and_stop(self, grace: float = 5.0) -> None:
        """Flush remaining queued items within `grace`, then spill whatever is left."""
        deadline = time.monotonic() + grace
        while not self._q.empty() and time.monotonic() < deadline:
            item = self._q.get_nowait()
            try:
                token = self._token_fn()
                result = await asyncio.to_thread(
                    send_once, self._base, token, item["verb"], item["args"], item["event_time"]
                )
                self._tickets.mark_sent(item["ticket"], result.get("id") if isinstance(result, dict) else None)
            except Exception as exc:
                self._spill(item, 1, f"{type(exc).__name__}: {exc}")
        while not self._q.empty():  # grace expired: spill the rest
            item = self._q.get_nowait()
            self._spill(item, 0, "bridge shutdown grace expired")
        self._stop.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_sender.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/sender.py partyline-client/tests/test_sender.py
git commit -m "feat(bridge): REST background sender with backoff and spool-on-giveup"
```

---

### Task 8: Metrics + footprint report (metrics.py)

**Files:**
- Create: `partyline-client/covasant_partyline/metrics.py`
- Test: `partyline-client/tests/test_metrics.py`

**Interfaces:**
- Produces:
  - `est_tokens(text: str) -> int` — `tiktoken` if importable, else `ceil(len/4)`.
  - `footprint(tools: list, instructions: str | None) -> dict` — `{total_bytes, est_total_tokens, instructions_tokens, per_tool: [{name, bytes, est_tokens}]}`.
  - `metrics_enabled() -> bool` — `PARTYLINE_METRICS` truthy.
  - `log_call(tool, in_bytes, out_content_bytes, out_structured_bytes, duration_ms, outcome)` — emit one JSONL line on the `partyline.metrics` logger, only when enabled.

- [ ] **Step 1: Write the failing test**

Create `partyline-client/tests/test_metrics.py`:

```python
import json
import logging
from covasant_partyline import metrics


def test_est_tokens_fallback_is_chars_over_four():
    # 40 chars -> 10 tokens under the fallback (tiktoken may give a different but positive value)
    n = metrics.est_tokens("x" * 40)
    assert n >= 1


class _Tool:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.inputSchema = schema


def test_footprint_has_per_tool_breakdown():
    tools = [
        _Tool("post_message", "post a message " * 20, {"type": "object", "properties": {}}),
        _Tool("get_unread", "read unread " * 10, {"type": "object", "properties": {}}),
    ]
    fp = metrics.footprint(tools, "INSTRUCTIONS " * 100)
    assert fp["total_bytes"] > 0
    assert fp["est_total_tokens"] > 0
    names = {t["name"] for t in fp["per_tool"]}
    assert names == {"post_message", "get_unread"}
    assert all(t["est_tokens"] > 0 for t in fp["per_tool"])


def test_log_call_emits_only_when_enabled(monkeypatch, caplog):
    monkeypatch.delenv("PARTYLINE_METRICS", raising=False)
    with caplog.at_level(logging.INFO, logger="partyline.metrics"):
        metrics.log_call("get_unread", 10, 200, 0, 5.0, "ok")
    assert not caplog.records  # disabled -> silent

    monkeypatch.setenv("PARTYLINE_METRICS", "1")
    with caplog.at_level(logging.INFO, logger="partyline.metrics"):
        metrics.log_call("get_unread", 10, 200, 0, 5.0, "ok")
    assert caplog.records
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["tool"] == "get_unread" and payload["outcome"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_metrics.py -v`
Expected: FAIL (module `metrics` missing).

- [ ] **Step 3: Implement**

Create `partyline-client/covasant_partyline/metrics.py`:

```python
"""Token/byte cost diagnostics for the bridge.

The bridge sees BYTES, not tokens (it never calls a model), so token counts are ESTIMATES
(always prefixed est_). The startup footprint report — the always-on schema+instructions
tax paid every turn — is logged unconditionally; per-call metrics are gated by
PARTYLINE_METRICS=1. See docs/superpowers/specs/2026-07-16-*.
"""
from __future__ import annotations

import json
import logging
import math
import os

log = logging.getLogger("partyline.metrics")

try:
    import tiktoken  # optional
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def est_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    return math.ceil(len(text) / 4)


def metrics_enabled() -> bool:
    return os.getenv("PARTYLINE_METRICS", "").strip().lower() in ("1", "true", "yes", "on")


def _tool_text(tool) -> str:
    name = getattr(tool, "name", "") or ""
    desc = getattr(tool, "description", "") or ""
    schema = getattr(tool, "inputSchema", None)
    schema_s = json.dumps(schema, default=str) if schema else ""
    return f"{name}\n{desc}\n{schema_s}"


def footprint(tools: list, instructions: str | None) -> dict:
    per_tool = []
    total = 0
    for t in tools:
        text = _tool_text(t)
        b = len(text.encode("utf-8"))
        per_tool.append({"name": getattr(t, "name", "?"), "bytes": b, "est_tokens": est_tokens(text)})
        total += b
    instr = instructions or ""
    instr_b = len(instr.encode("utf-8"))
    return {
        "total_bytes": total + instr_b,
        "est_total_tokens": est_tokens(instr) + sum(t["est_tokens"] for t in per_tool),
        "instructions_bytes": instr_b,
        "instructions_tokens": est_tokens(instr),
        "per_tool": sorted(per_tool, key=lambda x: x["est_tokens"], reverse=True),
    }


def log_footprint(tools: list, instructions: str | None) -> dict:
    """Always logged (not gated): the per-turn, per-agent floor."""
    fp = footprint(tools, instructions)
    log.info("partyline_footprint %s", json.dumps(fp))
    return fp


def est_tokens_from_bytes(n: int) -> int:
    """Estimate tokens from a byte count without materializing the text (chars/4 proxy)."""
    return math.ceil(n / 4) if n > 0 else 0


def log_call(tool: str, in_bytes: int, out_content_bytes: int, out_structured_bytes: int,
             duration_ms: float, outcome: str) -> None:
    if not metrics_enabled():
        return
    log.info(json.dumps({
        "tool": tool, "in_bytes": in_bytes,
        "out_content_bytes": out_content_bytes, "out_structured_bytes": out_structured_bytes,
        "est_tokens": est_tokens_from_bytes(out_content_bytes + out_structured_bytes),
        "duration_ms": round(duration_ms, 1), "outcome": outcome,
    }))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_metrics.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/metrics.py partyline-client/tests/test_metrics.py
git commit -m "feat(bridge): token/byte cost diagnostics + startup footprint report"
```

---

### Task 9: Fast-fail sync timeout tier (bridge.py)

**Files:**
- Modify: `partyline-client/covasant_partyline/bridge.py` (constants ~line 95–123; `_Remote.call`/`_call_bounded` ~line 387–482)
- Test: `partyline-client/tests/test_bridge.py`

**Interfaces:**
- Consumes: existing `_Remote`.
- Produces: `_Remote.call(op, label, sync=False)` — when `sync=True`, uses `_SYNC_CALL_TIMEOUT_SECS` (~8s) and `_SYNC_LOCK_ACQUIRE_TIMEOUT_SECS` (~10s) instead of the long ceilings. Reads/claim pass `sync=True`.

- [ ] **Step 1: Write the failing test**

Add to `partyline-client/tests/test_bridge.py`:

```python
def test_sync_call_uses_short_deadline(monkeypatch):
    # A sync=True call must abandon a wedged op at the SHORT deadline, not the 120s one.
    monkeypatch.setattr(bridge, "_SYNC_CALL_TIMEOUT_SECS", 0.2)

    async def body():
        r = _remote()
        sentinel = object()

        async def fake_connect(tag=None):
            r._session = sentinel
            return sentinel

        r._connect = fake_connect

        async def wedged(session):
            await asyncio.sleep(100)

        import time as _t
        t0 = _t.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await r.call(wedged, "wedged-sync", sync=True)
        assert _t.monotonic() - t0 < 3, "sync call must fail fast at the short deadline"

    asyncio.run(body())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_bridge.py::test_sync_call_uses_short_deadline -v`
Expected: FAIL (`call` has no `sync` param / no short constant).

- [ ] **Step 3: Implement**

In `bridge.py`, add constants near the other timeouts (~line 107):

```python
# Fast-fail ceilings for the SYNCHRONOUS path (reads + claim_resource). A stale-token or
# cold-app read fails in seconds so the agent retries cheaply, instead of riding the long
# fire-and-forget-era 120s deadline. Sends no longer use this path at all.
_SYNC_CALL_TIMEOUT_SECS = 8.0
_SYNC_LOCK_ACQUIRE_TIMEOUT_SECS = 10.0
```

Change `call` to accept `sync` and thread the chosen ceilings through:

```python
    async def call(self, op, label: str = "?", sync: bool = False):
        tag = f"{label}#{next(_call_seq)}"
        lock_timeout = _SYNC_LOCK_ACQUIRE_TIMEOUT_SECS if sync else _LOCK_ACQUIRE_TIMEOUT_SECS
        call_timeout = _SYNC_CALL_TIMEOUT_SECS if sync else _CALL_TIMEOUT_SECS
        log.debug("[%s] acquiring lock (sync=%s) ...", tag, sync)
        t_wait = time.monotonic()
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            log.error("[%s] bridge busy >%.0fs — failing fast", tag, lock_timeout)
            raise RuntimeError(
                f"partyline bridge busy for >{lock_timeout:.0f}s; the remote app "
                "may be restarting — please retry."
            )
        lock_wait = time.monotonic() - t_wait
        if lock_wait > _LOCK_WARN_SECS:
            log.warning("[%s] lock acquired after %.2fs (contention)", tag, lock_wait)
        try:
            return await self._call_bounded(op, tag, call_timeout)
        finally:
            self._lock.release()
```

Change `_call_bounded` to take the timeout:

```python
    async def _call_bounded(self, op, tag: str, call_timeout: float = _CALL_TIMEOUT_SECS):
```

and replace the `asyncio.wait_for(asyncio.shield(task), timeout=_CALL_TIMEOUT_SECS)` line with `timeout=call_timeout`, and the log line's `_CALL_TIMEOUT_SECS` with `call_timeout`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_bridge.py -v`
Expected: PASS (all existing tests still pass — default `sync=False` preserves behavior).

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/bridge.py partyline-client/tests/test_bridge.py
git commit -m "feat(bridge): fast-fail timeout tier for synchronous reads/claim"
```

---

### Task 10: Local send intercept + check_send tool (bridge.py)

**Files:**
- Modify: `partyline-client/covasant_partyline/bridge.py` (`_serve` `call_tool`/`list_tools` ~line 514–541; add tool defs + a module-level set of send verbs)
- Test: `partyline-client/tests/test_bridge_dispatch.py`

**Interfaces:**
- Consumes: `sender.BackgroundSender`, `outbox.TicketTable`, Tasks 7–8.
- Produces:
  - `_SEND_VERBS = {"post_message", "reply", "ack_checkpoint", "register_agent", "release_resource"}`.
  - `_SYNC_VERBS = {"get_unread", "get_since", "get_conversation", "search_messages", "list_agents", "list_claims", "claim_resource"}`.
  - `dispatch_kind(name) -> "send"|"sync"|"proxy"` — pure classifier.
  - `_CHECK_SEND_TOOL` MCP tool + local handler returning the ticket status.
  - `call_tool` routes: send verbs → `sender.enqueue` → `{queued, ticket}`; `check_send` → local ticket lookup; sync verbs → `remote.call(..., sync=True)`; everything else → proxied as today.

- [ ] **Step 1: Write the failing test**

Create `partyline-client/tests/test_bridge_dispatch.py`:

```python
from covasant_partyline import bridge


def test_dispatch_kind_classifies_verbs():
    assert bridge.dispatch_kind("post_message") == "send"
    assert bridge.dispatch_kind("reply") == "send"
    assert bridge.dispatch_kind("ack_checkpoint") == "send"
    assert bridge.dispatch_kind("get_unread") == "sync"
    assert bridge.dispatch_kind("claim_resource") == "sync"
    assert bridge.dispatch_kind("check_send") == "local"
    assert bridge.dispatch_kind("partyline_bridge_info") == "local"
    assert bridge.dispatch_kind("something_else") == "proxy"


def test_send_verbs_and_sync_verbs_disjoint():
    assert bridge._SEND_VERBS.isdisjoint(bridge._SYNC_VERBS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_bridge_dispatch.py -v`
Expected: FAIL (`dispatch_kind`/`_SEND_VERBS` undefined).

- [ ] **Step 3: Implement**

In `bridge.py`, add near the top (after `_BRIDGE_INFO_TOOL`):

```python
_SEND_VERBS = {"post_message", "reply", "ack_checkpoint", "register_agent", "release_resource"}
_SYNC_VERBS = {"get_unread", "get_since", "get_conversation", "search_messages",
               "list_agents", "list_claims", "claim_resource"}
_LOCAL_VERBS = {_BRIDGE_INFO_TOOL.name, "check_send"}


def dispatch_kind(name: str) -> str:
    if name in _LOCAL_VERBS:
        return "local"
    if name in _SEND_VERBS:
        return "send"
    if name in _SYNC_VERBS:
        return "sync"
    return "proxy"


_CHECK_SEND_TOOL = mcp_types.Tool(
    name="check_send",
    description=(
        "Check the delivery status of a fire-and-forget send by its ticket (the uuid "
        "returned by post_message/reply/ack_checkpoint/register_agent/release_resource). "
        "Returns pending | sent {message_id} | failed {spooled_to} | unknown. Handled "
        "locally by the bridge — instant, works even when the app is unreachable."
    ),
    inputSchema={
        "type": "object",
        "properties": {"ticket": {"type": "string"}},
        "required": ["ticket"], "additionalProperties": False,
    },
)
```

`_serve` gains a sender + tickets (wired fully in Task 11); here update `call_tool` and `list_tools`. Replace the `call_tool` body with dispatch routing:

```python
    @proxy.call_tool()
    async def call_tool(name: str, arguments: dict):
        kind = dispatch_kind(name)
        if name == _BRIDGE_INFO_TOOL.name:
            info = _bridge_info(remote, sender=sender_ref["s"])
            return [mcp_types.TextContent(type="text", text=json.dumps(info, indent=2, default=str))], info
        if name == "check_send":
            status = tickets.get(arguments.get("ticket", ""))
            return [mcp_types.TextContent(type="text", text=json.dumps(status, default=str))], status
        if kind == "send":
            import datetime as dt
            event_time = dt.datetime.now(dt.timezone.utc).isoformat()
            ticket = sender_ref["s"].enqueue(name, dict(arguments or {}), event_time)
            out = {"queued": True, "ticket": ticket}
            prefix = _drain_spill_prefix(sender_ref["s"])
            text = (prefix + "\n" if prefix else "") + json.dumps(out)
            log.info("-> call_tool(%s) [queued ticket=%s]", name, ticket)
            return [mcp_types.TextContent(type="text", text=text)], out
        # sync reads/claim: fast-fail; everything else: normal proxy
        log.info("-> call_tool(%s) args=%s", name, list(arguments.keys()) if arguments else [])
        result = await _proxied(
            f"call_tool({name})", lambda s: s.call_tool(name, arguments), f"call_tool/{name}",
            sync=(kind == "sync"),
        )
        log.info("<- call_tool(%s) OK", name)
        return result.content, getattr(result, "structuredContent", None)
```

Add the spill-prefix helper near `_serve`:

```python
    def _drain_spill_prefix(sender) -> str:
        """One-line warning prepended to the next tool result when sends have spooled."""
        n = sender.spill_flag.get("pending", 0)
        if not n:
            return ""
        path = sender.spill_flag.get("path")
        sender.spill_flag["pending"] = 0
        return f"⚠ {n} partyline message(s) spooled to {path} (app unreachable)"
```

Update `_proxied` to forward `sync`:

```python
    async def _proxied(desc: str, op, label: str, sync: bool = False):
        try:
            return await remote.call(op, label=label, sync=sync)
        except Exception as exc:
            log.error("%s FAILED (%s: %s)", desc, type(exc).__name__, exc, exc_info=True)
            raise
```

Advertise `check_send` in `list_tools` (add to the local tools):

```python
        return list(upstream) + [_BRIDGE_INFO_TOOL, _CHECK_SEND_TOOL]
```

(`sender_ref` and `tickets` are created in Task 11; for this task's unit test only `dispatch_kind`/`_SEND_VERBS`/`_SYNC_VERBS` are exercised, which are module-level and independent.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/test_bridge_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/bridge.py partyline-client/tests/test_bridge_dispatch.py
git commit -m "feat(bridge): local send intercept, check_send tool, dispatch split"
```

---

### Task 11: Wire sender lifecycle + non-blocking startup + agent_id (bridge.py)

**Files:**
- Modify: `partyline-client/covasant_partyline/bridge.py` (`_serve` ~line 485–563; `main` argparse ~line 566–571; `_bridge_info` ~line 145–173)
- Test: `partyline-client/tests/test_bridge_dispatch.py`

**Interfaces:**
- Consumes: `sender.BackgroundSender`, `outbox.TicketTable`, `outbox.resolve_agent_id`, `outbox.resolve_spool_path`, `metrics.log_footprint`.
- Produces: `_serve(cfg, mcp_url, base_url, agent_id, spool_path)` constructs the tickets + sender, runs the sender + a non-blocking startup connect as background tasks, and drains the sender on shutdown. `main` resolves `--agent-id`/env, derives `base_url` from the mcp_url (strip trailing `/mcp`), resolves the spool path, and logs the footprint after the background connect captures the tool list. `_bridge_info` gains an optional `sender` kwarg reporting `spool_path` + queued/spilled counts.

- [ ] **Step 1: Write the failing test**

Add to `partyline-client/tests/test_bridge_dispatch.py`:

```python
def test_base_url_from_mcp_url():
    assert bridge._base_url_from_mcp("https://app.example.com/mcp") == "https://app.example.com"
    assert bridge._base_url_from_mcp("https://app.example.com/mcp/") == "https://app.example.com"


def test_bridge_info_reports_spool_when_sender_present():
    class FakeSender:
        spool_path = "/tmp/partyline_a.yaml"
        spill_flag = {"pending": 2, "path": "/tmp/partyline_a.yaml"}
    info = bridge._bridge_info(None, sender=FakeSender())
    assert info["spool_path"] == "/tmp/partyline_a.yaml"
    assert info["spooled_pending"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd partyline-client && python -m pytest tests/test_bridge_dispatch.py -k "base_url or spool" -v`
Expected: FAIL (`_base_url_from_mcp` undefined; `_bridge_info` has no `sender` kwarg).

- [ ] **Step 3: Implement**

Add the helper near `_resolve_mcp_url`:

```python
def _base_url_from_mcp(mcp_url: str) -> str:
    """The REST base is the app root: the mcp URL minus its trailing /mcp."""
    u = mcp_url.rstrip("/")
    return u[:-4] if u.endswith("/mcp") else u
```

Extend `_bridge_info` signature and body:

```python
def _bridge_info(remote: "_Remote | None" = None, sender=None) -> dict:
    ...
    if sender is not None:
        info["spool_path"] = getattr(sender, "spool_path", None) or getattr(sender, "_spool_path", None)
        info["spooled_pending"] = sender.spill_flag.get("pending", 0)
    return info
```

(Expose `spool_path` publicly on `BackgroundSender`: in `sender.py` set `self.spool_path = spool_path` alongside `self._spool_path`, or rename to the public attr. Use `self.spool_path = spool_path` and reference it internally.)

Rewrite `_serve` signature and wiring:

```python
async def _serve(cfg: Config, mcp_url: str, base_url: str, agent_id: str, spool_path: str) -> None:
    remote = _Remote(cfg, mcp_url)
    tickets = outbox_mod.TicketTable()

    def _token() -> str | None:
        headers = _auth_headers(cfg)
        auth = headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    sender = sender_mod.BackgroundSender(base_url, _token, tickets, spool_path, agent_id=agent_id)
    sender_ref = {"s": sender}
    ...
```

Add the imports at the top of `bridge.py`:

```python
from . import outbox as outbox_mod
from . import sender as sender_mod
from . import metrics as metrics_mod
```

Replace the eager `await remote.call(...)` startup block with a background task, and log the footprint once tools are known:

```python
    async def _startup_connect():
        try:
            tools = (await remote.call(lambda s: s.list_tools(), label="startup/list_tools", sync=True)).tools
            proxy.instructions = remote.instructions or proxy.instructions
            metrics_mod.log_footprint(list(tools) + [_BRIDGE_INFO_TOOL, _CHECK_SEND_TOOL], remote.instructions or INSTRUCTIONS_FALLBACK)
            log.info("Startup connect OK — upstream reachable")
        except Exception:
            log.warning("Startup connect failed — serving anyway; will connect on first call", exc_info=True)
```

(Define `INSTRUCTIONS_FALLBACK = None` or reuse the proxy's static instructions; `metrics_mod.log_footprint` tolerates `None`.)

In the serve body, start both background tasks and drain on exit:

```python
    opts = proxy.create_initialization_options()
    log.info("stdio proxy ready — serving MCP over stdin/stdout")
    try:
        async with stdio_server() as (r, w):
            bg = asyncio.create_task(sender.run(), name="partyline-sender")
            sc = asyncio.create_task(_startup_connect(), name="partyline-startup")
            try:
                await proxy.run(r, w, opts)
            finally:
                await sender.drain_and_stop(grace=5.0)
                bg.cancel()
                sc.cancel()
        log.info("stdio loop ended — shutting down")
    finally:
        await remote.close()
```

Update `main` to resolve agent_id / base_url / spool_path and pass them:

```python
    p.add_argument("--agent-id", help="Agent id on the line (else PARTYLINE_AGENT_ID / .partyline/config.json)")
    ...
    mcp_url = _resolve_mcp_url(cfg, args.url, args.app_name)
    base_url = _base_url_from_mcp(mcp_url)
    agent_id = outbox_mod.resolve_agent_id(args.agent_id) or "unknown_agent"
    spool_path = outbox_mod.resolve_spool_path(agent_id, os.getenv("PARTYLINE_SPOOL_PATH"), os.getcwd())
    log.info("agent_id=%s base_url=%s spool_path=%s", agent_id, base_url, spool_path)
    asyncio.run(_serve(cfg, mcp_url, base_url, agent_id, spool_path))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd partyline-client && python -m pytest tests/ -v`
Expected: PASS (all bridge, dispatch, outbox, sender, metrics tests green).

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/bridge.py partyline-client/covasant_partyline/sender.py partyline-client/tests/test_bridge_dispatch.py
git commit -m "feat(bridge): wire sender lifecycle, non-blocking startup, agent_id + spool config"
```

---

### Task 12: Update .mcp.json + docs for agent_id/spool config

**Files:**
- Modify: `.mcp.json` (the `partyline` server `args`/`env`)
- Modify: `README.md` and/or `CLAUDE.md` (document `PARTYLINE_SPOOL_PATH`, `PARTYLINE_METRICS`, `check_send`, `--agent-id`, and the client-side `/cost` + OTel pointer)
- Test: none (config/docs) — verified by the live run in Task 13.

- [ ] **Step 1: Add agent_id to the bridge invocation**

In `.mcp.json`, add `--agent-id` to the `partyline` server args (matching the channel server's `PARTYLINE_AGENT_ID`) so the default spool filename and spool records name the agent:

```json
    "partyline": {
      "command": "python",
      "args": ["-m", "covasant_partyline.bridge", "--app-name", "mcp-covasant-partyline", "--agent-id", "alan_partyline_1"],
      "env": { "DATABRICKS_CONFIG_PROFILE": "dev-eastus2" }
    }
```

- [ ] **Step 2: Document the new surface**

Add a short "Bridge reliability & diagnostics" subsection to `README.md`:
- `post_message`/`reply`/`ack_checkpoint`/`register_agent`/`release_resource` are fire-and-forget: they return `{queued, ticket}` instantly; the message is delivered by a background sender.
- `check_send(ticket)` → `pending`/`sent`/`failed`/`unknown`.
- `PARTYLINE_SPOOL_PATH` — where undeliverable messages spill (dir / file / `{agent_id}`; default `../partyline_<agent_id>.yaml`). Safety-net only; not auto-replayed.
- `PARTYLINE_METRICS=1` — emit per-call byte/est-token JSONL on `partyline.metrics`; the startup footprint line is always logged.
- The bridge sees bytes, not tokens — for true session token spend use Claude Code `/cost` and OTel (`CLAUDE_CODE_ENABLE_TELEMETRY=1`).

- [ ] **Step 3: Commit**

```bash
git add .mcp.json README.md
git commit -m "docs: document fire-and-forget bridge, check_send, spool + metrics config"
```

---

### Task 13: Deploy migration + live verification

**Files:**
- No code changes; runs the deploy + live checks per CLAUDE.md.

- [ ] **Step 1: Claim the shared migration surface on the live line**

Register + claim before touching deployed schema (use the existing installed bridge/CLI):

```bash
python -m covasant_partyline.cli register alan_partyline_1 --owner alan --repo partyline
python -m covasant_partyline.cli claim alan_partyline_1 partyline scripts/migrate_schema.py --ttl 900
```

If refused, coordinate with the holder instead of proceeding.

- [ ] **Step 2: Reinstall the client editable + run unit suite**

```bash
pip install -e ./partyline-client
python -m pytest partyline-client/tests -v && (cd covasant-partyline && python -m pytest ../tests -v)
```
Expected: all green.

- [ ] **Step 3: Deploy (provisions Lakebase, runs migrate_schema.py, deploys bundle)**

```bash
./deploy.sh dev-eastus2 dev-eastus2
```
Expected: `migrate_schema.py` logs the four new ALTERs applied (or skipped as no-ops if already present); bundle deploy succeeds.

- [ ] **Step 4: Verify the three-clock columns landed**

```bash
databricks bundle run covasant_partyline -p dev-eastus2 || true
python -m covasant_partyline.cli post alan_partyline_1 --type status \
  --summary "fire-and-forget verify" --payload "checking event_time + queued send"
```
Then confirm via a read that the posted message carries `event_time`/`received_at` and that the send returned a `ticket` (check the bridge from a Claude Code session: call `post_message` and confirm the instant `{queued, ticket}`, then `check_send(ticket)` → `sent {message_id}`).

- [ ] **Step 5: Verify fire-and-forget + fallback behavior**

- With the app reachable: a `post_message` from an agent returns instantly; `check_send` transitions `pending → sent`.
- Simulate unreachable (point the bridge at a bad `--app-name`/URL in a throwaway session): `post_message` still returns instantly with a ticket; after ~2 min `check_send` → `failed` and a `partyline_<agent_id>.yaml` appears in the CWD parent with the stranded message; the next tool result is prefixed with the spool warning.

- [ ] **Step 6: Clean up the live line + release the claim**

Archive/clean any test messages posted, then:

```bash
python -m covasant_partyline.cli release alan_partyline_1 partyline scripts/migrate_schema.py
```

- [ ] **Step 7: Commit any deploy-artifact/doc updates** (if the deploy changed tracked files; otherwise skip).

---

## Self-Review

Run after the plan is written. (Completed during authoring.)

**1. Spec coverage:**
- Send/read split → Tasks 9 (sync tier), 10 (intercept/classify). ✓
- GUID ticket + `check_send` → Tasks 5, 10. ✓
- REST background sender + backoff→spool → Task 7. ✓
- Config file fallback (`PARTYLINE_SPOOL_PATH`) → Tasks 4, 6, 12. ✓
- Warning-prefix failure surfacing → Task 10 (`_drain_spill_prefix`). ✓
- Three-clock timestamps + migration + ordering → Tasks 1–3. ✓
- Non-blocking startup + sender lifecycle + shutdown grace → Task 11. ✓
- agent_id resolution → Tasks 4, 11, 12. ✓
- Diagnostics (footprint always-on; per-call gated; est_ labeling; `/cost`+OTel pointer) → Tasks 8, 11, 12. ✓
- Live verification + shared-surface claim → Task 13. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows real content.

**3. Type consistency:** `db.post_message(..., summary, event_time)` order matches Tasks 2/3. `BackgroundSender.enqueue(verb, args, event_time) -> ticket` and `spill_flag` used consistently in Tasks 7/10/11. `dispatch_kind` returns `local|send|sync|proxy`, matching the `call_tool` routing. `spool_path` exposed publicly on the sender for `_bridge_info` (Task 11 note). `resolve_spool_path`/`resolve_agent_id` signatures identical across Tasks 4/11.
