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
_workspace_client = None


def _ws():
    global _workspace_client
    if _workspace_client is None:
        from databricks.sdk import WorkspaceClient

        _workspace_client = WorkspaceClient()
    return _workspace_client


def _mint_token() -> str:
    """Mint a short-lived, Lakebase-scoped OAuth credential for Postgres login.

    A workspace-scoped OAuth token would be rejected at Postgres login — this must be a
    database credential from ``postgres.generate_database_credential``, scoped to the
    autoscale endpoint.
    """
    cred = _ws().postgres.generate_database_credential(endpoint=settings.endpoint_name)
    return cred.token


def _conninfo() -> str:
    return (
        f"dbname={settings.pg_database} "
        f"user={settings.pg_user} "
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"sslmode={settings.pg_sslmode}"
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
            "kwargs": {"row_factory": dict_row, "options": f"-c search_path={APP_SCHEMA}"},
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
    return _pool


def open_pool() -> None:
    get_pool().open(wait=True, timeout=30.0)


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


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
    payload       TEXT,
    metadata      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    mentions      TEXT[]      NOT NULL DEFAULT '{}',
    reply_to_id   BIGINT      REFERENCES messages(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_issue_ref ON messages (issue_ref);

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


def init_schema() -> None:
    """Idempotent bootstrap, statement by statement.

    Privilege errors are non-fatal: after an app rename/replace, objects may be owned
    by a prior identity (this SP has access via GRANTs). Everything here is CREATE IF
    NOT EXISTS or optional (REPLICA IDENTITY), so on an already-provisioned database a
    skipped statement is safe — log it and continue rather than crash the app.
    """
    statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
    statements += _REPLICA_IDENTITY
    with get_pool().connection() as conn:
        for stmt in statements:
            try:
                conn.execute(stmt)
                conn.commit()
            except psycopg.errors.InsufficientPrivilege as exc:
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
            """SELECT agent_id, owner, repo, branch, display_name,
                      registered_at, last_heartbeat
               FROM agents
               WHERE last_heartbeat > now() - make_interval(secs => %s)
               ORDER BY last_heartbeat DESC""",
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
    "issue_ref, payload, metadata, mentions, reply_to_id, created_at"
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
) -> dict:
    """Insert a message, refresh presence, and emit a NOTIFY for push delivery (FR1/FR2).

    ``recipient`` is the primary addressee ("To:"); the message is still broadcast to the
    whole line — recipient/mentions are for targeting and filtering, not privacy.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""INSERT INTO messages
                 (agent_id, author, message_type, recipient, repo, branch,
                  resource_path, issue_ref, payload, metadata, mentions, reply_to_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                payload,
                json.dumps(metadata),
                mentions,
                reply_to_id,
            ),
        ).fetchone()
        _heartbeat(conn, agent_id)
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
                        "mentions": mentions,
                    }
                ),
            ),
        )
        conn.commit()
    return row


def get_since(after_id: int, limit: int = 500, for_agent: str | None = None) -> list[dict]:
    """Return messages with id > after_id (FR3).

    If ``for_agent`` is given, restrict to messages addressed to that agent — i.e. it is
    the recipient or is mentioned ("for me" filter).
    """
    where = "id > %s"
    params: list = [after_id]
    if for_agent is not None:
        where += " AND (recipient = %s OR %s = ANY(mentions))"
        params += [for_agent, for_agent]
    params.append(limit)
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {_MSG_COLS} FROM messages WHERE {where} ORDER BY id ASC LIMIT %s",
            tuple(params),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #


def get_checkpoint(agent_id: str) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT last_read_message_id FROM checkpoints WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
        return int(row["last_read_message_id"]) if row else 0


def ack_checkpoint(agent_id: str, last_read_message_id: int) -> dict:
    """Advance (monotonically) a per-agent checkpoint (FR4)."""
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
            return True, row
        holder = conn.execute(
            f"""SELECT {_CLAIM_COLS} FROM claims
                WHERE repo = %s AND COALESCE(branch, '') = COALESCE(%s, '')
                  AND resource_path = %s""",
            (repo, branch, resource_path),
        ).fetchone()
    return False, holder


def release_resource(agent_id: str, repo: str, branch: str | None, resource_path: str) -> bool:
    """Release a claim, only if held by the caller (FR6). Returns True if released."""
    with get_pool().connection() as conn:
        row = conn.execute(
            """DELETE FROM claims
               WHERE repo = %s AND COALESCE(branch, '') = COALESCE(%s, '')
                 AND resource_path = %s AND agent_id = %s
               RETURNING id""",
            (repo, branch, resource_path, agent_id),
        ).fetchone()
        conn.commit()
    return row is not None


def list_claims() -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {_CLAIM_COLS} FROM claims WHERE expires_at > now() ORDER BY repo, resource_path"
        ).fetchall()


def get_metrics() -> dict:
    """Compute real-time partyline metrics from the database."""
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
