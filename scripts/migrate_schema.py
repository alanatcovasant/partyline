"""Apply the partyline schema DDL as the OWNER, at deploy time, before the app starts.

WHY: the deployed app connects as its service principal, which does NOT own the
`partyline.*` tables (owner is the deploying user). So DDL from the app's `init_schema`
(ALTER TABLE, CREATE INDEX) fails with InsufficientPrivilege and is skipped — a migration
that adds a column then never lands on deployed, and a dependent statement can crash the
app. (This caused a ~1h outage on 2026-07-11; see
docs/adr/2026-07-11-conversation-threading-and-search.md.)

This step runs the SAME statement list as `init_schema`, but with the DEPLOYING USER's
privileges (the table owner), and grants the app SP DML so it can read/write — including
any columns added here. deploy.sh runs it AFTER provisioning and BEFORE `bundle deploy`,
so owner-only schema changes are in place before the new app code starts. Idempotent;
safe to re-run. The app's `init_schema` remains as a best-effort safety net for local/
fresh dev (where the app owns its tables).

Usage:
    ENDPOINT_NAME=projects/partyline/branches/production/endpoints/primary \\
      APP_NAME=mcp-covasant-partyline DATABRICKS_CONFIG_PROFILE=<profile> \\
      python scripts/migrate_schema.py

Set PARTYLINE_PG_CONNINFO to bypass the SDK (local testing); then SP grants are skipped.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys

import psycopg

# Make the app package importable so we reuse its schema SQL (single source of truth).
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "covasant-partyline"))

from app.db import _MIGRATIONS, _REPLICA_IDENTITY, _SCHEMA  # noqa: E402

SCHEMA = "partyline"
DEFAULT_ENDPOINT = "projects/partyline/branches/production/endpoints/primary"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("partyline.migrate")


def _resolve() -> tuple[str, dict, str | None]:
    """Return (conninfo, connect_kwargs, sp_client_id). sp_client_id is None when it
    can't be resolved (local override, or app not found) — grants are then skipped."""
    override = os.environ.get("PARTYLINE_PG_CONNINFO")
    if override:
        return override, {}, None

    from databricks.sdk import WorkspaceClient

    endpoint = os.environ.get("ENDPOINT_NAME", DEFAULT_ENDPOINT)
    w = WorkspaceClient()
    ep = w.postgres.get_endpoint(name=endpoint)
    host = ep.status.hosts.host
    user = w.current_user.me().user_name  # the deploying user = table owner
    token = w.postgres.generate_database_credential(endpoint=endpoint).token
    conninfo = f"host={host} port=5432 dbname=databricks_postgres user={user} sslmode=require"

    sp_client_id = None
    app_name = os.environ.get("APP_NAME", "mcp-covasant-partyline")
    try:
        sp_client_id = w.apps.get(name=app_name).service_principal_client_id
    except Exception as exc:  # app may not exist yet on a first-ever deploy
        log.warning("Could not resolve app SP client id (%s); skipping grants", exc)
    return conninfo, {"password": token}, sp_client_id


def _grants(sp_client_id: str) -> list[str]:
    """DML grants so the app SP can use owner-created objects (incl. future columns/tables)."""
    sp = sp_client_id.replace('"', "")  # defensive; it's a uuid
    return [
        f'GRANT USAGE ON SCHEMA {SCHEMA} TO "{sp}"',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} TO "{sp}"',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO "{sp}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} '
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{sp}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} '
        f'GRANT USAGE, SELECT ON SEQUENCES TO "{sp}"',
    ]


def main() -> None:
    conninfo, kwargs, sp_client_id = _resolve()

    statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
    statements += _MIGRATIONS
    statements += _REPLICA_IDENTITY
    if sp_client_id:
        statements += _grants(sp_client_id)
        log.info("Will grant app SP %s DML on schema %s", sp_client_id, SCHEMA)

    ok = skipped = 0
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=15, **kwargs) as conn:
        conn.execute(f"SET search_path TO {SCHEMA}, public")
        for stmt in statements:
            try:
                conn.execute(stmt)
                ok += 1
            except Exception as exc:
                # Best-effort: on a FRESH db the ALTERs skip (table not yet created by the
                # app); on an existing db the CREATEs are no-ops. Log and continue.
                skipped += 1
                log.warning("skipped (%s): %.90s", type(exc).__name__, " ".join(stmt.split()))
    log.info("Schema migrate as owner complete: %d applied, %d skipped", ok, skipped)


if __name__ == "__main__":
    main()
