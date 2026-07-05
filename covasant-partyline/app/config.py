"""Runtime configuration, sourced entirely from environment variables.

In a deployed Databricks App the Postgres (PG*) variables are auto-injected when a
Lakebase resource is attached to the app; ``ENDPOINT_NAME`` is *not* auto-injected and
must be supplied explicitly via app.yaml (see the bundle's postgres resources).
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
    # Used to mint short-lived OAuth database credentials.
    endpoint_name: str | None = os.getenv("ENDPOINT_NAME")

    # --- Behaviour ---
    # Whether to run CREATE TABLE IF NOT EXISTS migrations on startup (FR9).
    auto_migrate: bool = os.getenv("PARTYLINE_AUTO_MIGRATE", "true").lower() == "true"
    # Default TTL (seconds) for a resource claim when the caller doesn't specify one.
    default_claim_ttl_seconds: int = int(os.getenv("PARTYLINE_CLAIM_TTL", "900"))
    # Local development mode — bypass Lakebase OAuth, use PGPASSWORD instead.
    local: bool = os.getenv("PARTYLINE_LOCAL", "").lower() in ("true", "1", "yes")

    @property
    def db_configured(self) -> bool:
        if self.local:
            return bool(self.pg_host and self.pg_user)
        return bool(self.pg_host and self.pg_user and self.endpoint_name)


settings = Settings()
