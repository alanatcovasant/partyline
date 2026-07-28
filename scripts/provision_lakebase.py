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
# Suspend after 5 min idle (PRD G5/NFR4 cheap-at-rest). The protocol is fail-open, so a
# cold start only delays awareness, never blocks work; agents warm it on first call.
SUSPEND_SECONDS = 300
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

    desired = pg.EndpointSpec(
        endpoint_type=pg.EndpointType.ENDPOINT_TYPE_READ_WRITE,
        autoscaling_limit_min_cu=MIN_CU,
        autoscaling_limit_max_cu=MAX_CU,
        suspend_timeout_duration=pg.Duration(seconds=SUSPEND_SECONDS),
    )
    try:
        ep = w.postgres.get_endpoint(name=ENDPOINT_NAME)
        # Reconcile, don't just create-if-missing: endpoints provisioned out-of-band
        # (or by older script versions) drift from the PRD's scale-to-zero posture.
        st = ep.status
        cu_drift = (
            getattr(st, "autoscaling_limit_min_cu", None) != MIN_CU
            or getattr(st, "autoscaling_limit_max_cu", None) != MAX_CU
        )
        if cu_drift:
            print(
                f"[~] reconciling endpoint {endpoint_id} CU: "
                f"{getattr(st, 'autoscaling_limit_min_cu', '?')}-"
                f"{getattr(st, 'autoscaling_limit_max_cu', '?')} -> {MIN_CU}-{MAX_CU}"
            )
            mask = pg.FieldMask()
            mask.paths = [
                "spec.autoscaling_limit_min_cu",
                "spec.autoscaling_limit_max_cu",
            ]
            w.postgres.update_endpoint(
                name=ENDPOINT_NAME,
                endpoint=pg.Endpoint(spec=desired),
                update_mask=mask,
            )
        else:
            print(f"[ok] endpoint {endpoint_id} exists (CU limits match)")
        current_suspend = getattr(
            getattr(st, "suspend_timeout_duration", None), "seconds", None
        )
        if current_suspend != SUSPEND_SECONDS:
            # The endpoint-level suspend timeout is not updatable via PATCH in this API
            # version (no accepted update_mask path); only new endpoints inherit the
            # project default set below. Flag it rather than silently ignoring.
            print(
                f"[!] endpoint {endpoint_id} suspend timeout is {current_suspend}s "
                f"(want {SUSPEND_SECONDS}s) — not updatable in place; will apply on "
                f"endpoint recreation"
            )
    except NotFound:
        print(f"[+] creating endpoint {endpoint_id}")
        w.postgres.create_endpoint(
            parent=branch,
            endpoint=pg.Endpoint(spec=desired),
            endpoint_id=endpoint_id,
        )

    # Project-level default endpoint settings: new endpoints inherit these (the
    # existing endpoint's suspend timeout cannot be patched — see note above).
    proj = w.postgres.get_project(name=project)
    des = getattr(proj.status, "default_endpoint_settings", None)
    if getattr(getattr(des, "suspend_timeout_duration", None), "seconds", None) != SUSPEND_SECONDS:
        print(f"[~] setting project default endpoint settings (suspend {SUSPEND_SECONDS}s)")
        w.api_client.do(
            "PATCH",
            f"/api/2.0/postgres/{project}",
            query={"update_mask": "spec.default_endpoint_settings"},
            body={"spec": {"default_endpoint_settings": {
                "autoscaling_limit_min_cu": MIN_CU,
                "autoscaling_limit_max_cu": MAX_CU,
                "suspend_timeout_duration": f"{SUSPEND_SECONDS}s",
            }}},
        )
    else:
        print("[ok] project default endpoint settings match")

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
