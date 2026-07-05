"""On-Behalf-Of (OBO) user authorization.

Databricks Apps forward the calling user's access token in the
``X-Forwarded-Access-Token`` header when user authorization is enabled on the app
(requires a workspace admin to enable it, plus declared ``user_api_scopes`` in the
bundle). We use it to resolve the authenticated identity for the audit trail
(``messages.author``) — it does NOT distinguish agent *instances*, since e.g. aura_1..6
may all run as the same human. Instance identity comes from the caller-supplied
``agent_id``; OBO answers "which principal authorized this call".

The database connection itself uses the app service principal (see db.py). Minting
per-user Lakebase credentials from this token is a deliberate future step.
"""

from __future__ import annotations

import logging

from fastapi import Request

log = logging.getLogger("partyline.auth")

USER_TOKEN_HEADER = "x-forwarded-access-token"

# Cache resolved identities for the life of the process to avoid a WorkspaceClient
# round-trip on every request (tokens map stably to a user for their lifetime).
_identity_cache: dict[str, str] = {}


def user_token(request: Request) -> str | None:
    return request.headers.get(USER_TOKEN_HEADER)


def resolve_author(request: Request) -> str | None:
    """Resolve the OBO user's identity (user_name) for audit, or None if unavailable.

    Failures are non-fatal: the bus still records the message, just without an author.
    """
    token = user_token(request)
    if not token:
        return None
    if token in _identity_cache:
        return _identity_cache[token]
    try:
        from databricks.sdk import WorkspaceClient

        # The forwarded value is the user's OAuth access token (Databricks Apps do not use
        # PATs). Passing it as `token` sends it as an Authorization: Bearer header, which
        # is what the workspace API expects to resolve the caller's identity.
        me = WorkspaceClient(token=token).current_user.me()
        author = me.user_name or me.display_name
        if author:
            _identity_cache[token] = author
        return author
    except Exception as exc:  # pragma: no cover - identity is best-effort
        log.warning("Could not resolve OBO identity: %s", exc)
        return None
