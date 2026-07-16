"""Lakebase (autoscale Postgres) access: OAuth-authenticated connection pool, idempotent
schema bootstrap (PRD §10), and the data-access layer for the bus.

Auth follows the canonical Lakebase Autoscaling pattern: a psycopg ``ConnectionPool`` whose
connection class mints a fresh, short-lived OAuth database credential each time a physical
connection is opened. ``max_lifetime`` recycles connections before the ~1h token expiry, so
no background refresh thread is needed.

The connection uses the *app service principal* identity (``PGUSER``). Per-request OBO
(the calling user's identity) is captured separately at the HTTP layer for audit — see
auth.py — and stamped into ``messages.author``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger("partyline.db")

NOTIFY_CHANNEL = "partyline"

# The app's service principal has CREATE on the database (CAN_CONNECT_AND_CREATE) but not
# on the pre-owned `public` schema (Postgres 15+ default). So the bus owns its own schema;
# every connection sets search_path to it, and init_schema creates it first.
APP_SCHEMA = "partyline"

_pool: ConnectionPool | None = None
_pool_opened = False
_workspace_client = None
_ws_lock = threading.Lock()

# Cached Lakebase credential. Minting hits the Databricks control plane, so without a
# cache every physical connection open (pool fill, max_lifetime recycle, listener
# reconnect) would make a blocking, un-timed control-plane call — coupling DB
# availability to control-plane latency. The credential is a bearer token reusable until
# it expires (~1h, only used at Postgres login), so mint once and reuse until near expiry.
_token_lock = threading.Lock()
_cached_token: str | None = None
_cached_token_exp: float = 0.0  # monotonic-clock deadline; 0.0 = nothing cached
_TOKEN_SAFETY_MARGIN_SECS = 300.0  # refresh this long before the real expiry
_TOKEN_FALLBACK_TTL_SECS = 600.0  # used when expiration_time is missing/unparseable


def _ws():
    global _workspace_client
    with _ws_lock:
        if _workspace_client is None:
            from databricks.sdk import WorkspaceClient

            _workspace_client = WorkspaceClient()
        return _workspace_client


def _credential_ttl(expiration_time: str | None) -> float:
    """Seconds to cache a credential: (expiry - now) minus a safety margin.

    Falls back to a short TTL if the timestamp is absent or unparseable, so a bad value
    means "mint more often", never "reuse an expired token".
    """
    if not expiration_time:
        return _TOKEN_FALLBACK_TTL_SECS
    try:
        exp = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        secs = (exp - datetime.now(timezone.utc)).total_seconds() - _TOKEN_SAFETY_MARGIN_SECS
        return max(secs, 0.0)
    except (ValueError, TypeError):
        return _TOKEN_FALLBACK_TTL_SECS


def _mint_token() -> str:
    """Return a Lakebase-scoped OAuth credential for Postgres login, cached until expiry.

    Must be a database credential from ``postgres.generate_database_credential`` (a
    workspace-scoped OAuth token would be rejected at Postgres login). Cached so the
    control-plane mint stays off the per-connection hot path (see cache constants above).
    """
    global _cached_token, _cached_token_exp
    with _token_lock:
        if _cached_token and time.monotonic() < _cached_token_exp:
            return _cached_token
    # Mint outside the lock so a slow control-plane call can't block other threads' cache
    # hits; a brief window where two threads both mint is harmless (last write wins).
    log.debug("Minting Lakebase OAuth credential for endpoint=%s", settings.endpoint_name)
    cred = _ws().postgres.generate_database_credential(endpoint=settings.endpoint_name)
    # getattr, not attribute access: older databricks-sdk builds (the version pinned in
    # the deployed app) have no `expiration_time` on DatabaseCredential — a hard access
    # raises AttributeError, which previously broke every connection and crashed startup.
    ttl = _credential_ttl(getattr(cred, "expiration_time", None))
    with _token_lock:
        _cached_token = cred.token
        _cached_token_exp = time.monotonic() + ttl
    log.debug("Lakebase credential minted; cached for %.0fs", ttl)
    return cred.token


def _conninfo() -> str:
    # Re-read from env at call time — Databricks Apps may inject PG* variables after
    # the process starts (resource binding race), so the frozen Settings fields can be
    # None even though the env vars are available by the time open_pool() runs.
    import os

    pg_host = settings.pg_host or os.getenv("PGHOST", "")
    pg_user = settings.pg_user or os.getenv("PGUSER", "")
    pg_database = settings.pg_database or os.getenv("PGDATABASE", "databricks_postgres")
    pg_port = settings.pg_port or os.getenv("PGPORT", "5432")
    pg_sslmode = settings.pg_sslmode or os.getenv("PGSSLMODE", "require")
    # connect_timeout bounds a stalled TCP/TLS handshake (Lakebase scaling from zero,
    # control-plane slowness). TCP keepalives let the kernel surface a silently-dropped
    # socket as an error instead of a pooled connection that hangs on first use.
    return (
        f"dbname={pg_database} user={pg_user} host={pg_host} port={pg_port} "
        f"sslmode={pg_sslmode} connect_timeout=10 "
        "keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3"
    )


class OAuthConnection(psycopg.Connection):
    """psycopg connection that injects a fresh Lakebase token as the password."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
        kwargs["password"] = _mint_token()
        return super().connect(conninfo, **kwargs)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        kwargs: dict = {
            "conninfo": _conninfo(),
            "min_size": 1,
            "max_size": 10,
            "open": False,
            # search_path pins all statements to the app's own schema (see APP_SCHEMA).
            # statement_timeout caps any single query so a hung statement (lock wait,
            # Lakebase stall) can't pin a threadpool thread forever and starve the server.
            "kwargs": {
                "row_factory": dict_row,
                "options": f"-c search_path={APP_SCHEMA} -c statement_timeout=30000",
            },
        }
        if settings.local:
            # Local dev: use PGPASSWORD, standard connection class, no token rotation.
            import os

            kwargs["kwargs"]["password"] = os.environ.get("PGPASSWORD", "")
        else:
            # Deployed: OAuth token rotation via Lakebase credential minting.
            kwargs["connection_class"] = OAuthConnection
            kwargs["max_lifetime"] = 2700  # 45m: recycle before the ~1h token expiry
        _pool = ConnectionPool(**kwargs)
    # Self-heal: open the pool on first use if startup never did (e.g. PG* env injected
    # after the startup window — otherwise the pool stays constructed-but-closed and every
    # query fails forever until a manual restart). open() is idempotent on an already-open
    # pool; guard on db_configured so we don't burn connect_timeout against an empty host.
    global _pool_opened
    if not _pool_opened and settings.db_configured:
        try:
            _pool.open(wait=True, timeout=30.0)
            _pool_opened = True
            log.info("Connection pool opened")
        except Exception:
            # A pool that failed to open is closed, and psycopg cannot reopen a closed
            # pool. Discard it so the next call builds a fresh one (self-heal), and
            # re-raise — never return a dead pool that would then raise PoolClosed on use
            # (which is exactly what crashed startup once mint began failing).
            log.exception("Pool open failed; discarding pool for rebuild")
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
            _pool_opened = False
            raise
    return _pool


