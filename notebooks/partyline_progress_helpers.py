"""Pure, stdlib-only helpers for the Partyline progress-reporting example job.

Deliberately free of dbutils / WorkspaceClient / Databricks imports so the logic is
unit-testable off-cluster and the file is a clean copy-and-adapt reference. The notebook
(partyline_progress.py) supplies the run-context values (job id, run id, token, url) and
these functions turn them into REST calls. REST is done with stdlib urllib — the same
mechanism the partyline CLI uses internally — so no client wheel needs to be installed on
the serverless task.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


def derive_agent_id(job_id: str | None, run_id: str | None, override: str | None) -> str:
    """Agent id for the line. A non-blank override wins; otherwise derive from run context.

    The derived form (job_<job_id>_run_<run_id>) is intentionally computed from runtime
    context, never a checked-in constant — a committed id is exactly the coupling that
    causes cross-delivery (defect #1545), so the example must not reintroduce it.
    """
    if override and override.strip():
        return override.strip()
    if job_id and run_id:
        return f"job_{job_id}_run_{run_id}"
    return "job_adhoc"


def build_start_summary(run_label: str | None) -> str:
    base = "run started"
    if run_label and run_label.strip():
        return f"{base} — {run_label.strip()}"
    return base


def build_step_summary(task_label: str, metric: int) -> str:
    return f"{task_label}: processed {metric:,} rows"


def build_finish_summary(outcomes: list[str]) -> str:
    if outcomes:
        return "run complete — " + "; ".join(outcomes)
    return "run complete — no task outcomes reported"


def build_failure_summary(outcomes: list[str]) -> str:
    if outcomes:
        return "run finished with upstream failure — " + "; ".join(outcomes)
    return "run finished with upstream failure — no task outcomes reported"


def simulated_metric(task_label: str) -> int:
    """A deterministic stand-in for 'work done' — no RNG so runs/tests are reproducible."""
    return 1000 + (sum(ord(c) for c in task_label) % 1000)


class PartylineError(RuntimeError):
    """A partyline REST call failed. Carries the HTTP status (or None for a transport error)
    and the response body, so a failed POST surfaces loudly rather than looking like success."""

    def __init__(self, status: int | None, body: str):
        super().__init__(f"partyline call failed (status={status}): {body[:300]}")
        self.status = status
        self.body = body


def _request(method: str, url: str, token: str | None, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        raise PartylineError(e.code, e.read().decode(errors="replace")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PartylineError(None, str(getattr(e, "reason", e))) from e


def register_agent(url, token, agent_id, owner, repo, branch, *, timeout=30.0):
    return _request("POST", url.rstrip("/") + "/api/agents", token, {
        "agent_id": agent_id, "owner": owner, "repo": repo,
        "branch": branch, "display_name": agent_id,
    }, timeout)


def post_message(url, token, *, agent_id, message_type, summary, payload=None,
                 repo=None, branch=None, recipient=None, reply_to_id=None, timeout=30.0):
    return _request("POST", url.rstrip("/") + "/api/messages", token, {
        "agent_id": agent_id, "message_type": message_type, "summary": summary,
        "payload": payload, "repo": repo, "branch": branch,
        "recipient": recipient, "reply_to_id": reply_to_id,
        "mentions": [], "metadata": {},
    }, timeout)


def mint_m2m_token(host, client_id, client_secret, *, timeout=30.0):
    """Mint a workspace-audience M2M OAuth token via the OIDC token endpoint.

    Uses the client-credentials grant with scope=all-apis — the token TYPE the Databricks
    Apps OAuth proxy accepts. The serverless task's ambient runtime credential is the WRONG
    type and the proxy rejects it with 401; a token minted here is accepted. Basic-auth
    client_id:client_secret, form-encoded body, per the OIDC client-credentials spec.

    Raises PartylineError on transport failure or a response with no access_token. A 401 with
    an "invalid_client" body means the SP secret has not propagated yet — the caller should
    retry (secret propagation after a fresh mint can lag a few seconds).
    """
    token_url = host.rstrip("/") + "/oidc/v1/token"
    data = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": "all-apis"}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        raise PartylineError(e.code, e.read().decode(errors="replace")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PartylineError(None, str(getattr(e, "reason", e))) from e
    token = (payload or {}).get("access_token")
    if not token:
        raise PartylineError(None, f"no access_token in OIDC response: {payload}")
    return token
