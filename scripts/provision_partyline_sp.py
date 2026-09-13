"""Idempotent provisioning of the example job's dedicated service principal + OAuth secret.

Run (as a user with account SCIM + workspace admin rights) AFTER the app is deployed and
BEFORE the example job is deployed. Prints the SP applicationId as the LAST stdout line so
deploy.sh can capture it. Safe to re-run.

Provisions, in order:
  1. Account-SCIM SP (find-by-displayName, create if absent) -> applicationId
  2. Workspace SP membership: the account SP must be IN the workspace, or the workspace
     OIDC token endpoint returns invalid_client -> numeric id
  3. A fresh OAuth secret for the SP (delete-all-then-mint-one: the cleartext secret is only
     available at mint time, so an existing secret cannot be reused)
  4. A secret scope holding the SP's client_id + client_secret, with a READ ACL for the SP
     (load-bearing in prod where the job runs as the SP; in dev the deploying user reads via
     creator-MANAGE)

  DATABRICKS_CONFIG_PROFILE=dev-eastus2 python scripts/provision_partyline_sp.py
"""
from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import AclPermission

DISPLAY_NAME = os.getenv("PARTYLINE_SP_DISPLAY_NAME", "covasant-partyline-example-job")
SECRET_SCOPE = os.getenv("PARTYLINE_SECRET_SCOPE", "covasant-partyline-example-job")
CLIENT_ID_KEY = os.getenv("PARTYLINE_CLIENT_ID_KEY", "sp_client_id")
CLIENT_SECRET_KEY = os.getenv("PARTYLINE_CLIENT_SECRET_KEY", "sp_client_secret")
# 90 days; the job mints short-lived per-run tokens from it, so a long-lived secret is fine.
SECRET_LIFETIME_SECONDS = int(os.getenv("PARTYLINE_SECRET_LIFETIME_SECONDS", "7776000"))
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


def ensure_workspace_sp(w: WorkspaceClient, app_id: str, display_name: str) -> str:
    """Return the SP's WORKSPACE numeric id, adding it to the workspace if not present.

    The account-SCIM SP is not automatically a workspace member; without membership the
    workspace OIDC token endpoint rejects its client-credentials with invalid_client.
    w.service_principals.create(application_id=...) on a WorkspaceClient references the
    existing account SP by applicationId and adds it to this workspace.
    """
    hits = list(w.service_principals.list(filter=f'applicationId eq "{app_id}"'))
    if hits:
        print(f"[ok] SP {app_id} already in workspace (id={hits[0].id})", file=sys.stderr)
        return str(hits[0].id)
    created = w.service_principals.create(application_id=app_id, display_name=display_name)
    print(f"[+] added SP {app_id} to workspace (id={created.id})", file=sys.stderr)
    return str(created.id)


def mint_fresh_secret(w: WorkspaceClient, sp_numeric_id: str, lifetime_seconds: int) -> str:
    """Delete all existing OAuth secrets for the SP, then mint exactly one; return cleartext.

    Delete-all-then-mint-one (not skip-if-exists): the cleartext secret is only readable at
    mint time, so a pre-existing secret is unusable here. Snapshot ids first, then delete —
    listing and deleting in one pass races and can leave a secret behind.
    """
    existing = [s.id for s in w.service_principal_secrets_proxy.list(sp_numeric_id)]
    for sid in existing:
        w.service_principal_secrets_proxy.delete(sp_numeric_id, sid)
    if existing:
        print(f"[ok] deleted {len(existing)} stale SP secret(s)", file=sys.stderr)
    created = w.service_principal_secrets_proxy.create(
        sp_numeric_id, lifetime=f"{lifetime_seconds}s"
    )
    print(f"[+] minted SP OAuth secret (id={created.id})", file=sys.stderr)
    return created.secret


def ensure_secret_scope(
    w: WorkspaceClient, scope: str, client_id: str, client_secret: str, sp_app_id: str
) -> None:
    """Create the scope if absent, (over)write client_id + client_secret, grant SP READ.

    create_scope raises if the scope already exists, so guard it with list_scopes. put_secret
    and put_acl are idempotent, so call them unconditionally — the scope creator holds MANAGE,
    and the SP READ ACL matters in prod (job runs as the SP)."""
    if scope not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(scope)
        print(f"[+] created secret scope '{scope}'", file=sys.stderr)
    else:
        print(f"[ok] secret scope '{scope}' exists", file=sys.stderr)
    w.secrets.put_secret(scope, CLIENT_ID_KEY, string_value=client_id)
    w.secrets.put_secret(scope, CLIENT_SECRET_KEY, string_value=client_secret)
    w.secrets.put_acl(scope, sp_app_id, AclPermission.READ)
    print(f"[ok] wrote client_id/client_secret + READ acl for {sp_app_id}", file=sys.stderr)


def main() -> None:
    w = WorkspaceClient()
    app_id = resolve_or_create_sp(w, DISPLAY_NAME)
    numeric_id = ensure_workspace_sp(w, app_id, DISPLAY_NAME)
    secret = mint_fresh_secret(w, numeric_id, SECRET_LIFETIME_SECONDS)
    ensure_secret_scope(w, SECRET_SCOPE, app_id, secret, app_id)
    # LAST stdout line = the applicationId (deploy.sh: SP_APP_ID="$(... | tail -1)").
    print(app_id)


if __name__ == "__main__":
    main()