def open_pool() -> None:
    log.info("Opening connection pool (host=%s db=%s) ...", settings.pg_host, settings.pg_database)
    get_pool()  # constructs and opens (idempotent; see self-heal note in get_pool)
    log.info("Connection pool open")


def close_pool() -> None:
    global _pool, _pool_opened
    log.info("Closing connection pool")
    if _pool is not None:
        _pool.close()
    # Reset so a subsequent get_pool() builds a fresh pool — a psycopg pool cannot be
    # reopened once closed.
    _pool = None
    _pool_opened = False
    log.info("Connection pool closed")


# --------------------------------------------------------------------------- #
# Schema (FR9: idempotent self-provisioning) — mirrors PRD §10
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS partyline;

CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    owner          TEXT NOT NULL,
    repo           TEXT NOT NULL,
    branch         TEXT,
    display_name   TEXT,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id            BIGSERIAL PRIMARY KEY,
    agent_id      TEXT        NOT NULL,
    author        TEXT,
    message_type  TEXT        NOT NULL,
    recipient     TEXT,
    repo          TEXT,
    branch        TEXT,
    resource_path TEXT,
    issue_ref     TEXT,
    -- summary: a concise, agent-authored one-liner (the "what") — payload holds the full
    -- detail (the "why/how"). Readers get summary by default and fetch payload on demand,
    -- so the common case costs a fraction of the tokens. Null summary means fall back to
    -- payload. (Keep this comment free of semicolons: _SCHEMA is split on the semicolon.)
    summary       TEXT,
    payload       TEXT,
    metadata      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    mentions      TEXT[]      NOT NULL DEFAULT '{}',
    reply_to_id   BIGINT      REFERENCES messages(id),
    conversation_id UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_issue_ref ON messages (issue_ref);
