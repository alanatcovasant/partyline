"""Idempotent grant of CAN_USE on the deployed app to the example job's SP.

A freshly created SP is not in the `users` group (which the bundle grants CAN_USE), so it
must be granted explicitly — least privilege: it can reach only this app. Run AFTER the app
is deployed. Idempotent: the permissions PATCH merges the ACL, so re-running is a no-op.

  DATABRICKS_CONFIG_PROFILE=dev-eastus2 APP_NAME=mcp-covasant-partyline \\
    PARTYLINE_SP_APP_ID=<applicationId> python scripts/grant_app_access.py
"""
from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient


def grant(w: WorkspaceClient, app_name: str, sp_app_id: str) -> None:
    """Grant CAN_USE to sp_app_id on app_name. Prefer the typed SDK method; fall back to REST
    for older SDKs (the deployed CLI's SDK may lag — see the version-skew note in the repo)."""
    if hasattr(w.apps, "update_permissions"):
        from databricks.sdk.service import apps as apps_svc
        acl = [apps_svc.AppAccessControlRequest(
            service_principal_name=sp_app_id,
            permission_level=apps_svc.AppPermissionLevel.CAN_USE,
        )]
        # Pass the app name POSITIONALLY. Its first parameter is `app_name` in current SDKs
        # but was `name` in older ones; a keyword binds to whichever the installed SDK uses
        # and raises TypeError on the other. Positional binds regardless. update_permissions
        # is PATCH/merge (idempotent, additive) — not set_permissions (PUT, would replace the
        # users-group grant).
        w.apps.update_permissions(app_name, access_control_list=acl)
    else:
        w.api_client.do(
            "PATCH", f"/api/2.0/permissions/apps/{app_name}",
            body={"access_control_list": [
                {"service_principal_name": sp_app_id, "permission_level": "CAN_USE"}
            ]},
        )


def main() -> None:
    app_name = os.getenv("APP_NAME", "mcp-covasant-partyline")
    sp_app_id = os.getenv("PARTYLINE_SP_APP_ID")
    if not sp_app_id:
        sys.exit("PARTYLINE_SP_APP_ID is required")
    w = WorkspaceClient()
    grant(w, app_name, sp_app_id)
    print(f"[ok] granted CAN_USE on {app_name} to SP {sp_app_id}")


if __name__ == "__main__":
    main()
