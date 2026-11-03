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

import difflib
import json
import logging
import threading
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import presence
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

CREATE TABLE IF NOT EXISTS progress_tasks (
    id UUID PRIMARY KEY,
    folder TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','active','blocked','done','cancelled')),
    agent_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT,
    workstream TEXT,
    workdir TEXT,
    branch TEXT,
    issue_ref TEXT,
    next_action TEXT,
    handoff_to TEXT,
    handoff_from TEXT,
    handoff_note TEXT,
    handoff_offered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS progress_one_current_folder
    ON progress_tasks(folder) WHERE status IN ('active','blocked');
CREATE INDEX IF NOT EXISTS progress_folder ON progress_tasks(folder, created_at DESC);
CREATE TABLE IF NOT EXISTS progress_updates (
    id BIGSERIAL PRIMARY KEY,
    operation_id UUID UNIQUE NOT NULL,
    task_id UUID NOT NULL REFERENCES progress_tasks(id) ON DELETE CASCADE,
    folder TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT,
    author TEXT,
    request JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS progress_task_history ON progress_updates(task_id, id DESC);

CREATE TABLE IF NOT EXISTS vq_items (
    id UUID PRIMARY KEY,
    issue_ref TEXT NOT NULL,
    target TEXT NOT NULL,
    script TEXT,
    proc TEXT,
    pr TEXT,
    enqueued_by TEXT NOT NULL,
    claimed_by TEXT,
    claimed_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issue_ref, target)
);
CREATE TABLE IF NOT EXISTS vq_runs (
    id BIGSERIAL PRIMARY KEY,
    operation_id UUID UNIQUE NOT NULL,
    item_id UUID NOT NULL REFERENCES vq_items(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    exit_code INT NOT NULL CHECK (exit_code IN (0,1,2)),
    executed BOOLEAN NOT NULL DEFAULT false,
    commit TEXT,
    output_tail TEXT,
    request JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vq_runs_item ON vq_runs(item_id, id DESC);

-- Work queue: a read-through MIRROR of GitHub issues (GitHub stays the source). A claim is a live row in
-- claims keyed issue:owner/repo#N, never a column here. Keep comments semicolon-free: _SCHEMA is split on it.
CREATE TABLE IF NOT EXISTS work_items (
    source_ref     TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    number         INT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT,
    gh_state       TEXT NOT NULL DEFAULT 'open',
    labels         JSONB NOT NULL DEFAULT '[]',
    assignees      JSONB NOT NULL DEFAULT '[]',
    priority       TEXT,
    severity       TEXT,
    points         INT,
    cluster        TEXT,
    band           INT,
    claim_label    BOOLEAN NOT NULL DEFAULT false,
    external_claim TEXT,
    gh_created_at  TIMESTAMPTZ,
    gh_updated_at  TIMESTAMPTZ,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_items_queue ON work_items(repo, gh_state, band, points, gh_created_at);

CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    owner          TEXT NOT NULL,
    repo           TEXT NOT NULL,
    branch         TEXT,
    display_name   TEXT,
    -- Optional client fingerprint (harness + model). Nullable: not every runtime can
    -- report them. Keep this comment semicolon-free: _SCHEMA is split on the semicolon.
    harness        TEXT,
    model          TEXT,
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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- event_time: when the agent EMITTED the event (set by the bridge at enqueue) —
    --   the truthful "when did this happen", since a queued/retried/reposted send may
    --   insert much later. received_at: when the APP accepted the POST. Both nullable so
    --   existing rows and non-bridge callers keep working — post_message defaults
    --   event_time to now(). (Keep this comment semicolon-free: _SCHEMA is split on the
    --   semicolon.)
    event_time    TIMESTAMPTZ,
    received_at   TIMESTAMPTZ
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
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_path
    ON claims (resource_path);

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
    # Work-queue phase 1: ownership handoff on the progress ledger (docs/PROPOSAL_Unified_Work_Queue).
    "ALTER TABLE progress_tasks ADD COLUMN IF NOT EXISTS next_action TEXT",
    "ALTER TABLE progress_tasks ADD COLUMN IF NOT EXISTS handoff_to TEXT",
    "ALTER TABLE progress_tasks ADD COLUMN IF NOT EXISTS handoff_from TEXT",
    "ALTER TABLE progress_tasks ADD COLUMN IF NOT EXISTS handoff_note TEXT",
    "ALTER TABLE progress_tasks ADD COLUMN IF NOT EXISTS handoff_offered_at TIMESTAMPTZ",
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
    # 2026-08-01 claim semantics: claims key on the target alone (canonical absolute path
    # or work-item URI). repo/branch stay as display metadata but leave the key — git
    # worktrees put the same (repo, branch, relpath) at two different directories, so they
    # never identified the thing being claimed.
    "DROP INDEX IF EXISTS ux_claims_key",
    # The old key allowed the SAME resource_path under different (repo, branch) pairs, so
    # the live table really does hold duplicates — creating the new index without clearing
    # them first fails with UniqueViolation, and because that failure is *skipped* rather
    # than fatal, the app would then run ON CONFLICT (resource_path) against an index that
    # does not exist. Expired leases are garbage by definition: drop them.
    "DELETE FROM claims WHERE expires_at < now()",
    # Among any surviving live duplicates keep the longest-lived (id as tie-break) — that
    # is the claim an agent is most likely still relying on.
    """
    DELETE FROM claims a USING claims b
     WHERE a.resource_path = b.resource_path
       AND (a.expires_at < b.expires_at
            OR (a.expires_at = b.expires_at AND a.id < b.id))
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_path ON claims (resource_path)",
    # Full-text search (FR: inspect history). websearch_to_tsquery queries these.
    "CREATE INDEX IF NOT EXISTS idx_messages_fts ON messages USING GIN (to_tsvector('english', coalesce(payload, '')))",
    "CREATE INDEX IF NOT EXISTS idx_archive_fts  ON messages_archive USING GIN (to_tsvector('english', coalesce(payload, '')))",
    # Thread lookup (get_conversation).
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_archive_conversation  ON messages_archive (conversation_id, id)",
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
    # Optional agent runtime fingerprint (harness + model). Nullable; re-register omit
    # preserves via COALESCE in register_agent — mid-session updates use update_agent_runtime.
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS harness TEXT",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS model TEXT",
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


def register_agent(
    agent_id: str,
    owner: str,
    repo: str,
    branch: str | None,
    display_name: str | None,
    workdirs: list[str] | None = None,
    harness: str | None = None,
    model: str | None = None,
) -> dict:
    """Register (or refresh) an agent, optionally taking territory.

    Each workdir is claimed as an exclusive `workdir` subtree. A refused workdir is
    REPORTED, never fatal: an agent that cannot register is invisible to the line, which
    is strictly worse than one that registers while overlapping. Surfacing the holder at
    session start is the whole point — it is the earliest moment an agent can act on the
    collision, and it rides on the one call every agent already makes.

    `harness` / `model` are optional — not every runtime can report them. On re-register,
    a null/omitted value PRESERVES the stored one (COALESCE); pass a new non-null value to
    overwrite, or call update_agent_runtime to change/clear mid-session without a full
    re-register.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO agents (agent_id, owner, repo, branch, display_name, harness, model)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (agent_id) DO UPDATE
                 SET owner = EXCLUDED.owner, repo = EXCLUDED.repo,
                     branch = EXCLUDED.branch, display_name = EXCLUDED.display_name,
                     harness = COALESCE(EXCLUDED.harness, agents.harness),
                     model = COALESCE(EXCLUDED.model, agents.model),
                     last_heartbeat = now()
               RETURNING agent_id, owner, repo, branch, display_name, harness, model,
                         registered_at, last_heartbeat""",
            (agent_id, owner, repo, branch, display_name, harness, model),
        ).fetchone()
        _extend_workdir_claims(conn, agent_id)
        conn.commit()

    results: list[dict] = []
    for path in workdirs or []:
        acquired, claims, blocker = claim_targets(
            agent_id,
            repo,
            branch,
            [(path, "workdir")],
            "exclusive",
            f"session workdir for {agent_id}",
            settings.workdir_claim_ttl_seconds,
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
                agent_id,
                path,
                blocker.get("agent_id") if blocker else "?",
            )
    row["workdir_claims"] = results
    log.info(
        "Agent registered/refreshed: agent_id=%s repo=%s branch=%s harness=%s model=%s workdirs=%s",
        agent_id,
        repo,
        branch,
        harness,
        model,
        len(results),
    )
    return row


def update_agent_runtime(
    agent_id: str,
    *,
    harness: str | None | object = ...,
    model: str | None | object = ...,
) -> dict | None:
    """Patch harness and/or model mid-session. Returns the agent row, or None if missing.

    Use the ellipsis sentinel for "leave unchanged". Explicit None clears the column.
    Also refreshes last_heartbeat (any call = alive).
    """
    sets: list[str] = ["last_heartbeat = now()"]
    params: list = []
    if harness is not ...:
        sets.append("harness = %s")
        params.append(harness)
    if model is not ...:
        sets.append("model = %s")
        params.append(model)
    if len(sets) == 1:
        raise ValueError("update_agent_runtime requires at least one of harness, model")
    params.append(agent_id)
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""UPDATE agents SET {', '.join(sets)}
                WHERE agent_id = %s
                RETURNING agent_id, owner, repo, branch, display_name, harness, model,
                          registered_at, last_heartbeat""",
            params,
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        _extend_workdir_claims(conn, agent_id)
        conn.commit()
    log.info(
        "Agent runtime updated: agent_id=%s harness=%s model=%s",
        agent_id,
        row.get("harness"),
        row.get("model"),
    )
    return row


def _extend_workdir_claims(conn, agent_id: str) -> None:
    """Push this agent's live `workdir` claims out to a full TTL.

    Territory follows the session: an active agent keeps its subtree, and a dead one drops
    it one TTL later without needing a session-end hook. Ordinary (task-scoped) claims are
    untouched — those expire on their own schedule.

    Called from both heartbeat paths. `register_agent` does its own upsert rather than
    going through `_heartbeat`, and a re-register is the strongest liveness signal there
    is, so it must renew territory too.
    """
    conn.execute(
        """UPDATE claims
              SET expires_at = now() + make_interval(secs => %s)
            WHERE agent_id = %s AND resource_type = 'workdir' AND expires_at > now()""",
        (settings.workdir_claim_ttl_seconds, agent_id),
    )


def _heartbeat(conn, agent_id: str) -> None:
    """Refresh presence for an already-registered agent (no-op if unregistered)."""
    cur = conn.execute("UPDATE agents SET last_heartbeat = now() WHERE agent_id = %s", (agent_id,))
    if cur.rowcount == 0:
        log.warning("Heartbeat ignored for unregistered agent_id=%s", agent_id)
        return
    _extend_workdir_claims(conn, agent_id)


def _classify_row(row: dict) -> dict:
    """Attach presence labels to an agent row that already carries both clock ages."""
    row.update(
        presence.classify(
            row.get("heartbeat_age_seconds"),
            row.get("read_age_seconds"),
            settings.presence_live_seconds,
            settings.presence_idle_seconds,
        )
    )
    return row


def list_agents(active_within_seconds: int | None = None, include_stale: bool = False) -> list[dict]:
    """Registered agents, each labelled with its presence (never filtered into oblivion).

    Every row carries `status` (live|idle|stale), `reading`, and both clock ages, so a
    caller can tell "that agent is not listening" from "that agent does not exist". The
    two are indistinguishable when a roster silently omits rows — the failure that made
    a sender read silence as consent.

    `include_stale=True` returns every agent ever registered. The default still applies
    the window, because the SessionStart banner is a token budget, not an audit: callers
    that need the truth ask for it, and nothing is ever deleted either way.
    """
    active_within_seconds = active_within_seconds or settings.presence_idle_seconds
    where = "" if include_stale else "WHERE a.last_heartbeat > now() - make_interval(secs => %(win)s)"
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""SELECT a.agent_id, a.owner, a.repo, a.branch, a.display_name,
                      a.harness, a.model,
                      a.registered_at, a.last_heartbeat,
                      EXTRACT(EPOCH FROM (now() - a.last_heartbeat))::BIGINT
                          AS heartbeat_age_seconds,
                      EXTRACT(EPOCH FROM (now() - c.updated_at))::BIGINT
                          AS read_age_seconds,
                      s.payload    AS last_status,
                      s.created_at AS last_status_at
               FROM agents a
               LEFT JOIN checkpoints c ON c.agent_id = a.agent_id
               LEFT JOIN LATERAL (
                   SELECT payload, created_at
                   FROM messages m
                   WHERE m.agent_id = a.agent_id AND m.message_type = 'status'
                   ORDER BY m.created_at DESC
                   LIMIT 1
               ) s ON true
               {where}
               ORDER BY a.last_heartbeat DESC""",
            {"win": active_within_seconds},
        ).fetchall()
    log.info(
        "list_agents returned count=%s active_within_seconds=%s include_stale=%s",
        len(rows), active_within_seconds, include_stale,
    )
    return [_classify_row(r) for r in rows]


def get_presence(conn, agent_ids: list[str]) -> dict[str, dict]:
    """Presence for specific agents, keyed by agent_id. Missing ids are simply absent.

    Takes an open connection so the send path can check and insert in one transaction.
    """
    if not agent_ids:
        return {}
    rows = conn.execute(
        """SELECT a.agent_id,
                  EXTRACT(EPOCH FROM (now() - a.last_heartbeat))::BIGINT
                      AS heartbeat_age_seconds,
                  EXTRACT(EPOCH FROM (now() - c.updated_at))::BIGINT
                      AS read_age_seconds
             FROM agents a
             LEFT JOIN checkpoints c ON c.agent_id = a.agent_id
            WHERE a.agent_id = ANY(%s)""",
        (list(agent_ids),),
    ).fetchall()
    return {r["agent_id"]: _classify_row(r) for r in rows}


def _suggest_agents(conn, unknown: str, limit: int = 3) -> list[str]:
    """Closest known agent_ids to a typo, else the agents currently live.

    A bare "unknown recipient" is only half an answer; the sender still has to guess the
    right spelling. Near-matches catch the typo case, and the live list catches the
    "I invented an id that never existed" case.
    """
    known = [r["agent_id"] for r in conn.execute("SELECT agent_id FROM agents").fetchall()]
    close = difflib.get_close_matches(unknown, known, n=limit, cutoff=0.6)
    if close:
        return close
    live = conn.execute(
        """SELECT agent_id FROM agents
            WHERE last_heartbeat > now() - make_interval(secs => %s)
            ORDER BY last_heartbeat DESC LIMIT %s""",
        (settings.presence_idle_seconds, limit),
    ).fetchall()
    return [r["agent_id"] for r in live]


def get_agent_presence_diagnostics(active_within_seconds: int | None = None, sample_size: int = 50) -> dict:
    """Return a snapshot explaining why agents are considered active/stale.

    Useful when clients report "0 connected" while operators believe agents are alive.
    """
    active_within_seconds = active_within_seconds or settings.presence_idle_seconds
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
                     a.agent_id,
                     a.owner,
                     a.repo,
                     a.branch,
                     a.display_name,
                     a.harness,
                     a.model,
                     a.registered_at,
                     a.last_heartbeat,
                     EXTRACT(EPOCH FROM (now() - a.last_heartbeat))::BIGINT
                         AS heartbeat_age_seconds,
                     EXTRACT(EPOCH FROM (now() - c.updated_at))::BIGINT
                         AS read_age_seconds,
                     (a.last_heartbeat > now() - make_interval(secs => %s)) AS is_active
               FROM agents a
               LEFT JOIN checkpoints c ON c.agent_id = a.agent_id
               ORDER BY a.last_heartbeat DESC
               LIMIT %s""",
            (active_within_seconds, sample_size),
        ).fetchall()
    rows = [_classify_row(r) for r in rows]

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
    "issue_ref, summary, payload, metadata, mentions, reply_to_id, conversation_id::text, "
    "created_at, event_time, received_at"
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


class UnknownRecipient(ValueError):
    """A post addressed to an agent_id that has never registered.

    Distinct from an idle one on purpose: idle is a timing problem (the message waits and
    is eventually read), unknown is a typo (the message waits forever). Only the second is
    worth failing the write for. Carries suggestions so the sender can fix it in one step.
    """

    def __init__(self, recipient: str, suggestions: list[str]) -> None:
        self.recipient = recipient
        self.suggestions = suggestions
        did_you_mean = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(
            f"unknown recipient {recipient!r} — no agent has ever registered under that id, "
            f"so this message could never be delivered.{did_you_mean} "
            "Pass allow_unregistered_recipient=true if you are deliberately addressing a "
            "session that has not started yet."
        )


def _check_delivery(
    conn,
    recipient: str | None,
    mentions: list[str],
    allow_unregistered_recipient: bool,
) -> tuple[dict | None, list[str]]:
    """Resolve the addressees BEFORE inserting. Returns (recipient_presence, warnings).

    Raises UnknownRecipient for an unregistered recipient (unless overridden). Unknown
    *mentions* only warn: a recipient is a structured field someone typed deliberately,
    while mentions are parsed out of prose (the dashboard turns '@name' into one), so a
    hard failure there would reject a legitimate message over a false positive in its text.
    """
    wanted = [x for x in ([recipient] if recipient else []) + list(mentions) if x]
    known = get_presence(conn, wanted)
    warnings: list[str] = []

    if recipient and recipient not in known:
        if not allow_unregistered_recipient and settings.strict_recipients:
            raise UnknownRecipient(recipient, _suggest_agents(conn, recipient))
        warnings.append(
            f"recipient {recipient!r} is not registered — delivered only if that id ever "
            "registers and reads the line"
        )

    recipient_presence = known.get(recipient) if recipient else None
    if recipient_presence:
        warning = presence.delivery_warning(recipient, recipient_presence)
        if warning:
            warnings.append(warning)

    unknown_mentions = [m for m in mentions if m not in known]
    if unknown_mentions:
        warnings.append(
            f"unregistered mentions: {', '.join(sorted(unknown_mentions))} — "
            "nothing will surface this to them"
        )
    return recipient_presence, warnings


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
    event_time=None,  # datetime | ISO-str | None; agent emit time (None -> now())
    *,
    # Keyword-only on purpose: the bridge passes event_time as the trailing POSITIONAL
    # arg (locked by tests/test_event_time.py), so anything added after it must never
    # claim a positional slot.
    allow_unregistered_recipient: bool = False,
    tag_everyone: bool = False,
) -> dict:
    """Insert a message, refresh presence, and emit a NOTIFY for push delivery (FR1/FR2).

    ``recipient`` is the primary addressee ("To:"); the message is still broadcast to the
    whole line — recipient/mentions are for targeting and filtering, not privacy.

    The write is durable, but delivery is not observable: a post into a recipient with no
    listener has always returned success, so the sender learned nothing and read silence
    as agreement. The returned row therefore carries ``recipient_presence`` and
    ``delivery_warnings``, and an unknown recipient raises instead of succeeding quietly.
    """
    log.info("post_message agent_id=%s type=%s recipient=%s repo=%s", agent_id, message_type, recipient, repo)
    active_ids: list[str] = []
    with get_pool().connection() as conn:
        if tag_everyone:
            active_ids = [
                row["agent_id"]
                for row in conn.execute(
                    """SELECT agent_id
                       FROM agents
                       WHERE agent_id <> %s
                         AND last_heartbeat > now() - make_interval(secs => %s)
                       ORDER BY agent_id""",
                    (agent_id, settings.presence_idle_seconds),
                ).fetchall()
            ]
            # Explicit mentions remain authoritative (including a deliberate self-mention),
            # while the expanded output is deterministic and duplicate-free.
            mentions = sorted({x for x in [*mentions, *active_ids] if x})
        tag_warning = (
            ["tag_everyone matched no other non-stale agents"]
            if tag_everyone and not active_ids
            else []
        ) if tag_everyone else []
        recipient_presence, warnings = _check_delivery(
            conn, recipient, mentions, allow_unregistered_recipient
        )
        warnings = tag_warning + warnings
        row = conn.execute(
            f"""INSERT INTO messages
                 (agent_id, author, message_type, recipient, repo, branch,
                  resource_path, issue_ref, summary, payload, metadata, mentions, reply_to_id,
                  conversation_id, event_time, received_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       -- inherit the parent's thread on a reply, else start a new one
                       COALESCE((SELECT conversation_id FROM messages WHERE id = %s),
                                gen_random_uuid()),
                       COALESCE(%s::timestamptz, now()), now())
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
                event_time,
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
                        "event_time": str(row["event_time"]),
                    }
                ),
            ),
        )
        conn.commit()
    # Reported on the write, not left for the sender to infer from silence later.
    row["recipient_presence"] = recipient_presence
    row["delivery_warnings"] = warnings
    for warning in warnings:
        log.warning("post_message id=%s delivery: %s", row["id"], warning)
    log.info("post_message OK id=%s agent_id=%s warnings=%s", row["id"], agent_id, len(warnings))
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
            f"SELECT * FROM (SELECT {cols} FROM messages WHERE {where} "
            f"ORDER BY id ASC LIMIT %s) w ORDER BY event_time ASC, id ASC",
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
            f"SELECT {_MSG_COLS} FROM messages ORDER BY event_time DESC NULLS LAST, id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return list(reversed(rows))


def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Full-text search over live + archived history (inspect history).

    Thin wrapper over query_messages(q=...). Note: the full-text index/expression now
    spans summary+payload, so this matches summary text too (an intended improvement over
    the old payload-only search). Each hit carries conversation_id (pivot to the full
    thread via get_conversation) and a `source` tag.
    """
    return query_messages(q=query, limit=limit)


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
# Claims — keyed on the target alone (canonical absolute path or work-item URI); FR5
# --------------------------------------------------------------------------- #

_CLAIM_COLS = "id, agent_id, repo, branch, resource_path, resource_type, claim_scope, reason, created_at, expires_at"

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
                    agent_id,
                    path,
                    blocker.get("agent_id"),
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
        agent_id,
        repo,
        branch,
        [(resource_path, resource_type)],
        claim_scope,
        reason,
        ttl_seconds,
    )
    return (True, rows[0]) if acquired else (False, blocker)


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


def archive_all_messages() -> dict:
    """Move every live message into messages_archive and clear the live table.

    Same move-then-delete pattern as the retention job (src/covasant_partyline_jobs), but
    for ALL messages regardless of age — an operator "archive everything now" action. Runs
    in a single transaction so a failure rolls back both halves and no history is lost;
    idempotent via ON CONFLICT. The app SP has DML on these tables (no owner-only DDL), so
    this needs no privileged step. Returns counts for UI feedback.
    """
    log.info("archive_all_messages requested")
    with get_pool().connection() as conn:
        archived = conn.execute(
            "INSERT INTO messages_archive SELECT * FROM messages ON CONFLICT (id) DO NOTHING"
        ).rowcount
        # reply_to_id is a self-FK; null out references before the wholesale delete. (Not
        # strictly needed when deleting ALL rows, but keeps parity with the retention job
        # and is safe.)
        conn.execute("UPDATE messages SET reply_to_id = NULL WHERE reply_to_id IS NOT NULL")
        deleted = conn.execute("DELETE FROM messages").rowcount
        conn.commit()
    log.info("archive_all_messages OK archived=%s deleted=%s", archived, deleted)
    return {"archived": archived, "deleted": deleted}