-- Serves the "latest status per agent" lateral join in list_agents (dashboard/extension
-- agent grid): newest status row per agent_id without scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_messages_agent_status ON messages (agent_id, message_type, created_at DESC);

-- Retention archive: the retention job moves (not deletes) old messages here, in the
-- same transaction as the prune, so history survives OLTP pruning. Queryable from
-- Unity Catalog via the registered `partyline_pg` catalog. No FK: archived rows keep
-- their original reply_to_id values even when the referenced row is archived too.
-- (Interim for PRD §7.10 until the Lakehouse Sync API is available programmatically.)
CREATE TABLE IF NOT EXISTS messages_archive (LIKE messages INCLUDING ALL);

CREATE TABLE IF NOT EXISTS checkpoints (
    agent_id             TEXT PRIMARY KEY,
    last_read_message_id BIGINT NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claims (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      TEXT        NOT NULL REFERENCES agents(agent_id),
    repo          TEXT        NOT NULL,
    branch        TEXT,
    resource_path TEXT        NOT NULL,
    resource_type TEXT,
    claim_scope   TEXT        NOT NULL DEFAULT 'exclusive',
    reason        TEXT,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_key
    ON claims (repo, (COALESCE(branch, '')), resource_path);

"""

# Native Lakehouse Sync (wal2delta CDC -> UC Delta SCD2 history) requires REPLICA
# IDENTITY FULL so deletes/updates carry the full old row into the WAL. These ALTERs
# require table *ownership* — after an app rename/replace the tables may be owned by a
# prior identity (access via GRANTs), so run them best-effort: they only matter for the
# future wal2delta sync (PRD §14), never for correctness of the bus itself.
_REPLICA_IDENTITY = [
    "ALTER TABLE messages    REPLICA IDENTITY FULL",
    "ALTER TABLE checkpoints REPLICA IDENTITY FULL",
    "ALTER TABLE claims      REPLICA IDENTITY FULL",
    "ALTER TABLE agents      REPLICA IDENTITY FULL",
]

# Idempotent migrations for existing databases (ADD COLUMN IF NOT EXISTS, NULL-only
# backfill, CREATE INDEX IF NOT EXISTS). Fresh DBs get conversation_id from the CREATE
# above; these bring already-provisioned DBs up to date. See docs/adr/2026-07-11-*.
_MIGRATIONS = [
    "ALTER TABLE messages         ADD COLUMN IF NOT EXISTS conversation_id UUID",
    "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS conversation_id UUID",
    # Concise agent-authored summary; payload keeps the full detail. Nullable — existing
    # rows and callers that don't set it fall back to payload, so this is non-breaking.
    "ALTER TABLE messages         ADD COLUMN IF NOT EXISTS summary TEXT",
    "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS summary TEXT",
    # Backfill live threads: each reply-tree (root = no resolvable parent) gets one UUID,
    # propagated down reply_to_id chains. Fills NULLs only, so re-running is a no-op.
    """
    WITH RECURSIVE roots AS (
        SELECT id, id AS root_id FROM messages
         WHERE reply_to_id IS NULL OR reply_to_id NOT IN (SELECT id FROM messages)
        UNION ALL
        SELECT m.id, r.root_id FROM messages m JOIN roots r ON m.reply_to_id = r.id
    ),
    gen AS (SELECT DISTINCT root_id, gen_random_uuid() AS cid FROM roots)
    UPDATE messages m SET conversation_id = g.cid
      FROM roots r JOIN gen g ON g.root_id = r.root_id
     WHERE m.id = r.id AND m.conversation_id IS NULL
    """,
    # Archived rows (>retention window) are not re-threaded: give each its own id.
    "UPDATE messages_archive SET conversation_id = gen_random_uuid() WHERE conversation_id IS NULL",
    # Full-text search (FR: inspect history). websearch_to_tsquery queries these.
    "CREATE INDEX IF NOT EXISTS idx_messages_fts ON messages USING GIN (to_tsvector('english', coalesce(payload, '')))",
    "CREATE INDEX IF NOT EXISTS idx_archive_fts  ON messages_archive USING GIN (to_tsvector('english', coalesce(payload, '')))",
    # Thread lookup (get_conversation).
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_archive_conversation  ON messages_archive (conversation_id, id)",
]


def init_schema() -> None:
    """Idempotent bootstrap, statement by statement.

    Privilege errors are non-fatal: after an app rename/replace, objects may be owned
    by a prior identity (this SP has access via GRANTs). Everything here is CREATE IF
    NOT EXISTS or optional (REPLICA IDENTITY), so on an already-provisioned database a
    skipped statement is safe — log it and continue rather than crash the app.
    """
    statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
    statements += _MIGRATIONS
    statements += _REPLICA_IDENTITY
    with get_pool().connection() as conn:
        for stmt in statements:
            try:
                conn.execute(stmt)
                conn.commit()
            except (
                psycopg.errors.InsufficientPrivilege,
                # With statement_timeout set, a best-effort ALTER (e.g. REPLICA IDENTITY,
                # which needs ACCESS EXCLUSIVE) can be canceled or lose a lock race. These
                # are idempotent/optional, so skip-and-continue rather than crash startup.
                psycopg.errors.QueryCanceled,
                psycopg.errors.LockNotAvailable,
                # A migration that depends on an owner-only DDL statement this SP was NOT
                # allowed to run (e.g. a backfill/index that needs a column a skipped
                # ALTER couldn't add) surfaces as UndefinedColumn/UndefinedTable/-Object.
                # On the deployed DB the privileged deploy-time migrate step (scripts/
                # migrate_schema.py) applies that DDL; here we must NEVER let the cascade
                # crash-loop the whole app — a request-time 500 on one path beats the line
                # going down. (Cost: a genuinely mis-referenced object is skipped silently;
                # acceptable for this best-effort, idempotent bootstrap.)
                psycopg.errors.UndefinedColumn,
                psycopg.errors.UndefinedTable,
                psycopg.errors.UndefinedObject,
            ) as exc:
                conn.rollback()
                log.warning("Schema statement skipped (%s): %.80s", exc, stmt)
    log.info("Schema ensured (agents, messages, checkpoints, claims).")


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


def register_agent(agent_id: str, owner: str, repo: str, branch: str | None, display_name: str | None) -> dict:
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
    log.info("Agent registered/refreshed: agent_id=%s repo=%s branch=%s", agent_id, repo, branch)
    return row


def _heartbeat(conn, agent_id: str) -> None:
    """Refresh presence for an already-registered agent (no-op if unregistered)."""
    cur = conn.execute("UPDATE agents SET last_heartbeat = now() WHERE agent_id = %s", (agent_id,))
    if cur.rowcount == 0:
        log.warning("Heartbeat ignored for unregistered agent_id=%s", agent_id)


def list_agents(active_within_seconds: int = 3600) -> list[dict]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT a.agent_id, a.owner, a.repo, a.branch, a.display_name,
                      a.registered_at, a.last_heartbeat,
                      s.payload    AS last_status,
                      s.created_at AS last_status_at
               FROM agents a
               LEFT JOIN LATERAL (
                   SELECT payload, created_at
                   FROM messages m
                   WHERE m.agent_id = a.agent_id AND m.message_type = 'status'
                   ORDER BY m.created_at DESC
                   LIMIT 1
               ) s ON true
               WHERE a.last_heartbeat > now() - make_interval(secs => %s)
               ORDER BY a.last_heartbeat DESC""",
            (active_within_seconds,),
        ).fetchall()
    log.info("list_agents returned count=%s active_within_seconds=%s", len(rows), active_within_seconds)
    return rows


def get_agent_presence_diagnostics(active_within_seconds: int = 3600, sample_size: int = 50) -> dict:
    """Return a snapshot explaining why agents are considered active/stale.

    Useful when clients report "0 connected" while operators believe agents are alive.
    """
    with get_pool().connection() as conn:
        summary = conn.execute(
            """SELECT
                     count(*) AS total_agents,
                     count(*) FILTER (
                       WHERE last_heartbeat > now() - make_interval(secs => %s)
                     ) AS active_agents,
                     min(last_heartbeat) AS oldest_heartbeat,
                     max(last_heartbeat) AS newest_heartbeat
               FROM agents""",
            (active_within_seconds,),
        ).fetchone()

        rows = conn.execute(
            """SELECT
                     agent_id,
                     owner,
                     repo,
                     branch,
                     display_name,
                     registered_at,
                     last_heartbeat,
                     EXTRACT(EPOCH FROM (now() - last_heartbeat))::BIGINT AS heartbeat_age_seconds,
                     (last_heartbeat > now() - make_interval(secs => %s)) AS is_active
               FROM agents
               ORDER BY last_heartbeat DESC
               LIMIT %s""",
            (active_within_seconds, sample_size),
        ).fetchall()

    return {
        "active_within_seconds": active_within_seconds,
        "total_agents": int(summary["total_agents"] or 0),
        "active_agents": int(summary["active_agents"] or 0),
        "stale_agents": int((summary["total_agents"] or 0) - (summary["active_agents"] or 0)),
        "oldest_heartbeat": summary["oldest_heartbeat"],
        "newest_heartbeat": summary["newest_heartbeat"],
        "sample_size": sample_size,
        "sampled_agents": rows,
    }


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

_MSG_COLS = (
    "id, agent_id, author, message_type, recipient, repo, branch, resource_path, "
    "issue_ref, summary, payload, metadata, mentions, reply_to_id, conversation_id::text, created_at"
)

# Same columns as _MSG_COLS but payload is elided ONLY when a summary exists — used by the
# token-frugal read paths (get_unread/get_conversation with include_payload=false). Agents
# get the concise summary and refetch full payload on demand. Messages with no summary
# (legacy rows, or callers that didn't set one) still return their payload, so no content
# is ever silently lost.
_MSG_COLS_CONCISE = _MSG_COLS.replace(
    "issue_ref, summary, payload,",
    "issue_ref, summary, CASE WHEN summary IS NULL THEN payload END AS payload,",
)


def post_message(
    agent_id: str,
    message_type: str,
    recipient: str | None,
    repo: str | None,
    branch: str | None,
    resource_path: str | None,
    issue_ref: str | None,
    payload: str | None,
    metadata: dict,
    mentions: list[str],
    reply_to_id: int | None,
    author: str | None,
    summary: str | None = None,
) -> dict:
    """Insert a message, refresh presence, and emit a NOTIFY for push delivery (FR1/FR2).

    ``recipient`` is the primary addressee ("To:"); the message is still broadcast to the
    whole line — recipient/mentions are for targeting and filtering, not privacy.
    """
    log.info("post_message agent_id=%s type=%s recipient=%s repo=%s", agent_id, message_type, recipient, repo)
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""INSERT INTO messages
                 (agent_id, author, message_type, recipient, repo, branch,
                  resource_path, issue_ref, summary, payload, metadata, mentions, reply_to_id,
                  conversation_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       -- inherit the parent's thread on a reply, else start a new one
                       COALESCE((SELECT conversation_id FROM messages WHERE id = %s),
                                gen_random_uuid()))
               RETURNING {_MSG_COLS}""",
            (
                agent_id,
                author,
                message_type,
                recipient,
                repo,
                branch,
                resource_path,
                issue_ref,
                summary,
                payload,
                json.dumps(metadata),
                mentions,
                reply_to_id,
                reply_to_id,  # again: for the COALESCE subquery that resolves the thread
            ),
        ).fetchone()
        _heartbeat(conn, agent_id)
        # Postgres NOTIFY has an 8000-byte payload limit; truncate the message
        # body so we never hit it (the full text is always in the DB row).
        notify_payload = payload[:7000] if payload and len(payload) > 7000 else payload
        conn.execute(
            "SELECT pg_notify(%s, %s)",
            (
                NOTIFY_CHANNEL,
                json.dumps(
                    {
                        "id": row["id"],
                        "agent_id": agent_id,
                        "message_type": message_type,
                        "recipient": recipient,
                        "repo": repo,
                        "branch": branch,
                        "resource_path": resource_path,
                        "summary": summary,
                        "payload": notify_payload,
                        "mentions": mentions,
                        "reply_to_id": reply_to_id,
                        "conversation_id": row["conversation_id"],
                        "created_at": str(row["created_at"]),
                    }
                ),
            ),
        )
        conn.commit()
    log.info("post_message OK id=%s agent_id=%s", row["id"], agent_id)
    return row


def get_since(
    after_id: int, limit: int = 500, for_agent: str | None = None, include_payload: bool = True
) -> list[dict]:
    """Return messages with id > after_id (FR3).

    If ``for_agent`` is given, restrict to messages addressed to that agent — i.e. it is
    the recipient or is mentioned ("for me" filter).

    ``include_payload`` False elides the (potentially large) payload, returning only the
    concise summary — the token-frugal default for agent reads. Callers refetch full
    payload on demand (e.g. get_conversation with include_payload=True).
    """
    log.debug("get_since after_id=%s limit=%s for_agent=%s payload=%s", after_id, limit, for_agent, include_payload)
    cols = _MSG_COLS if include_payload else _MSG_COLS_CONCISE
    where = "id > %s"
    params: list = [after_id]
    if for_agent is not None:
        where += " AND (recipient = %s OR %s = ANY(mentions))"
        params += [for_agent, for_agent]
    params.append(limit)
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {cols} FROM messages WHERE {where} ORDER BY id ASC LIMIT %s",
            tuple(params),
        ).fetchall()


def get_recent(limit: int = 500) -> list[dict]:
    """Return the most recent `limit` messages in ascending id order.

    For the dashboard initial seed: it must open on the LATEST activity. `get_since`
    tails forward from an id, so seeding it with after_id=0 returns the OLDEST rows —
    on a large backlog the feed then never showed recent posts (they looked unpersisted).
    We fetch newest-first with LIMIT, then reverse so the client still renders
    oldest→newest and can set its cursor to the last (highest) id.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_MSG_COLS} FROM messages ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return list(reversed(rows))


