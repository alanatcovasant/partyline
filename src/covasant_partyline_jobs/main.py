"""Partyline retention job — prunes old messages from Lakebase Postgres.

Native Lakehouse Sync (wal2delta) mirrors rows to Delta SCD2 history *before*
they're deleted, so the archive persists in Unity Catalog while the OLTP tier
stays small.  See PRD §7.10.

Connection: unlike the app (which gets PG* auto-injected by its bound database
resource), a job gets nothing injected. So we resolve everything from the autoscale
endpoint via the SDK: host from the endpoint, user = the job's run-as identity,
password = a freshly minted Lakebase database credential. The run-as identity must
have access to the `partyline` schema (the project owner does).

Set PARTYLINE_PG_CONNINFO to bypass the SDK entirely (local testing).
"""

import argparse
import logging
import os

import psycopg

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "projects/partyline/branches/production/endpoints/primary"
SCHEMA = "partyline"  # must match APP_SCHEMA in the app (app/db.py)


def _conninfo(endpoint_name: str) -> tuple[str, dict]:
    """Resolve (conninfo, kwargs) for psycopg.connect."""
    override = os.environ.get("PARTYLINE_PG_CONNINFO")
    if override:
        return override, {}

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    endpoint = w.postgres.get_endpoint(name=endpoint_name)
    host = endpoint.status.hosts.host
    user = w.current_user.me().user_name
    token = w.postgres.generate_database_credential(endpoint=endpoint_name).token
    conninfo = (
        f"host={host} port=5432 dbname=databricks_postgres "
        f"user={user} sslmode=require"
    )
    return conninfo, {"password": token}


def main():
    parser = argparse.ArgumentParser(description="Partyline message retention job")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("PARTYLINE_RETENTION_DAYS", "30")),
        help="Delete messages older than this many days (default: 30)",
    )
    parser.add_argument(
        "--endpoint-name",
        default=os.environ.get("ENDPOINT_NAME", DEFAULT_ENDPOINT),
        help="Autoscale Lakebase endpoint resource name",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print count of rows that would be deleted without deleting",
    )
    args = parser.parse_args()

    conninfo, kwargs = _conninfo(args.endpoint_name)
    with psycopg.connect(conninfo, **kwargs) as conn:
        with conn.cursor() as cur:
            if args.dry_run:
                cur.execute(
                    f"SELECT count(*) FROM {SCHEMA}.messages "
                    "WHERE created_at < now() - make_interval(days => %s)",
                    (args.retention_days,),
                )
                count = cur.fetchone()[0]
                logger.info("Dry run: %d messages older than %d days", count, args.retention_days)
                print(f"Would delete {count} messages older than {args.retention_days} days")
            else:
                # reply_to_id is a self-FK: detach replies that point at rows about to
                # be pruned, or the DELETE would fail once threads span the boundary.
                cur.execute(
                    f"UPDATE {SCHEMA}.messages SET reply_to_id = NULL "
                    f"WHERE reply_to_id IN (SELECT id FROM {SCHEMA}.messages "
                    "WHERE created_at < now() - make_interval(days => %s))",
                    (args.retention_days,),
                )
                cur.execute(
                    f"DELETE FROM {SCHEMA}.messages "
                    "WHERE created_at < now() - make_interval(days => %s)",
                    (args.retention_days,),
                )
                deleted = cur.rowcount
                conn.commit()
                logger.info("Deleted %d messages older than %d days", deleted, args.retention_days)
                print(f"Deleted {deleted} messages older than {args.retention_days} days")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
