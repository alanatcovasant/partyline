"""Partyline retention job — prunes old messages from Lakebase Postgres.

Native Lakehouse Sync (wal2delta) mirrors rows to Delta SCD2 history *before*
they're deleted, so the archive persists in Unity Catalog while the OLTP tier
stays small.  See PRD §7.10.
"""

import argparse
import logging
import os

import psycopg

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Partyline message retention job")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("PARTYLINE_RETENTION_DAYS", "30")),
        help="Delete messages older than this many days (default: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print count of rows that would be deleted without deleting",
    )
    args = parser.parse_args()

    conninfo = os.environ.get(
        "PARTYLINE_PG_CONNINFO",
        f"host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ.get('PGDATABASE', 'partyline')} "
        f"user={os.environ.get('PGUSER', 'partyline')} "
        f"sslmode={os.environ.get('PGSSLMODE', 'prefer')}",
    )

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            if args.dry_run:
                cur.execute(
                    "SELECT count(*) FROM messages "
                    "WHERE created_at < now() - make_interval(days => %s)",
                    (args.retention_days,),
                )
                count = cur.fetchone()[0]
                logger.info("Dry run: %d messages older than %d days", count, args.retention_days)
                print(f"Would delete {count} messages older than {args.retention_days} days")
            else:
                cur.execute(
                    "DELETE FROM messages "
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