def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Full-text search over live + archived message payloads (inspect history).

    Uses websearch_to_tsquery (supports quoted phrases, OR, and -exclusions) against the
    GIN tsvector indexes, ranked by ts_rank then recency. Each hit carries its
    conversation_id (pivot to the full thread via get_conversation) and a `source` tag.
    """
    sql = f"""
        SELECT * FROM (
            SELECT {_MSG_COLS},
                   ts_rank(to_tsvector('english', coalesce(payload, '')), q) AS rank,
                   'live' AS source
              FROM messages, websearch_to_tsquery('english', %s) AS q
             WHERE to_tsvector('english', coalesce(payload, '')) @@ q
            UNION ALL
            SELECT {_MSG_COLS},
                   ts_rank(to_tsvector('english', coalesce(payload, '')), q) AS rank,
                   'archive' AS source
              FROM messages_archive, websearch_to_tsquery('english', %s) AS q
             WHERE to_tsvector('english', coalesce(payload, '')) @@ q
        ) hits
        ORDER BY rank DESC, id DESC
        LIMIT %s
    """
    with get_pool().connection() as conn:
        return conn.execute(sql, (query, query, limit)).fetchall()


def get_conversation(
    conversation_id: str, limit: int = 500, include_payload: bool = True
) -> list[dict]:
    """Return every message in a conversation (live + archived), oldest first.

    Defaults to full payload — this is the "drill into the detail" path an agent uses
    after seeing concise summaries elsewhere. Pass include_payload=False for a summary-only
    thread overview.
    """
    cols = _MSG_COLS if include_payload else _MSG_COLS_CONCISE
    sql = f"""
        SELECT * FROM (
            SELECT {cols}, 'live' AS source
              FROM messages WHERE conversation_id = %s::uuid
            UNION ALL
            SELECT {cols}, 'archive' AS source
              FROM messages_archive WHERE conversation_id = %s::uuid
        ) thread
        ORDER BY id ASC
        LIMIT %s
    """
    with get_pool().connection() as conn:
        return conn.execute(sql, (conversation_id, conversation_id, limit)).fetchall()


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #


def get_checkpoint(agent_id: str) -> int:
    log.debug("get_checkpoint agent_id=%s", agent_id)
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT last_read_message_id FROM checkpoints WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        return int(row["last_read_message_id"]) if row else 0


def ack_checkpoint(agent_id: str, last_read_message_id: int) -> dict:
    """Advance (monotonically) a per-agent checkpoint (FR4)."""
    log.info("ack_checkpoint agent_id=%s last_read_message_id=%s", agent_id, last_read_message_id)
    with get_pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO checkpoints (agent_id, last_read_message_id, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (agent_id) DO UPDATE
                 SET last_read_message_id = GREATEST(
                       checkpoints.last_read_message_id, EXCLUDED.last_read_message_id),
                     updated_at = now()
               RETURNING agent_id, last_read_message_id, updated_at""",
            (agent_id, last_read_message_id),
        ).fetchone()
        _heartbeat(conn, agent_id)
        conn.commit()
    return row


