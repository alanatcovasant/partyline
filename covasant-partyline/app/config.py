"""Runtime configuration, sourced entirely from environment variables.

In a deployed Databricks App the Postgres (PG*) variables are auto-injected when a
Lakebase resource is bound to the app; ``ENDPOINT_NAME`` (the autoscale endpoint resource
name) is *not* auto-injected and is supplied by the bundle's app config.env.
Locally, populate these via a .env file (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- HTTP server ---
    # Databricks Apps inject the port the app must bind to.
    port: int = int(os.getenv("DATABRICKS_APP_PORT", "8000"))
    host: str = os.getenv("APP_HOST", "0.0.0.0")

    # --- Lakebase / Postgres (autoscale) ---
    pg_host: str | None = os.getenv("PGHOST")
    pg_database: str = os.getenv("PGDATABASE", "databricks_postgres")
    pg_user: str | None = os.getenv("PGUSER")
    pg_port: str = os.getenv("PGPORT", "5432")
    pg_sslmode: str = os.getenv("PGSSLMODE", "require")
    # Full Lakebase endpoint resource name, e.g.
    #   projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>
    # Used to mint short-lived OAuth database credentials and to self-provision the
    # project/branch/endpoint if missing (see app/db.py). Normally supplied by app.yaml env
    # (re-read on every start). The default is a self-heal fallback: if the env var is ever
    # dropped (e.g. a config layer that isn't re-applied on restart), db_configured stays
    # true and the DB layer still comes up, rather than 500'ing every route with PoolClosed.
    endpoint_name: str | None = os.getenv(
        "ENDPOINT_NAME", "projects/partyline/branches/production/endpoints/primary"
    )

    # --- Behaviour ---
    # Whether to run CREATE TABLE IF NOT EXISTS migrations on startup (FR9).
    auto_migrate: bool = os.getenv("PARTYLINE_AUTO_MIGRATE", "true").lower() == "true"
    # Default TTL (seconds) for a resource claim when the caller doesn't specify one.
    default_claim_ttl_seconds: int = int(os.getenv("PARTYLINE_CLAIM_TTL", "900"))
    # A session's territory outlives a single task, so workdir claims get their own,
    # longer TTL. Refreshed on every heartbeat, so a live session holds indefinitely and
    # a dead one releases one TTL later without a session-end hook.
    workdir_claim_ttl_seconds: int = int(os.getenv("PARTYLINE_WORKDIR_TTL", "3600"))
    # Presence thresholds (see presence.py). Liveness DECAYS — it never removes a row.
    # An agent within live_seconds is 'live', within idle_seconds is 'idle', beyond that
    # 'stale'. Identity and read cursors survive all three: a returning agent must never
    # re-read its whole backlog.
    presence_live_seconds: int = int(os.getenv("PARTYLINE_PRESENCE_LIVE", "300"))
    presence_idle_seconds: int = int(os.getenv("PARTYLINE_PRESENCE_IDLE", "3600"))
    # Refuse a post addressed to an agent_id that has never registered. On by default: an
    # unknown recipient is a typo, and a typo'd handoff is silently undeliverable forever.
    # Callers with a genuine pre-registration handoff pass allow_unregistered_recipient.
    strict_recipients: bool = os.getenv("PARTYLINE_STRICT_RECIPIENTS", "true").lower() == "true"
    # Local development mode — bypass Lakebase OAuth, use PGPASSWORD instead.
    local: bool = os.getenv("PARTYLINE_LOCAL", "").lower() in ("true", "1", "yes")

    @property
    def db_configured(self) -> bool:
        # Re-read from the live environment on every check — Databricks Apps may
        # inject PGHOST/PGUSER *after* the process starts (resource binding race).
        pg_host = self.pg_host or os.getenv("PGHOST")
        pg_user = self.pg_user or os.getenv("PGUSER")
        endpoint = self.endpoint_name or os.getenv("ENDPOINT_NAME")
        if self.local:
            return bool(pg_host and pg_user)
        return bool(pg_host and pg_user and endpoint)


settings = Settings()
