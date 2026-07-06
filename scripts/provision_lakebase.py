"""Idempotent provisioning of the autoscale Lakebase project/branch/endpoint for Partyline.

Run this (as the deploying user, who has CAN MANAGE) BEFORE `databricks bundle deploy` —
the app's bundle binding requires the branch to already exist at deploy time, and the
autoscale projects/branches/endpoints are not yet bundle-managed resources in the installed
Databricks CLI. So we create them here via the `w.postgres` SDK. Safe to re-run: existing
resources are detected and left untouched (get -> create-if-missing).

Usage:
    ENDPOINT_NAME=projects/partyline/branches/production/endpoints/primary \\
      DATABRICKS_CONFIG_PROFILE=<profile> python scripts/provision_lakebase.py

Defaults to projects/partyline/branches/production/endpoints/primary if ENDPOINT_NAME unset.
"""

from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service import postgres as pg

# Autoscale endpoint capacity (CU). Small floor for scale-to-zero; modest ceiling for bursts.
MIN_CU = 0.5
MAX_CU = 2.0
PG_VERSION = 17

ENDPOINT_NAME = os.getenv(
    "ENDPOINT_NAME", "projects/partyline/branches/production/endpoints/primary"
)


def _parse(endpoint_name: str) -> tuple[str, str, str]:
    """projects/<pid>/branches/<bid>/endpoints/<eid> -> (project, branch, endpoint) names."""
    p = endpoint_name.split("/")
    if len(p) != 6 or p[0] != "projects" or p[2] != "branches" or p[4] != "endpoints":
        sys.exit(f"ENDPOINT_NAME is not a full endpoint resource name: {endpoint_name!r}")
    return p[1], p[3], p[5]


def main() -> None:
    project_id, branch_id, endpoint_id = _parse(ENDPOINT_NAME)
    project = f"projects/{project_id}"
    branch = f"{project}/branches/{branch_id}"
    w = WorkspaceClient()

    try:
        w.postgres.get_project(name=project)
        print(f"[ok] project {project_id} exists")
    except NotFound:
        print(f"[+] creating project {project_id}")
        w.postgres.create_project(
            project=pg.Project(spec=pg.ProjectSpec(display_name=project_id, pg_version=PG_VERSION)),
            project_id=project_id,
        )

    try:
        w.postgres.get_branch(name=branch)
        print(f"[ok] branch {branch_id} exists")
    except NotFound:
        print(f"[+] creating branch {branch_id}")
        w.postgres.create_branch(
            parent=project,
            branch=pg.Branch(spec=pg.BranchSpec(no_expiry=True)),
            branch_id=branch_id,
        )

    try:
        w.postgres.get_endpoint(name=ENDPOINT_NAME)
        print(f"[ok] endpoint {endpoint_id} exists")
    except NotFound:
        print(f"[+] creating endpoint {endpoint_id}")
        w.postgres.create_endpoint(
            parent=branch,
            endpoint=pg.Endpoint(
                spec=pg.EndpointSpec(
                    autoscaling_limit_min_cu=MIN_CU,
                    autoscaling_limit_max_cu=MAX_CU,
                )
            ),
            endpoint_id=endpoint_id,
        )

    # Register the Postgres database as a Unity Catalog catalog so the bus tables —
    # including the retention archive (partyline.messages_archive) — are queryable
    # from UC/warehouses. (Not yet in the installed SDK; use the REST API directly.)
    catalog_id = f"{project_id.replace('-', '_')}_pg"
    try:
        w.api_client.do("GET", f"/api/2.0/postgres/catalogs/{catalog_id}")
        print(f"[ok] UC catalog {catalog_id} exists")
    except NotFound:
        print(f"[+] registering UC catalog {catalog_id}")
        w.api_client.do(
            "POST",
            "/api/2.0/postgres/catalogs",
            query={"catalog_id": catalog_id},
            body={"spec": {"postgres_database": "databricks_postgres", "branch": branch}},
        )

    print(f"[ok] Lakebase ready: {ENDPOINT_NAME} (UC catalog: {catalog_id})")


if __name__ == "__main__":
    main()