# --------------------------------------------------------------------------- #
# Claims — keyed on (repo, branch, resource_path); FR5
# --------------------------------------------------------------------------- #

_CLAIM_COLS = "id, agent_id, repo, branch, resource_path, resource_type, claim_scope, reason, created_at, expires_at"


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
    """Atomically acquire a lease on (repo, branch, resource_path) (FR5).

    Returns (acquired, claim). The unique index on (repo, COALESCE(branch,''),
    resource_path) plus a WHERE guard means an unexpired claim held by *another* agent
    cannot be stolen, while an expired claim or the agent's own claim is (re)acquired.
    Two agents racing cannot both win. The holder is registered (FK to agents).
    """
    log.info(
        "claim_resource agent_id=%s repo=%s branch=%s resource=%s ttl=%s",
        agent_id,
        repo,
        branch,
        resource_path,
        ttl_seconds,
    )
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""INSERT INTO claims
                 (agent_id, repo, branch, resource_path, resource_type,
                  claim_scope, reason, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       now() + make_interval(secs => %s))
               ON CONFLICT (repo, (COALESCE(branch, '')), resource_path) DO UPDATE
                 SET agent_id = EXCLUDED.agent_id,
                     resource_type = EXCLUDED.resource_type,
                     claim_scope = EXCLUDED.claim_scope,
                     reason = EXCLUDED.reason,
                     created_at = now(),
                     expires_at = EXCLUDED.expires_at
                 WHERE claims.expires_at < now()
                    OR claims.agent_id = EXCLUDED.agent_id
               RETURNING {_CLAIM_COLS}""",
            (agent_id, repo, branch, resource_path, resource_type, claim_scope, reason, ttl_seconds),
        ).fetchone()
        _heartbeat(conn, agent_id)
        conn.commit()
        if row:
            log.info("claim_resource ACQUIRED agent_id=%s resource=%s", agent_id, resource_path)
            return True, row
        holder = conn.execute(
            f"""SELECT {_CLAIM_COLS} FROM claims
                WHERE repo = %s AND COALESCE(branch, '') = COALESCE(%s, '')
                  AND resource_path = %s""",
            (repo, branch, resource_path),
        ).fetchone()
    log.info(
        "claim_resource REFUSED agent_id=%s resource=%s holder=%s",
        agent_id,
        resource_path,
        holder.get("agent_id") if holder else "?",
    )
    return False, holder


def release_resource(agent_id: str, repo: str, branch: str | None, resource_path: str) -> bool:
    """Release a claim, only if held by the caller (FR6). Returns True if released."""
    log.info("release_resource agent_id=%s repo=%s resource=%s", agent_id, repo, resource_path)
    with get_pool().connection() as conn:
        row = conn.execute(
            """DELETE FROM claims
               WHERE repo = %s AND COALESCE(branch, '') = COALESCE(%s, '')
                 AND resource_path = %s AND agent_id = %s
               RETURNING id""",
            (repo, branch, resource_path, agent_id),
        ).fetchone()
        conn.commit()
    released = row is not None
    log.info("release_resource agent_id=%s resource=%s released=%s", agent_id, resource_path, released)
    return released


def list_claims() -> list[dict]:
    log.debug("list_claims")
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {_CLAIM_COLS} FROM claims WHERE expires_at > now() ORDER BY repo, resource_path"
        ).fetchall()


def get_metrics() -> dict:
    """Compute real-time partyline metrics from the database."""
    log.debug("get_metrics")
    with get_pool().connection() as conn:
        row = conn.execute("""
            SELECT
                (SELECT count(*) FROM messages) AS total_messages,
                (SELECT count(*) FROM messages WHERE created_at > now() - interval '1 minute') AS messages_last_1m,
                (SELECT count(*) FROM messages WHERE created_at > now() - interval '5 minutes') AS messages_last_5m,
                (SELECT count(*) FROM messages WHERE created_at > now() - interval '1 hour') AS messages_last_1h,
                (SELECT count(*) FROM agents WHERE last_heartbeat > now() - interval '5 minutes') AS active_agents_5m,
                (SELECT count(*) FROM agents WHERE last_heartbeat > now() - interval '1 hour') AS active_agents_1h,
                (SELECT count(*) FROM claims WHERE expires_at > now()) AS active_claims,
                (SELECT max(created_at) FROM messages) AS last_message_at,
                (SELECT count(DISTINCT agent_id) FROM messages WHERE created_at > now() - interval '1 hour') AS unique_posters_1h
        """).fetchone()
        return row if row else {}
