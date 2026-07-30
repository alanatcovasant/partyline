"""Idempotent provisioning of the example job's dedicated service principal via account SCIM.

Run (as a user with account SCIM / admin rights) AFTER the app is deployed and BEFORE the
example job is deployed. Prints the SP applicationId as the LAST stdout line so deploy.sh can
capture it for the job's run_as. Safe to re-run: find-by-displayName, create only if absent.

  DATABRICKS_CONFIG_PROFILE=dev-eastus2 python scripts/provision_partyline_sp.py
"""
from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient

DISPLAY_NAME = os.getenv("PARTYLINE_SP_DISPLAY_NAME", "covasant-partyline-example-job")
_SCIM_SP = "/api/2.0/account/scim/v2/ServicePrincipals"


def resolve_or_create_sp(w: WorkspaceClient, display_name: str) -> str:
    """Return the SP applicationId; create the SP via account SCIM only if not present."""
    found = w.api_client.do(
        "GET", _SCIM_SP, query={"filter": f'displayName eq "{display_name}"'}
    )
    resources = (found or {}).get("Resources") or []
    for r in resources:
        if r.get("applicationId"):
            print(f"[ok] SP '{display_name}' exists (applicationId={r['applicationId']})", file=sys.stderr)
            return r["applicationId"]

    print(f"[+] creating SP '{display_name}' via account SCIM", file=sys.stderr)
    created = w.api_client.do(
        "POST", _SCIM_SP,
        body={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"],
            "displayName": display_name,
            "active": True,
        },
    )
    app_id = created.get("applicationId")
    if not app_id:
        sys.exit(f"SCIM create returned no applicationId: {created}")
    return app_id


def main() -> None:
    w = WorkspaceClient()
    app_id = resolve_or_create_sp(w, DISPLAY_NAME)
    # LAST stdout line = the applicationId (deploy.sh: APP_ID="$(... | tail -1)").
    print(app_id)


if __name__ == "__main__":
    main()
